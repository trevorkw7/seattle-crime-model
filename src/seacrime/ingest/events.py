"""Scheduled crowd events in Seattle: pro / college home games, permits, holidays.

Real sources only (see docs/DATA_SOURCES_CONTEXT.md for coverage and caveats):

* **MLB Stats API** (statsapi.mlb.com) — every game at T-Mobile Park
  (venue 680: Mariners home games, postseason, the 2023 All-Star Game) with
  announced attendance and actual game duration.
* **ESPN site API** (site.api.espn.com) — team schedules for the Seahawks
  (NFL), Sounders (MLS + CONCACAF Champions Cup + Leagues Cup + U.S. Open Cup),
  Storm (WNBA), Kraken (NHL), UW Huskies football (NCAAF), Reign (NWSL + NWSL
  Challenge Cup); month scoreboards for the FIFA Club World Cup 2025 and FIFA
  World Cup 2026. Only games whose venue is in Seattle are kept (the venue
  city reported by ESPN, not the home/away flag — Storm/Reign home games in
  Everett/Tacoma are excluded, neutral-site games in Seattle are included).
* **Seattle Special Events Permits** (data.seattle.gov ``dm95-f8w5``) —
  permitted parades, runs, festivals, free-speech events (dates only).
* **Nager.Date** public-holiday API — US federal + Washington State holidays.
* Venue coordinates: MLB venue API (T-Mobile Park) and OpenStreetMap
  Nominatim (one lookup per venue).

Output: ``data/processed/events.parquet`` (one row per event). ``start_utc``
is the scheduled start; ``end_utc`` is an *estimate* (start + per-category
duration, :data:`DURATION_H`); ``schedule_known_at_utc`` is a conservative
(late) estimate of when the event's date and time were public
(:func:`schedule_known_at`). ``attendance`` for games is the announced
attendance, known only *after* the game — never use it as a forecast-time
feature; permit attendance is the applicant's advance estimate.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import Config
from ._context_common import HttpError, RawSnapshot, http_get, local_to_utc, session, to_utc, write_parquet
from .socrata import SocrataQuery, read_snapshot, snapshot

log = logging.getLogger(__name__)

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
MLB_VENUE = "https://statsapi.mlb.com/api/v1/venues/{venue_id}"
MLB_VENUE_IDS = (680,)  # T-Mobile Park
MLB_GAME_TYPES = "S,E,R,F,D,L,W,A"  # spring, exhibition, regular, WC, DS, LCS, WS, All-Star
MLB_POSTSEASON = {"F", "D", "L", "W"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
NAGER = "https://date.nager.at/api/v3/PublicHolidays/{year}/US"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
PERMITS_DATASET = "dm95-f8w5"


@dataclass(frozen=True)
class EspnTeam:
    category: str
    sport: str  # e.g. "football"
    league: str  # e.g. "nfl"
    team: str  # ESPN team id / abbreviation
    first_season: int
    season_types: tuple[int | None, ...] = (None, 1, 2, 3)
    extra_leagues: tuple[str, ...] = ()  # cup competitions under the same sport
    season_offset: int = 0  # NHL: ESPN season 2022 == 2021-22


ESPN_TEAMS: tuple[EspnTeam, ...] = (
    EspnTeam("nfl", "football", "nfl", "sea", 2019),
    EspnTeam(
        "mls",
        "soccer",
        "usa.1",
        "9726",
        2019,
        season_types=(None,),
        extra_leagues=("concacaf.champions", "concacaf.leagues.cup", "usa.open"),
    ),
    EspnTeam("wnba", "basketball", "wnba", "sea", 2019),
    EspnTeam("nhl", "hockey", "nhl", "sea", 2022, season_offset=1),
    EspnTeam("ncaaf", "football", "college-football", "264", 2019, season_types=(None, 2, 3)),
    EspnTeam("nwsl", "soccer", "usa.nwsl", "15363", 2019, season_types=(None,), extra_leagues=("usa.nwsl.cup",)),
)
# Tournaments hosted in Seattle that are not part of a Seattle team's schedule.
ESPN_SCOREBOARDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("soccer_intl", "soccer/fifa.cwc", ("202506", "202507")),
    ("soccer_intl", "soccer/fifa.world", ("202606", "202607")),
)

# Estimated event duration (scheduled start -> end) in hours, used for end_utc.
DURATION_H: dict[str, float] = {
    "mlb": 3.0,
    "nfl": 3.25,
    "ncaaf": 3.5,
    "nhl": 2.75,
    "wnba": 2.25,
    "mls": 2.0,
    "nwsl": 2.0,
    "soccer_intl": 2.0,
}
# Conservative lead time (hours) by which a *regular* fixture's date+time was public.
KNOWN_LEAD_H: dict[str, float] = {"nfl": 5 * 24, "ncaaf": 5 * 24, "holiday": 365 * 24}
DEFAULT_KNOWN_LEAD_H = 7 * 24
POSTSEASON_KNOWN_LEAD_H = 12.0
PERMIT_MIN_KNOWN_LEAD_H = 24.0

SEATTLE_VENUE_NAMES = {  # fallback only, when a feed omits the venue city
    "lumen field",
    "centurylink field",
    "t-mobile park",
    "climate pledge arena",
    "keyarena",
    "husky stadium",
    "alaska airlines arena",
    "hec edmundson pavilion",
}
GEOCODE_ALIASES = {"CenturyLink Field": "Lumen Field", "KeyArena": "Climate Pledge Arena"}
POSTSEASON_RE = re.compile(r"playoff|final|knockout|round of|quarter|semi|mls cup|wild ?card|round-of", re.I)
FIXED_DATE_HOLIDAYS = {  # observed (Friday/Monday) vs actual date for fixed-date federal holidays
    "new year": (1, 1),
    "juneteenth": (6, 19),
    "independence day": (7, 4),
    "veterans day": (11, 11),
    "christmas": (12, 25),
}

EVENT_COLUMNS = [
    "event_id",
    "name",
    "category",
    "competition",
    "season",
    "is_postseason",
    "home_team",
    "away_team",
    "venue",
    "venue_city",
    "lat",
    "lon",
    "location_text",
    "start_utc",
    "end_utc",
    "all_day",
    "time_tbd",
    "attendance",
    "attendance_kind",
    "status",
    "status_raw",
    "schedule_known_at_utc",
    "actual_end_utc",
    "source",
    "source_id",
]


# --------------------------------------------------------------------------- pure helpers


def normalize_status(raw: Any) -> str:
    s = str(raw or "").lower()
    if not s or s == "nan":
        return "unknown"
    if "postpon" in s:
        return "postponed"
    if "cancel" in s or "withdrawn" in s or "denied" in s:
        return "cancelled"
    if "suspend" in s:
        return "suspended"
    if "delay" in s:
        return "delayed"
    if any(k in s for k in ("final", "full_time", "complete", "game over")):
        return "final"
    if any(k in s for k in ("progress", "halftime", "in_play", "live")):
        return "in_progress"
    if any(k in s for k in ("scheduled", "pre-game", "warmup", "issued", "preview")):
        return "scheduled"
    if "process" in s or "hold" in s:
        return "pending"
    return "other"


def is_seattle_venue(city: Any, venue: Any) -> bool:
    c = str(city or "").lower()
    if c and c != "nan":
        return "seattle" in c
    return str(venue or "").strip().lower() in SEATTLE_VENUE_NAMES


def schedule_known_at(
    start_utc: pd.Series,
    category: pd.Series,
    is_postseason: pd.Series,
    application_utc: pd.Series | None = None,
) -> pd.Series:
    """Conservative (late) estimate of when an event's date/time became public.

    * regular-season / preseason fixtures: ``start - 7 d`` (NFL & NCAAF ``- 5 d``:
      flex scheduling and 6/12-day kickoff-time announcements);
    * postseason / knockout games (existence depends on earlier results,
      "if necessary" games): ``start - 12 h``;
    * holidays: ``start - 365 d``;
    * permits: ``max(application date, start - 24 h)``.
    """
    lead_h = category.map(KNOWN_LEAD_H).fillna(DEFAULT_KNOWN_LEAD_H).astype(float)
    lead_h = lead_h.mask(is_postseason.fillna(False).astype(bool), POSTSEASON_KNOWN_LEAD_H)
    lead_h = lead_h.mask(category.eq("permit"), PERMIT_MIN_KNOWN_LEAD_H)
    known = start_utc - pd.to_timedelta(lead_h, unit="h")
    if application_utc is not None:
        app = application_utc.where(category.eq("permit"))
        known = known.where(app.isna() | (app <= known), app)
    return known


def parse_espn_event(e: dict, category: str, competition: str) -> dict[str, Any]:
    c = (e.get("competitions") or [{}])[0]
    venue = c.get("venue") or {}
    comps = c.get("competitors") or []
    home = next((x for x in comps if x.get("homeAway") == "home"), {})
    away = next((x for x in comps if x.get("homeAway") == "away"), {})
    stype = e.get("seasonType") or (e.get("season") or {})
    status = ((c.get("status") or e.get("status") or {}).get("type")) or {}
    stype_name = str(stype.get("name") or stype.get("slug") or "")
    is_post = bool(stype.get("type") == 3 and category not in ("mls", "nwsl", "soccer_intl")) or bool(POSTSEASON_RE.search(stype_name))
    return {
        "source_id": str(e.get("id")),
        "name": e.get("name"),
        "start": e.get("date"),
        "time_tbd": not bool(e.get("timeValid", c.get("timeValid", True))),
        "venue": venue.get("fullName"),
        "venue_city": (venue.get("address") or {}).get("city"),
        "neutral_site": c.get("neutralSite"),
        "home_team": (home.get("team") or {}).get("displayName"),
        "away_team": (away.get("team") or {}).get("displayName"),
        "attendance": c.get("attendance"),
        "status_raw": status.get("name") or status.get("description"),
        "season": (e.get("season") or {}).get("year"),
        "season_type": stype_name,
        "is_postseason": is_post,
        "category": category,
        "competition": competition,
    }


def parse_mlb_games(payload: dict) -> list[dict[str, Any]]:
    rows = []
    for d in payload.get("dates", []):
        for g in d.get("games", []):
            info = g.get("gameInfo") or {}
            first = info.get("firstPitch")
            dur = (info.get("gameDurationMinutes") or 0) + (info.get("delayDurationMinutes") or 0)
            rows.append(
                {
                    "source_id": str(g["gamePk"]),
                    "name": f"{g['teams']['away']['team']['name']} at {g['teams']['home']['team']['name']}",
                    "start": g.get("gameDate"),
                    "time_tbd": bool((g.get("status") or {}).get("startTimeTBD")),
                    "venue": (g.get("venue") or {}).get("name"),
                    "venue_id": (g.get("venue") or {}).get("id"),
                    "venue_city": "Seattle",  # filtered by venue id
                    "home_team": g["teams"]["home"]["team"]["name"],
                    "away_team": g["teams"]["away"]["team"]["name"],
                    "attendance": info.get("attendance"),
                    "status_raw": (g.get("status") or {}).get("detailedState"),
                    "season": int(g.get("season")) if g.get("season") else None,
                    "season_type": g.get("gameType"),
                    "is_postseason": g.get("gameType") in MLB_POSTSEASON,
                    "category": "mlb",
                    "competition": g.get("seriesDescription") or g.get("gameType"),
                    "actual_end": (
                        (pd.Timestamp(first) + pd.Timedelta(minutes=dur)).isoformat() if first and info.get("gameDurationMinutes") else None
                    ),
                }
            )
    return rows


def games_to_events(rows: list[dict[str, Any]], source: str, venue_coords: dict[str, tuple[float, float]]) -> pd.DataFrame:
    """Raw game dicts -> events schema (Seattle venues only, dedup by source id)."""
    if not rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    g = pd.DataFrame(rows).drop_duplicates("source_id", keep="last")
    g = g[[is_seattle_venue(c, v) for c, v in zip(g["venue_city"], g["venue"], strict=True)]].copy()
    g["start_utc"] = to_utc(g["start"])
    g["end_utc"] = g["start_utc"] + pd.to_timedelta(g["category"].map(DURATION_H), unit="h")
    g["status"] = g["status_raw"].map(normalize_status)
    att = pd.to_numeric(g["attendance"], errors="coerce").astype("float64")
    g["attendance"] = att.where(g["status"].eq("final"))  # future games report 0
    g["attendance_kind"] = np.where(g["attendance"].notna(), "announced_post_event", None)
    g["actual_end_utc"] = to_utc(g["actual_end"]) if "actual_end" in g else pd.NaT
    g["event_id"] = source + ":" + g["source_id"]
    g["source"] = source
    g["all_day"] = False
    g["location_text"] = g["venue"]
    lat = {k: v[0] for k, v in venue_coords.items()}
    lon = {k: v[1] for k, v in venue_coords.items()}
    g["lat"] = g["venue"].map(lat)
    g["lon"] = g["venue"].map(lon)
    return g.reindex(columns=EVENT_COLUMNS)


def permits_to_events(p: pd.DataFrame, tz: str, special_events_only: bool = True) -> pd.DataFrame:
    """Seattle Special Events Permits -> events schema (all-day local dates)."""
    p = p.copy()
    if special_events_only and "permit_type" in p:
        p = p[p["permit_type"].fillna("").str.strip().eq("Special Event")]
    start_local = pd.to_datetime(p["event_start_date"], errors="coerce")
    end_local = pd.to_datetime(p["event_end_date"], errors="coerce").fillna(start_local) + pd.Timedelta(days=1)
    app_local = pd.to_datetime(p.get("application_date"), errors="coerce")
    out = pd.DataFrame(
        {
            "event_id": "permit:" + p["year_month_app"].astype(str),
            "name": p["name_of_event"],
            "category": "permit",
            "competition": p.get("event_category"),
            "season": start_local.dt.year,
            "is_postseason": False,
            "venue": p.get("event_location_park"),
            "location_text": (
                p.get("event_location_park", pd.Series(index=p.index, dtype=str)).fillna("")
                + " | "
                + p.get("event_location_neighborhood", pd.Series(index=p.index, dtype=str)).fillna("")
            ).str.strip(" |"),
            "venue_city": "Seattle",
            "start_utc": local_to_utc(start_local, tz),
            "end_utc": local_to_utc(end_local, tz),
            "all_day": True,
            "time_tbd": True,
            "attendance": pd.to_numeric(p.get("attendance"), errors="coerce").astype("float64"),
            "attendance_kind": "applicant_estimate",
            "status_raw": p.get("permit_status"),
            "source": "seattle_special_events_permits",
            "source_id": p["year_month_app"].astype(str),
            "_application_utc": local_to_utc(app_local, tz),
            "_neighborhoods": p.get("event_location_neighborhood"),
        }
    )
    out["status"] = out["status_raw"].map(normalize_status)
    out = out.dropna(subset=["start_utc"])
    dup = out["event_id"].duplicated(keep=False)
    out.loc[dup, "event_id"] = out.loc[dup, "event_id"] + "#" + out[dup].groupby("event_id").cumcount().astype(str)
    return out


def holidays_to_events(payloads: dict[int, list[dict]], tz: str, state: str = "US-WA") -> pd.DataFrame:
    """Nager.Date responses -> federal + state public holidays (observed + actual dates)."""
    rows = []
    for _, items in sorted(payloads.items()):
        for h in items:
            counties = h.get("counties") or []
            if "Public" not in (h.get("types") or []) or not (h.get("global") or state in counties):
                continue
            d = pd.Timestamp(h["date"])
            rows.append((d, h["name"], True))
            key = next((k for k in FIXED_DATE_HOLIDAYS if k in h["name"].lower()), None)
            if key:
                m, dd = FIXED_DATE_HOLIDAYS[key]
                cands = [pd.Timestamp(year=y, month=m, day=dd) for y in (d.year - 1, d.year, d.year + 1)]
                actual = min(cands, key=lambda c: abs((c - d).days))
                if actual != d and abs((actual - d).days) <= 3:
                    rows.append((actual, f"{h['name']} (actual date)", False))
    if not rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    df = pd.DataFrame(rows, columns=["date", "name", "observed"]).drop_duplicates(["date", "name"])
    out = pd.DataFrame(
        {
            "event_id": "holiday:" + df["date"].dt.strftime("%Y-%m-%d") + ":" + df["name"],
            "name": df["name"],
            "category": "holiday",
            "competition": np.where(df["observed"], "observed", "actual_date"),
            "season": df["date"].dt.year,
            "is_postseason": False,
            "venue_city": "Seattle",
            "start_utc": local_to_utc(df["date"], tz),
            "end_utc": local_to_utc(df["date"] + pd.Timedelta(days=1), tz),
            "all_day": True,
            "time_tbd": False,
            "status": "scheduled",
            "status_raw": None,
            "source": "nager_date",
            "source_id": df["date"].dt.strftime("%Y-%m-%d"),
        }
    )
    return out.reindex(columns=EVENT_COLUMNS)


def assign_neighborhood_centroids(events: pd.DataFrame, hoods: pd.DataFrame | None) -> pd.DataFrame:
    """Give single-neighborhood permits the centroid of that neighborhood polygon."""
    if hoods is None or hoods.empty or "_neighborhoods" not in events:
        return events
    lut: dict[str, tuple[float, float]] = {}
    for _, r in hoods.iterrows():
        for name in (r.get("s_hood"), r.get("l_hood")):
            if isinstance(name, str) and name.strip():
                lut.setdefault(_norm(name), (r["centroid_lat"], r["centroid_lon"]))

    def lookup(text: Any) -> tuple[float, float] | None:
        if not isinstance(text, str) or ";" in text or not text.strip():
            return None
        parts = [_norm(x) for x in re.split(r"\s*/\s*", text)]
        for k in [_norm(text), *parts]:
            if k in lut:
                return lut[k]
        return None

    hits = events["_neighborhoods"].map(lookup)
    m = hits.notna() & events["lat"].isna()
    events.loc[m, "lat"] = [h[0] for h in hits[m]]
    events.loc[m, "lon"] = [h[1] for h in hits[m]]
    return events


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# --------------------------------------------------------------------------- fetchers


def _seasons(first: int, offset: int = 0) -> list[int]:
    return list(range(first, datetime.now(UTC).year + 1 + offset))


def _fetch_mlb(snap: RawSnapshot, sess: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for season in _seasons(2019):
        params = {
            "sportId": 1,
            "venueIds": ",".join(map(str, MLB_VENUE_IDS)),
            "season": season,
            "gameType": MLB_GAME_TYPES,
            "hydrate": "gameInfo",
        }
        r = http_get(sess, MLB_SCHEDULE, params)
        got = parse_mlb_games(r.json())
        snap.save(f"mlb/schedule_{season}.json", r.content, MLB_SCHEDULE, params, rows=len(got))
        rows += got
    return rows


def _fetch_espn(snap: RawSnapshot, sess: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in ESPN_TEAMS:
        leagues = [(t.league, t.season_types)] + [(lg, (None,)) for lg in t.extra_leagues]
        for league, stypes in leagues:
            for season in _seasons(t.first_season, t.season_offset):
                for st in stypes:
                    url = f"{ESPN}/{t.sport}/{league}/teams/{t.team}/schedule"
                    params: dict[str, Any] = {"season": season}
                    if st is not None:
                        params["seasontype"] = st
                    try:
                        r = http_get(sess, url, params, retries=2)
                    except HttpError as e:
                        if e.status == 400:  # team not in this competition/season
                            continue
                        snap.blocked(f"espn {league} {season} {st}", url, e.status, e.detail)
                        continue
                    events = r.json().get("events", []) or []
                    comp = t.league if league == t.league else league
                    got = [parse_espn_event(e, t.category, comp) for e in events]
                    snap.save(f"espn/{league}_{t.team}_{season}_{st}.json", r.content, url, params, rows=len(got))
                    rows += got
                    time.sleep(0.25)
    for category, path, months in ESPN_SCOREBOARDS:
        for month in months:
            url = f"{ESPN}/{path}/scoreboard"
            params = {"dates": month, "limit": 1000}
            try:
                r = http_get(sess, url, params, retries=2)
            except HttpError as e:
                snap.blocked(f"espn scoreboard {path} {month}", url, e.status, e.detail)
                continue
            events = r.json().get("events", []) or []
            got = [parse_espn_event(e, category, path.split("/")[1]) for e in events]
            snap.save(f"espn/scoreboard_{path.replace('/', '_')}_{month}.json", r.content, url, params, rows=len(got))
            rows += got
    return rows


def _geocode_venues(snap: RawSnapshot, sess: Any, venues: Iterable[str], bbox: list[float]) -> dict[str, tuple[float, float]]:
    coords: dict[str, tuple[float, float]] = {}
    for vid in MLB_VENUE_IDS:
        url = MLB_VENUE.format(venue_id=vid)
        r = http_get(sess, url, {"hydrate": "location"})
        snap.save(f"geocode/mlb_venue_{vid}.json", r.content, url, {"hydrate": "location"})
        v = r.json()["venues"][0]
        c = v["location"]["defaultCoordinates"]
        coords[v["name"]] = (float(c["latitude"]), float(c["longitude"]))
    w, s, e, n = bbox
    for name in sorted({v for v in venues if isinstance(v, str)} - set(coords)):
        q = f"{GEOCODE_ALIASES.get(name, name)}, Seattle, Washington"
        params = {"q": q, "format": "jsonv2", "limit": 1, "viewbox": f"{w},{n},{e},{s}", "bounded": 1}
        try:
            r = http_get(sess, NOMINATIM, params, retries=2)
        except HttpError as err:
            snap.blocked(f"nominatim {name}", NOMINATIM, err.status, err.detail)
            continue
        snap.save(f"geocode/nominatim_{_norm(name).replace(' ', '_')}.json", r.content, NOMINATIM, params)
        hits = r.json()
        if hits:
            coords[name] = (float(hits[0]["lat"]), float(hits[0]["lon"]))
        time.sleep(1.1)  # Nominatim usage policy: <= 1 request / second
    return coords


def _fetch_holidays(snap: RawSnapshot, sess: Any) -> dict[int, list[dict]]:
    out = {}
    for y in _seasons(2019, 1):
        url = NAGER.format(year=y)
        r = http_get(sess, url)
        snap.save(f"holidays/nager_us_{y}.json", r.content, url)
        out[y] = r.json()
    return out


def fetch_events(cfg: Config) -> Path:
    """Download all event sources and write ``data/processed/events.parquet``."""
    snap = RawSnapshot(cfg.raw_dir, "events")
    sess = session()
    snap.meta["license"] = {
        "mlb_statsapi": "MLB Advanced Media; use subject to http://gdx.mlb.com/components/copyright.txt (non-commercial research use)",
        "espn": "Undocumented public ESPN site API; no published licence — research use, do not redistribute raw data",
        "seattle_permits": "City of Seattle open data, Public Domain",
        "nager_date": "Nager.Date API, MIT-licensed project; data from public sources",
        "nominatim": "OpenStreetMap data (c) OSM contributors, ODbL 1.0",
    }
    mlb_rows = _fetch_mlb(snap, sess)
    espn_rows = _fetch_espn(snap, sess)
    venues = {r["venue"] for r in mlb_rows + espn_rows if is_seattle_venue(r.get("venue_city"), r.get("venue"))}
    coords = _geocode_venues(snap, sess, venues, cfg["grid"]["bbox"])
    games = pd.concat(
        [games_to_events(mlb_rows, "mlb_statsapi", coords), games_to_events(espn_rows, "espn", coords)],
        ignore_index=True,
    )

    permit_snap = snapshot("special_events_permits", SocrataQuery(PERMITS_DATASET, order=":id"), cfg.raw_dir)
    permits = permits_to_events(read_snapshot(permit_snap), cfg.tz)
    hoods_path = cfg.processed_dir / "neighborhoods.parquet"
    hoods = pd.read_parquet(hoods_path) if hoods_path.exists() else None
    permits["lat"], permits["lon"] = np.nan, np.nan
    permits = assign_neighborhood_centroids(permits, hoods)
    permits = permits.reindex(columns=[*EVENT_COLUMNS, "_application_utc"])

    holidays = holidays_to_events(_fetch_holidays(snap, sess), cfg.tz)

    events = pd.concat([games, permits, holidays], ignore_index=True)
    for c in ("start_utc", "end_utc", "actual_end_utc", "_application_utc"):
        events[c] = pd.to_datetime(events[c], utc=True).astype("datetime64[ns, UTC]")
    events["is_postseason"] = events["is_postseason"].fillna(False).astype(bool)
    events["schedule_known_at_utc"] = schedule_known_at(
        events["start_utc"], events["category"], events["is_postseason"], events.pop("_application_utc")
    )
    events["fetched_at_utc"] = pd.Timestamp(snap.fetched_at)
    events = events.drop_duplicates("event_id").sort_values("start_utc").reset_index(drop=True)
    for c in ("time_tbd", "all_day"):
        events[c] = events[c].fillna(False).astype(bool)
    events["season"] = pd.to_numeric(events["season"], errors="coerce").astype("Int64")

    out = write_parquet(events, cfg.processed_dir / "events.parquet")
    by_cat = events.groupby("category").agg(n=("event_id", "size"), first=("start_utc", "min"), last=("start_utc", "max"))
    snap.finish(
        outputs={"events": str(out)},
        permits_snapshot=str(permit_snap),
        venue_coords={k: list(v) for k, v in coords.items()},
        row_counts=json.loads(by_cat.to_json(date_format="iso")),
    )
    log.info("events: %d rows\n%s", len(events), by_cat)
    return out
