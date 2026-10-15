"""Scheduled transit service intensity from GTFS static feeds (+ live service alerts).

Feeds (current versions only — see "historical" caveat below):

* King County Metro (official ``metro.kingcounty.gov/GTFS/google_transit.zip``;
  also contains the Seattle Streetcar and the ST Express routes Metro operates),
* Sound Transit (official ``soundtransit.org/GTFS-rail/40_gtfs.zip``: Link 1/2/T
  lines, Sounder, rail shuttles),
* Community Transit and Pierce Transit (they operate the ST Express routes
  510/512/513 and 59x into downtown Seattle),
* Washington State Ferries (Colman Dock / Fauntleroy terminals).

For each feed a *representative week* (Mon–Sun, inside the feed's validity
window, no US federal holiday, no DST change; the first such week starting on
or after today) is expanded from calendar.txt / calendar_dates.txt, and every
boardable stop_time (``pickup_type != 1``) of every active trip is counted by
local hour-of-week (Monday 00:00 = 0). GTFS times past 24:00 wrap into the
next day; Sunday-night service wraps into Monday 00–03 (circular week).

Outputs (stops inside the config bbox + ~1 km buffer):

* ``data/processed/transit_stops.parquet`` — stop_id (``<feed>:<gtfs id>``),
  feed, agency, stop_name, lat, lon, modes, n_routes, trips_per_week
  (= boardable scheduled departures in the representative week);
* ``data/processed/transit_stop_how.parquet`` — stop_id, hour_of_week,
  scheduled_departures (only non-zero combinations are stored).

This is a *static proxy*: only the current feed versions are downloadable
without keys, so the same weekly profile is applied to all historical hours.

:func:`fetch_gtfs_rt_alerts` snapshots the public GTFS-realtime service-alert
feeds of King County Metro (S3) and Sound Transit (OneBusAway Puget Sound
endpoint, no key) and appends to ``transit_alerts.parquet`` — a forward-only
collection (no history is published).
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

from ..config import Config
from ._context_common import HttpError, RawSnapshot, http_get, read_gz, session, write_parquet

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    urls: tuple[str, ...]  # tried in order


FEEDS: tuple[Feed, ...] = (
    Feed(
        "kcm",
        "King County Metro",
        ("https://metro.kingcounty.gov/GTFS/google_transit.zip", "https://gtfs.sound.obaweb.org/prod/1_gtfs.zip"),
    ),
    Feed(
        "st",
        "Sound Transit",
        ("https://www.soundtransit.org/GTFS-rail/40_gtfs.zip", "https://gtfs.sound.obaweb.org/prod/40_gtfs.zip"),
    ),
    Feed(
        "ct",
        "Community Transit",
        (
            "https://www.communitytransit.org/docs/default-source/open-data/gtfs/current.zip",
            "https://gtfs.sound.obaweb.org/prod/29_gtfs.zip",
        ),
    ),
    Feed("pt", "Pierce Transit", ("https://gtfs.sound.obaweb.org/prod/3_gtfs.zip",)),
    Feed("wsf", "Washington State Ferries", ("https://gtfs.sound.obaweb.org/prod/95_gtfs.zip",)),
    Feed(
        "monorail",
        "Seattle Center Monorail",
        (
            "https://gtfs.sound.obaweb.org/prod/96_gtfs.zip",
            "https://github.com/Seattle-Monorail-Services/gtfs/raw/refs/heads/main/96_gtfs.zip",
        ),
    ),
)

ALERT_FEEDS = {  # public, no key (observed 2026-10-14); OBA agencies 23/95/96 answer HTTP 401
    "kcm": "https://s3.amazonaws.com/kcm-alerts-realtime-prod/alerts.pb",
    "st": "https://api.pugetsound.onebusaway.org/api/gtfs_realtime/alerts-for-agency/40.pb",
}
BBOX_BUFFER_DEG = 0.01
ROUTE_TYPE = {
    0: "light_rail",
    1: "subway",
    2: "rail",
    3: "bus",
    4: "ferry",
    5: "cable_tram",
    6: "aerial",
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
}
CAUSE = {
    1: "UNKNOWN_CAUSE",
    2: "OTHER_CAUSE",
    3: "TECHNICAL_PROBLEM",
    4: "STRIKE",
    5: "DEMONSTRATION",
    6: "ACCIDENT",
    7: "HOLIDAY",
    8: "WEATHER",
    9: "MAINTENANCE",
    10: "CONSTRUCTION",
    11: "POLICE_ACTIVITY",
    12: "MEDICAL_EMERGENCY",
}
EFFECT = {
    1: "NO_SERVICE",
    2: "REDUCED_SERVICE",
    3: "SIGNIFICANT_DELAYS",
    4: "DETOUR",
    5: "ADDITIONAL_SERVICE",
    6: "MODIFIED_SERVICE",
    7: "OTHER_EFFECT",
    8: "UNKNOWN_EFFECT",
    9: "STOP_MOVED",
    10: "NO_EFFECT",
    11: "ACCESSIBILITY_ISSUE",
}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --------------------------------------------------------------------------- GTFS static (pure)


def read_gtfs_table(z: zipfile.ZipFile, name: str, usecols: list[str] | None = None) -> pd.DataFrame:
    """Read one GTFS text file as strings (BOM-safe); missing optional files -> empty frame."""
    if name not in z.namelist():
        return pd.DataFrame(columns=usecols or [])
    with z.open(name) as f:
        df = pd.read_csv(
            f, dtype=str, keep_default_na=False, encoding="utf-8-sig", usecols=lambda c: usecols is None or c.strip() in usecols
        )
    df.columns = [c.strip() for c in df.columns]
    return df


def gtfs_time_to_seconds(s: pd.Series) -> pd.Series:
    """'HH:MM:SS' (HH may exceed 23) -> seconds after service-day start; blanks -> NaN."""
    parts = s.fillna("").str.strip().str.split(":", expand=True)
    if parts.shape[1] < 3:
        return pd.Series(np.nan, index=s.index)
    h, m, sec = (pd.to_numeric(parts[i], errors="coerce") for i in range(3))
    return h * 3600 + m * 60 + sec


def active_services(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, days: list[date]) -> pd.DataFrame:
    """Expand calendar + calendar_dates into (service_id, service_date) rows for ``days``."""
    rows = []
    for d in days:
        ds = d.strftime("%Y%m%d")
        if not calendar.empty:
            c = calendar[(calendar["start_date"] <= ds) & (calendar["end_date"] >= ds) & (calendar[DAYS[d.weekday()]] == "1")]
            base = set(c["service_id"])
        else:
            base = set()
        if not calendar_dates.empty:
            cd = calendar_dates[calendar_dates["date"] == ds]
            base |= set(cd.loc[cd["exception_type"] == "1", "service_id"])
            base -= set(cd.loc[cd["exception_type"] == "2", "service_id"])
        rows += [(sid, d) for sid in base]
    return pd.DataFrame(rows, columns=["service_id", "service_date"])


def feed_window(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, feed_info: pd.DataFrame) -> tuple[date, date]:
    if not feed_info.empty and feed_info.get("feed_start_date", pd.Series([""])).iloc[0]:
        a, b = feed_info["feed_start_date"].iloc[0], feed_info["feed_end_date"].iloc[0]
    else:
        dates = list(calendar.get("start_date", [])) + list(calendar.get("end_date", [])) + list(calendar_dates.get("date", []))
        a, b = min(dates), max(dates)
    return datetime.strptime(a, "%Y%m%d").date(), datetime.strptime(b, "%Y%m%d").date()


def representative_week(start: date, end: date, tz: str, not_before: date | None = None) -> date:
    """First Monday >= max(start, not_before) whose Mon–Sun lies in [start, end] with no
    US federal holiday and no DST transition. Falls back to the first full week."""
    first = max(start, not_before or start)
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    hol = set(USFederalHolidayCalendar().holidays(start=str(start), end=str(end + timedelta(days=7))).date)
    candidate = monday
    while candidate + timedelta(days=6) <= end:
        days = [candidate + timedelta(days=i) for i in range(8)]
        offsets = {pd.Timestamp(d).tz_localize(tz).utcoffset() for d in days}
        if not (set(days[:7]) & hol) and len(offsets) == 1:
            return candidate
        candidate += timedelta(days=7)
    fallback = start + timedelta(days=(7 - start.weekday()) % 7)
    log.warning("no clean representative week in %s..%s; using %s", start, end, fallback)
    return fallback


def stop_hour_of_week(stop_times: pd.DataFrame, trips: pd.DataFrame, services: pd.DataFrame) -> pd.DataFrame:
    """Boardable scheduled departures per (stop_id, local hour_of_week 0..167, Monday-00 first)."""
    st = stop_times.copy()
    if "pickup_type" in st:
        st = st[st["pickup_type"].fillna("").str.strip() != "1"]
    dep = gtfs_time_to_seconds(st["departure_time"])
    if "arrival_time" in st:
        dep = dep.fillna(gtfs_time_to_seconds(st["arrival_time"]))
    st = st.assign(dep_s=dep, seq=pd.to_numeric(st["stop_sequence"], errors="coerce"))
    if st["dep_s"].isna().any():  # untimed stops: carry the previous timepoint's time
        st = st.sort_values(["trip_id", "seq"])
        st["dep_s"] = st.groupby("trip_id")["dep_s"].ffill()
    st = st.dropna(subset=["dep_s"])
    st["hh"] = (st["dep_s"] // 3600).astype(int)
    per = st.merge(trips[["trip_id", "service_id"]], on="trip_id").groupby(["service_id", "stop_id", "hh"]).size()
    per = per.rename("n").reset_index()
    svc = services.assign(dow=[d.weekday() for d in services["service_date"]])[["service_id", "dow"]]
    x = per.merge(svc, on="service_id")
    x["hour_of_week"] = (x["dow"] * 24 + x["hh"]) % 168
    out = x.groupby(["stop_id", "hour_of_week"])["n"].sum().rename("scheduled_departures").reset_index()
    out["scheduled_departures"] = out["scheduled_departures"].astype("int32")
    out["hour_of_week"] = out["hour_of_week"].astype("int16")
    return out


def summarize_feed(feed: Feed, z: zipfile.ZipFile, tz: str, bbox: list[float], not_before: date) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    calendar = read_gtfs_table(z, "calendar.txt")
    calendar_dates = read_gtfs_table(z, "calendar_dates.txt")
    feed_info = read_gtfs_table(z, "feed_info.txt")
    start, end = feed_window(calendar, calendar_dates, feed_info)
    week = representative_week(start, end, tz, not_before)
    days = [week + timedelta(days=i) for i in range(7)]
    services = active_services(calendar, calendar_dates, days)

    stops = read_gtfs_table(z, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"])
    stops = stops.reindex(columns=["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"])
    stops["lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
    w, s, e, n = bbox
    b = BBOX_BUFFER_DEG
    stops = stops[stops["lat"].between(s - b, n + b) & stops["lon"].between(w - b, e + b)]
    stops = stops[stops["location_type"].fillna("").isin(["", "0"])]
    keep = set(stops["stop_id"])

    trips = read_gtfs_table(z, "trips.txt", ["trip_id", "service_id", "route_id"])
    routes = read_gtfs_table(z, "routes.txt", ["route_id", "agency_id", "route_type"])
    agency = read_gtfs_table(z, "agency.txt", ["agency_id", "agency_name"])
    with z.open("stop_times.txt") as f:  # chunked: keep only in-bbox stops
        chunks = []
        cols = ["trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence", "pickup_type"]
        for ch in pd.read_csv(
            f, dtype=str, keep_default_na=False, encoding="utf-8-sig", chunksize=1_000_000, usecols=lambda c: c.strip() in cols
        ):
            ch.columns = [c.strip() for c in ch.columns]
            chunks.append(ch[ch["stop_id"].isin(keep)])
    stop_times = pd.concat(chunks, ignore_index=True)
    how = stop_hour_of_week(stop_times, trips, services)

    # stop attributes: agencies / modes / routes serving the stop during the week
    act = trips[trips["service_id"].isin(set(services["service_id"]))]
    sr = stop_times[["trip_id", "stop_id"]].drop_duplicates().merge(act[["trip_id", "route_id"]], on="trip_id")
    sr = sr[["stop_id", "route_id"]].drop_duplicates().merge(routes, on="route_id", how="left")
    if not agency.empty and "agency_id" in sr:
        if len(agency) == 1:
            sr["agency_name"] = agency["agency_name"].iloc[0]
        else:
            sr = sr.merge(agency, on="agency_id", how="left")
    else:
        sr["agency_name"] = feed.name
    sr["mode"] = pd.to_numeric(sr["route_type"], errors="coerce").map(ROUTE_TYPE).fillna("other")
    attrs = sr.groupby("stop_id").agg(
        agency=("agency_name", lambda x: "|".join(sorted({str(v) for v in x if isinstance(v, str) and v}))),
        modes=("mode", lambda x: "|".join(sorted(set(x)))),
        n_routes=("route_id", "nunique"),
    )
    tot = how.groupby("stop_id")["scheduled_departures"].sum().rename("trips_per_week")
    out = stops[["stop_id", "stop_name", "lat", "lon", "parent_station"]].merge(attrs, on="stop_id", how="left")
    out = out.merge(tot, on="stop_id", how="left")
    out["trips_per_week"] = out["trips_per_week"].fillna(0).astype("int32")
    out["n_routes"] = out["n_routes"].fillna(0).astype("int16")
    out["agency"] = out["agency"].fillna(feed.name).replace("", feed.name)
    out["feed"] = feed.key
    out["rep_week_start"] = pd.Timestamp(week)
    out["stop_id"] = feed.key + ":" + out["stop_id"]
    how["stop_id"] = feed.key + ":" + how["stop_id"]
    info = {
        "feed_window": [str(start), str(end)],
        "feed_version": feed_info["feed_version"].iloc[0] if "feed_version" in feed_info and len(feed_info) else None,
        "representative_week_start": str(week),
        "active_service_ids": int(services["service_id"].nunique()),
        "stops_in_bbox": len(out),
        "stops_with_service": int((out["trips_per_week"] > 0).sum()),
        "weekly_departures_in_bbox": int(out["trips_per_week"].sum()),
    }
    return out, how, info


# --------------------------------------------------------------------------- GTFS-realtime alerts (pure)


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def pb_fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Minimal protobuf wire-format reader yielding (field_number, value)."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
            yield field, v
        elif wt == 2:
            ln, i = _varint(buf, i)
            if ln > n - i:
                raise ValueError("truncated length-delimited field")
            yield field, buf[i : i + ln]
            i += ln
        elif wt == 1:
            yield field, buf[i : i + 8]
            i += 8
        elif wt == 5:
            yield field, buf[i : i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")


def _translated(buf: bytes) -> str | None:
    texts = {}
    for f, v in pb_fields(buf):
        if f == 1 and isinstance(v, bytes):
            t = dict((ff, vv) for ff, vv in pb_fields(v))
            lang = t.get(2, b"").decode("utf-8", "replace") if isinstance(t.get(2), bytes) else ""
            texts[lang] = t.get(1, b"").decode("utf-8", "replace") if isinstance(t.get(1), bytes) else ""
    return texts.get("en") or texts.get("") or next(iter(texts.values()), None)


def decode_gtfs_rt_alerts(buf: bytes) -> tuple[int | None, list[dict[str, Any]]]:
    """Decode a GTFS-realtime FeedMessage into (header timestamp, alert dicts)."""
    header_ts, alerts = None, []
    for f, v in pb_fields(buf):
        if f == 1 and isinstance(v, bytes):
            header_ts = next((int(x) for ff, x in pb_fields(v) if ff == 3 and isinstance(x, int)), None)
        elif f == 2 and isinstance(v, bytes):
            ent_id, alert = None, None
            for ff, vv in pb_fields(v):
                if ff == 1 and isinstance(vv, bytes):
                    ent_id = vv.decode("utf-8", "replace")
                elif ff == 5 and isinstance(vv, bytes):
                    alert = vv
            if alert is None:
                continue
            a: dict[str, Any] = {"alert_id": ent_id, "periods": [], "route_ids": set(), "stop_ids": set(), "agency_ids": set()}
            for af, av in pb_fields(alert):
                if af == 1 and isinstance(av, bytes):
                    p = dict(pb_fields(av))
                    a["periods"].append((p.get(1), p.get(2)))
                elif af == 5 and isinstance(av, bytes):
                    for sf, sv in pb_fields(av):
                        key = {1: "agency_ids", 2: "route_ids", 5: "stop_ids"}.get(sf)
                        if key and isinstance(sv, bytes):
                            a[key].add(sv.decode("utf-8", "replace"))
                elif af == 6 and isinstance(av, int):
                    a["cause"] = CAUSE.get(av, str(av))
                elif af == 7 and isinstance(av, int):
                    a["effect"] = EFFECT.get(av, str(av))
                elif af == 8 and isinstance(av, bytes):
                    a["url"] = _translated(av)
                elif af == 10 and isinstance(av, bytes):
                    a["header_text"] = _translated(av)
                elif af == 11 and isinstance(av, bytes):
                    a["description_text"] = _translated(av)
            alerts.append(a)
    return header_ts, alerts


def alerts_to_frame(header_ts: int | None, alerts: list[dict[str, Any]], feed: str, fetched_at: datetime) -> pd.DataFrame:
    rows = []
    for a in alerts:
        starts = [s for s, _ in a["periods"] if isinstance(s, int) and s > 0]
        ends = [e for _, e in a["periods"] if isinstance(e, int) and e > 0]
        rows.append(
            {
                "feed": feed,
                "alert_id": a["alert_id"],
                "cause": a.get("cause"),
                "effect": a.get("effect"),
                "header_text": a.get("header_text"),
                "description_text": a.get("description_text"),
                "url": a.get("url"),
                "route_ids": "|".join(sorted(a["route_ids"])),
                "stop_ids": "|".join(sorted(a["stop_ids"])),
                "n_active_periods": len(a["periods"]),
                "active_start_utc": pd.to_datetime(min(starts), unit="s", utc=True) if starts else pd.NaT,
                "active_end_utc": pd.to_datetime(max(ends), unit="s", utc=True) if ends else pd.NaT,
                "feed_header_utc": pd.to_datetime(header_ts, unit="s", utc=True) if header_ts else pd.NaT,
                "first_seen_at_utc": pd.Timestamp(fetched_at),
                "last_seen_at_utc": pd.Timestamp(fetched_at),
            }
        )
    return pd.DataFrame(rows)


def merge_alert_history(hist: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append alerts; an alert keeps its earliest first_seen and latest last_seen time."""
    if hist.empty:
        return new
    allf = pd.concat([hist, new], ignore_index=True)
    key = ["feed", "alert_id", "header_text", "active_start_utc"]
    first = allf.groupby(key, dropna=False)["first_seen_at_utc"].transform("min")
    last = allf.groupby(key, dropna=False)["last_seen_at_utc"].transform("max")
    allf = allf.assign(first_seen_at_utc=first, last_seen_at_utc=last)
    return allf.drop_duplicates(key, keep="last").reset_index(drop=True)


# --------------------------------------------------------------------------- fetchers


def _download_feed(snap: RawSnapshot, sess: Any, feed: Feed) -> bytes | None:
    for url in feed.urls:
        try:
            r = http_get(sess, url, timeout=600, retries=3)
        except HttpError as e:
            snap.blocked(f"gtfs {feed.key}", url, e.status, e.detail)
            continue
        if not r.content.startswith(b"PK"):
            snap.blocked(f"gtfs {feed.key}", url, r.status_code, "response is not a zip archive")
            continue
        snap.save(f"{feed.key}_gtfs.zip", r.content, r.url, compress=False, requested_url=url, last_modified=r.headers.get("Last-Modified"))
        return r.content
    return None


def fetch_gtfs_rt_alerts(cfg: Config) -> pd.DataFrame:
    """Snapshot the public GTFS-rt alert feeds and append to transit_alerts.parquet."""
    snap = RawSnapshot(cfg.raw_dir, "gtfs_rt_alerts")
    sess = session()
    frames = []
    for feed, url in ALERT_FEEDS.items():
        try:
            r = http_get(sess, url, timeout=60)
        except HttpError as e:
            snap.blocked(f"gtfs-rt alerts {feed}", url, e.status, e.detail)
            continue
        snap.save(f"{feed}_alerts.pb", r.content, url)
        header_ts, alerts = decode_gtfs_rt_alerts(r.content)
        frames.append(alerts_to_frame(header_ts, alerts, feed, snap.fetched_at))
    new = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    path = cfg.processed_dir / "transit_alerts.parquet"
    hist = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    merged = merge_alert_history(hist, new) if not new.empty else hist
    if not merged.empty:
        write_parquet(merged, path)
    snap.finish(
        license="King County Metro developer terms of use; Sound Transit Open Transit Data terms of use",
        row_counts={"alerts_in_feeds": len(new), "alerts_total": len(merged)},
    )
    return new


def fetch_gtfs(cfg: Config, feeds: tuple[Feed, ...] = FEEDS, from_snapshot: Path | None = None) -> dict[str, Path]:
    """Download GTFS feeds, compute stop and stop × hour-of-week service, fetch live alerts.

    ``from_snapshot`` re-processes an existing raw snapshot directory instead of downloading.
    """
    today = datetime.now(UTC).date()
    stops_all, how_all, info = [], [], {}
    snap = None if from_snapshot else RawSnapshot(cfg.raw_dir, "gtfs")
    sess = session()
    for feed in feeds:
        if from_snapshot:
            p = from_snapshot / f"{feed.key}_gtfs.zip"
            content = read_gz(p) if p.exists() else None
        else:
            content = _download_feed(snap, sess, feed)
        if content is None:
            info[feed.key] = {"status": "unavailable"}
            continue
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            stops, how, finfo = summarize_feed(feed, z, cfg.tz, cfg["grid"]["bbox"], today)
        log.info("GTFS %s: %s", feed.key, finfo)
        info[feed.key] = finfo
        stops_all.append(stops)
        how_all.append(how)
    stops = pd.concat(stops_all, ignore_index=True)
    how = pd.concat(how_all, ignore_index=True)
    outputs = {
        "transit_stops": write_parquet(stops, cfg.processed_dir / "transit_stops.parquet"),
        "transit_stop_how": write_parquet(how, cfg.processed_dir / "transit_stop_how.parquet"),
    }
    if snap is not None:
        snap.finish(
            license={
                "kcm": "King County Metro GTFS, King County developer terms of use",
                "st": "Sound Transit Open Transit Data terms of use",
                "others": "agency terms via Puget Sound OneBusAway GTFS mirror (gtfs.sound.obaweb.org)",
            },
            feeds=info,
            outputs={k: str(v) for k, v in outputs.items()},
            row_counts={"transit_stops": len(stops), "transit_stop_how": len(how)},
        )
    try:
        fetch_gtfs_rt_alerts(cfg)
    except Exception as e:  # live alerts are best-effort
        log.warning("GTFS-rt alerts failed: %s", e)
    return outputs
