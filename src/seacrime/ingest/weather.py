"""Seattle weather: hourly observations, archived point forecasts, live NWS forecast.

Three products, all real downloads:

1. **Observations** — NOAA NCEI hourly surface observations for
   Seattle-Tacoma Intl (KSEA; primary) and Boeing Field / King County Intl
   (KBFI; gap filler):

   * ``global-hourly`` (Integrated Surface Database, ISD) via the NCEI Access
     Data Service, one station-year per request. ISD stops at
     2025-08-27 07:00 UTC for both stations (NCEI is replacing ISD with GHCNh);
   * ``GHCNh`` (Global Historical Climatology Network – hourly) per-station
     yearly PSV files for the hours after the ISD record ends. The two
     products overlap for 2025-01-01..2025-08-27 (values identical there).
     After the cutoff GHCNh omits ``precipitation`` when the METAR carried no
     precipitation group (METAR convention: group omitted when none fell), so
     routine hours without a value are zero-filled (``precip_zero_filled``) and
     monthly totals are validated against GHCN-Daily PRCP (manifest).

   -> ``data/processed/weather_hourly.parquet``

2. **Archived forecasts (hindsight-free)** — NWS Model Output Statistics point
   forecasts for KSEA as archived by the Iowa Environmental Mesonet (IEM):
   GFS MOS ("MAV": runs 00/06/12/18Z, 3-hourly to 72 h) and GFS LAMP ("LAV":
   hourly steps to 25–38 h; IEM keeps the 00/06/12/18Z runs from 2020-07 on and
   hourly runs only for the most recent days). Each row carries the model run
   time and a conservative ``issued_at_utc`` (run + assumed publication delay)
   so a backtest only uses forecasts that existed at forecast-issue time.
   -> ``data/processed/weather_forecast_hourly.parquet``

   Open-Meteo's historical-forecast / previous-runs APIs are probed once and
   their HTTP status recorded in the manifest (HTTP 429 "Daily API request
   limit exceeded" from this egress IP earlier on 2026-10-14, 200 later).
   They are not ingested: values are not tagged with a model run/issue time.

3. **Live forecast** — :func:`fetch_nws_forecast` returns the current
   api.weather.gov hourly forecast for the SEW grid point covering downtown
   Seattle (temperature, PoP, humidity, wind) plus gridded QPF.

Timestamps: ISD/GHCNh ``DATE`` and MOS ``runtime``/``ftime`` are UTC; NWS
times carry explicit offsets. Everything is emitted tz-aware UTC; ``ts_utc``
is the start of the hour.
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import Config
from ._context_common import HttpError, RawSnapshot, http_get, session, to_utc, write_parquet

log = logging.getLogger(__name__)

NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
STATIONS = {"72793024233": "KSEA", "72793524234": "KBFI"}  # ISD ids, primary first
ISD_DATA_TYPES = "TMP,DEW,WND,AA1,AA2,AA3,AA4"
ISD_BAD_QUALITY = {"2", "3", "6", "7"}  # suspect / erroneous per ISD format doc

GHCNH_URL = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/access/by-year/{year}/psv/GHCNh_{station}_{year}.psv"
GHCNH_STATIONS = {"USW00024233": "KSEA", "USW00024234": "KBFI"}
GHCN_DAILY_DATASET = "daily-summaries"
GHCNH_COLS = [
    "DATE",
    "temperature",
    "temperature_Quality_Code",
    "temperature_Report_Type",
    "dew_point_temperature",
    "dew_point_temperature_Quality_Code",
    "wind_speed",
    "wind_speed_Quality_Code",
    "precipitation",
    "precipitation_Quality_Code",
    "precipitation_Report_Type",
]

IEM_MOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
IEM_PAUSE_S = 2.0  # IEM answers bursts with HTTP 429
MOS_STATION = "KSEA"
# model -> (first date to request, chunk frequency, assumed publication delay after run time).
# Delays are conservative assumptions (see docs/DATA_SOURCES_CONTEXT.md): on 2026-10-14
# at 19:14Z the 18Z LAMP run was already in the IEM archive while the 18Z GFS MOS was not.
MOS_MODELS: dict[str, tuple[str, str, float]] = {
    "GFS": ("2019-06-01", "QS", 6.0),
    "LAV": ("2020-07-01", "MS", 1.5),
}

OPEN_METEO_PROBES = {
    "historical_forecast": "https://historical-forecast-api.open-meteo.com/v1/forecast",
    "previous_runs": "https://previous-runs-api.open-meteo.com/v1/forecast",
}

NWS_POINT = (47.6062, -122.3321)  # downtown Seattle
NWS_HEADERS = {"Accept": "application/geo+json"}

KNOT_TO_MS = 0.514444
KMH_TO_MS = 1 / 3.6
MPH_TO_MS = 0.44704
OBS_COLS = ["temp_c", "dewpoint_c", "rel_humidity", "wind_ms", "precip_mm"]


# --------------------------------------------------------------------------- parsing (pure)


def _isd_split(s: pd.Series, n: int) -> pd.DataFrame:
    parts = s.fillna("").astype(str).str.split(",", expand=True)
    for i in range(parts.shape[1], n):
        parts[i] = None
    return parts.iloc[:, :n]


def parse_isd_scaled(s: pd.Series, missing: int = 9999, scale: float = 10.0) -> pd.Series:
    """Parse an ISD ``value,quality`` field such as TMP ``+0078,1`` -> 7.8."""
    p = _isd_split(s, 2)
    v = pd.to_numeric(p[0], errors="coerce")
    bad = v.abs().eq(missing) | p[1].isin(ISD_BAD_QUALITY)
    return (v / scale).mask(bad).astype("float64")


def parse_isd_wind_speed(s: pd.Series) -> pd.Series:
    """WND ``dir,dirQ,type,speed,speedQ`` -> speed in m/s (calm => 0)."""
    p = _isd_split(s, 5)
    v = pd.to_numeric(p[3], errors="coerce")
    bad = v.eq(9999) | p[4].isin(ISD_BAD_QUALITY)
    out = (v / 10.0).mask(bad)
    return out.mask(p[2].eq("C"), 0.0).astype("float64")


def parse_isd_precip(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """AAn ``period_h,depth(0.1mm),condition,quality`` -> (period_h, depth_mm)."""
    p = _isd_split(s, 4)
    period = pd.to_numeric(p[0], errors="coerce").mask(lambda x: x.eq(99))
    depth = pd.to_numeric(p[1], errors="coerce")
    bad = depth.eq(9999) | p[3].isin(ISD_BAD_QUALITY)
    return period.astype("float64"), (depth / 10.0).mask(bad).astype("float64")


def rel_humidity(temp_c: pd.Series, dew_c: pd.Series) -> pd.Series:
    """Relative humidity (%) from temperature and dew point (Magnus, Alduchov & Eskridge 1996)."""
    a, b = 17.625, 243.04
    rh = 100 * np.exp(a * dew_c / (b + dew_c)) / np.exp(a * temp_c / (b + temp_c))
    return rh.clip(upper=100.0)


def _to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Shared hourly collapse for report-level frames with columns
    ts, report_type, temp_c, dewpoint_c, wind_ms, p1h."""
    df = df.dropna(subset=["ts"]).copy()
    df["ts_utc"] = df["ts"].dt.floor("h")
    vars_ = ["temp_c", "dewpoint_c", "wind_ms"]
    routine_rows = df["report_type"].eq("FM-15")
    routine = df[routine_rows].sort_values("ts").groupby("ts_utc")[vars_].last()
    other = df[~routine_rows].groupby("ts_utc")[vars_].mean()
    out = routine.combine_first(other)
    out["precip_mm"] = df[routine_rows].groupby("ts_utc")["p1h"].max()
    out["has_routine"] = df[routine_rows].groupby("ts_utc").size().gt(0)
    out["has_routine"] = out["has_routine"].astype("boolean").fillna(False).astype(bool)
    out["n_reports"] = df.groupby("ts_utc").size()
    out = out.reset_index()
    out["rel_humidity"] = rel_humidity(out["temp_c"], out["dewpoint_c"])
    return out


def isd_to_hourly(raw: pd.DataFrame) -> pd.DataFrame:
    """Collapse one station's ISD reports to one row per UTC hour.

    * temperature / dew point / wind: the routine hourly METAR (FM-15) in the
      hour if present, else the mean of the other report types (SYNOP FM-12,
      SPECI FM-16, ...).
    * precip_mm: the 1-hour accumulation (AAn with period 01) reported by the
      FM-15 METAR; hours without one stay missing (NaN).
    * A METAR taken at HH:53 is assigned to hour HH (it summarises ~HH-1:53..HH:53).
    """
    precip = pd.Series(np.nan, index=raw.index)
    for col in ("AA1", "AA2", "AA3", "AA4"):
        if col in raw:
            period, depth = parse_isd_precip(raw[col])
            precip = precip.fillna(depth.where(period.eq(1)))
    df = pd.DataFrame(
        {
            "ts": to_utc(raw["DATE"]),
            "report_type": raw["REPORT_TYPE"].astype(str).str.strip(),
            "temp_c": parse_isd_scaled(raw["TMP"]),
            "dewpoint_c": parse_isd_scaled(raw["DEW"]),
            "wind_ms": parse_isd_wind_speed(raw["WND"]),
            "p1h": precip,
        }
    )
    return _to_hourly(df)


def _ghcnh_value(raw: pd.DataFrame, col: str) -> pd.Series:
    v = pd.to_numeric(raw[col], errors="coerce")
    q = raw.get(f"{col}_Quality_Code", pd.Series(index=raw.index, dtype=object)).fillna("").astype(str).str.strip()
    bad = q.isin(ISD_BAD_QUALITY) | q.str.fullmatch(r"[A-Za-z]")  # letters = failed GHCNh QC check
    return v.mask(bad).astype("float64")


def ghcnh_to_hourly(raw: pd.DataFrame) -> pd.DataFrame:
    """GHCNh PSV (one station) -> hourly frame with the same semantics as :func:`isd_to_hourly`.

    GHCNh only carries ``precipitation`` when the METAR reported a precipitation
    group, so hours with a routine METAR and no value are NaN here; the caller
    may zero-fill them (see :func:`zero_fill_routine_precip`).
    """
    rt = raw["temperature_Report_Type"].fillna("").astype(str).str.strip()
    df = pd.DataFrame(
        {
            "ts": to_utc(raw["DATE"]),
            "report_type": rt.str.replace(r"^FM(\d+)$", r"FM-\1", regex=True),
            "temp_c": _ghcnh_value(raw, "temperature"),
            "dewpoint_c": _ghcnh_value(raw, "dew_point_temperature"),
            "wind_ms": _ghcnh_value(raw, "wind_speed"),
            # only the METAR's own 1-h amount; other merged report types (e.g. FM94_1 in
            # KSEA 2026) carry different accumulations and inflate totals ~2-5x
            "p1h": _ghcnh_value(raw, "precipitation").where(raw["precipitation_Report_Type"].fillna("").str.strip().eq("FM15")),
        }
    )
    return _to_hourly(df)


def zero_fill_routine_precip(hourly: pd.DataFrame) -> pd.DataFrame:
    """Routine-METAR hours with no precipitation value -> 0.0 (flagged ``precip_zero_filled``)."""
    out = hourly.copy()
    fill = out["has_routine"] & out["precip_mm"].isna()
    out.loc[fill, "precip_mm"] = 0.0
    out["precip_zero_filled"] = fill
    return out


def validate_monthly_precip(hourly: pd.DataFrame, daily: pd.DataFrame, tz: str = "America/Los_Angeles") -> dict[str, Any]:
    """Compare monthly sums of hourly precip with GHCN-Daily PRCP (local-standard-time days).

    Only complete months present in both are compared; returns totals and the
    worst monthly relative difference.
    """
    h = hourly.dropna(subset=["precip_mm"]).copy()
    h["month"] = (h["ts_utc"] - pd.Timedelta(hours=8)).dt.strftime("%Y-%m")  # LST = UTC-8 all year
    hm = h.groupby("month").agg(hourly_mm=("precip_mm", "sum"), hours=("precip_mm", "size"))
    d = daily.copy()
    d["month"] = pd.to_datetime(d["DATE"]).dt.strftime("%Y-%m")
    dm = d.groupby("month").agg(daily_mm=("PRCP", "sum"), days=("PRCP", "size"))
    m = hm.join(dm, how="inner")
    days_in = pd.to_datetime(m.index + "-01").days_in_month
    m = m[(m["days"] >= days_in - 1) & (m["hours"] >= 24 * (days_in - 1))]
    if m.empty:
        return {"months_compared": 0}
    rel = (m["hourly_mm"] - m["daily_mm"]).abs() / m["daily_mm"].clip(lower=5.0)
    return {
        "months_compared": int(len(m)),
        "hourly_total_mm": round(float(m["hourly_mm"].sum()), 1),
        "ghcn_daily_total_mm": round(float(m["daily_mm"].sum()), 1),
        "max_monthly_rel_diff": round(float(rel.max()), 3),
        "monthly": {k: [round(a, 1), round(b, 1)] for k, a, b in zip(m.index, m["hourly_mm"], m["daily_mm"], strict=True)},
    }


def combine_stations(per_station: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Citywide hourly series: earlier keys win, later keys fill gaps column by column."""
    idx = pd.date_range(start, end, freq="h", tz="UTC", name="ts_utc")
    out = pd.DataFrame(index=idx, columns=OBS_COLS, dtype="float64")
    src = pd.DataFrame(index=idx, columns=OBS_COLS, dtype="object")
    for name, df in per_station.items():
        d = df.set_index("ts_utc").reindex(idx)
        for c in OBS_COLS:
            fill = out[c].isna() & d[c].notna()
            out.loc[fill, c] = d.loc[fill, c]
            src.loc[fill, c] = name
    used = src.apply(lambda r: "+".join(sorted({v for v in r if isinstance(v, str)})), axis=1)
    out["source"] = ("noaa_ncei:" + used).where(used.ne(""), None)
    out["temp_source"] = src["temp_c"]
    out["precip_source"] = src["precip_mm"]
    zf = pd.Series(False, index=idx)
    for name, df in per_station.items():
        if "precip_zero_filled" in df:
            flag = df.set_index("ts_utc")["precip_zero_filled"].reindex(idx).fillna(False).astype(bool)
            zf |= flag & src["precip_mm"].eq(name)
    out["precip_zero_filled"] = zf
    return out.reset_index()


def parse_mos_csv(text: str, model: str, delay_h: float) -> pd.DataFrame:
    """IEM MOS CSV -> tidy forecast rows (UTC; °F->°C, knots->m/s)."""
    m = pd.read_csv(io.StringIO(text), dtype=str)
    if m.empty:
        return pd.DataFrame()
    run = to_utc(m["runtime"])
    valid = to_utc(m["ftime"])

    def num(col: str) -> pd.Series:
        return pd.to_numeric(m[col], errors="coerce") if col in m else pd.Series(np.nan, index=m.index)

    temp_c = (num("tmp") - 32) * 5 / 9
    dew_c = (num("dpt") - 32) * 5 / 9
    out = pd.DataFrame(
        {
            "source": f"iem_mos:{model}",
            "model": model,
            "station": MOS_STATION,
            "run_utc": run,
            "issued_at_utc": run + pd.Timedelta(hours=delay_h),
            "valid_ts_utc": valid,
            "lead_h": (valid - run).dt.total_seconds() / 3600,
            "temp_c": temp_c.round(2),
            "dewpoint_c": dew_c.round(2),
            "rel_humidity": rel_humidity(temp_c, dew_c).round(1),
            "wind_ms": (num("wsp") * KNOT_TO_MS).round(2),
            "wind_dir_deg": num("wdr"),
            "cloud_cover": m["cld"].str.strip() if "cld" in m else None,
            "pop_6h_pct": num("p06"),
            "pop_12h_pct": num("p12"),
            "qpf_6h_cat": num("q06"),
            "ppo_pct": num("ppo"),
            "precip_type": m["typ"].str.strip() if "typ" in m else None,
        }
    )
    return out.dropna(subset=["run_utc", "valid_ts_utc"])


def _num_unit(text: Any) -> float:
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(text or ""))
    return float(m.group(1)) if m else np.nan


def parse_nws_hourly(hourly: dict, grid: dict | None, fetched_at: datetime) -> pd.DataFrame:
    """api.weather.gov hourly forecast (+ optional gridpoint QPF) -> tidy UTC frame."""
    props = hourly["properties"]
    rows = []
    for p in props["periods"]:
        t = p.get("temperature")
        unit = p.get("temperatureUnit", "C")
        temp_c = (t - 32) * 5 / 9 if unit == "F" and t is not None else t
        ws = str(p.get("windSpeed") or "")
        factor = KMH_TO_MS if "km/h" in ws else MPH_TO_MS if "mph" in ws else np.nan
        rows.append(
            {
                "valid_ts_utc": p["startTime"],
                "temp_c": temp_c,
                "dewpoint_c": (p.get("dewpoint") or {}).get("value"),
                "rel_humidity": (p.get("relativeHumidity") or {}).get("value"),
                "precip_prob": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                "wind_ms": _num_unit(ws) * factor,
                "wind_dir": p.get("windDirection"),
                "is_daytime": p.get("isDaytime"),
                "short_forecast": p.get("shortForecast"),
            }
        )
    df = pd.DataFrame(rows)
    df["valid_ts_utc"] = to_utc(df["valid_ts_utc"])
    for c in ("temp_c", "dewpoint_c", "rel_humidity", "precip_prob", "wind_ms"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df["qpf_mm"] = np.nan
    if grid is not None:
        qpf = (grid.get("properties", {}).get("quantitativePrecipitation") or {}).get("values", [])
        hourly_qpf: dict[pd.Timestamp, float] = {}
        for v in qpf:
            start_s, dur = v["validTime"].split("/")
            start = pd.Timestamp(start_s).tz_convert("UTC")
            hours = max(1, int(pd.Timedelta(dur).total_seconds() // 3600))
            for h in range(hours):  # spread the period total evenly over its hours
                hourly_qpf[start + pd.Timedelta(hours=h)] = (v["value"] or 0.0) / hours
        df["qpf_mm"] = df["valid_ts_utc"].map(hourly_qpf).astype("float64")
    df["issued_at_utc"] = pd.Timestamp(props.get("updateTime")).tz_convert("UTC")
    df["generated_at_utc"] = pd.Timestamp(props.get("generatedAt")).tz_convert("UTC")
    df["fetched_at_utc"] = pd.Timestamp(fetched_at)
    df["lead_h"] = (df["valid_ts_utc"] - df["fetched_at_utc"]).dt.total_seconds() / 3600
    df["source"] = "nws_api_hourly"
    return df


# --------------------------------------------------------------------------- fetchers


def _year_ranges(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[str, str]]:
    out = []
    for y in range(start.year, end.year + 1):
        a = max(start, pd.Timestamp(f"{y}-01-01", tz="UTC"))
        b = min(end, pd.Timestamp(f"{y}-12-31", tz="UTC"))
        out.append((a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")))
    return out


def _fetch_isd(snap: RawSnapshot, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.DataFrame]:
    sess = session()
    out: dict[str, pd.DataFrame] = {}
    for station, call in STATIONS.items():
        frames = []
        for a, b in _year_ranges(start, end):
            params = {
                "dataset": "global-hourly",
                "stations": station,
                "startDate": a,
                "endDate": b,
                "dataTypes": ISD_DATA_TYPES,
                "format": "csv",
            }
            try:
                r = http_get(sess, NCEI_URL, params, timeout=600)
            except HttpError as e:
                snap.blocked(f"ncei isd {station} {a}", NCEI_URL, e.status, e.detail)
                continue
            raw = pd.read_csv(io.StringIO(r.text), dtype=str)
            snap.save(f"ncei_isd_{station}_{a[:4]}.csv", r.content, NCEI_URL, params, rows=len(raw))
            if len(raw):
                frames.append(raw)
            log.info("NCEI ISD %s %s..%s: %d reports", call, a, b, len(raw))
        if frames:
            out[call] = isd_to_hourly(pd.concat(frames, ignore_index=True))
    return out


def _fetch_ghcnh(snap: RawSnapshot, years: list[int]) -> dict[str, pd.DataFrame]:
    sess = session()
    out: dict[str, pd.DataFrame] = {}
    for station, call in GHCNH_STATIONS.items():
        frames = []
        for y in years:
            url = GHCNH_URL.format(year=y, station=station)
            try:
                r = http_get(sess, url, timeout=900)
            except HttpError as e:
                snap.blocked(f"ncei ghcnh {station} {y}", url, e.status, e.detail)
                continue
            raw = pd.read_csv(io.BytesIO(r.content), sep="|", dtype=str, usecols=lambda c: c in GHCNH_COLS)
            snap.save(f"ncei_ghcnh_{station}_{y}.psv", r.content, url, rows=len(raw))
            frames.append(raw)
            log.info("NCEI GHCNh %s %d: %d reports", call, y, len(raw))
        if frames:
            out[call] = ghcnh_to_hourly(pd.concat(frames, ignore_index=True))
    return out


def _fetch_ghcn_daily(snap: RawSnapshot, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """GHCN-Daily PRCP (mm) for both stations — independent check of hourly precip."""
    params = {
        "dataset": GHCN_DAILY_DATASET,
        "stations": ",".join(GHCNH_STATIONS),
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "dataTypes": "PRCP",
        "format": "csv",
        "units": "metric",
    }
    try:
        r = http_get(session(), NCEI_URL, params, timeout=300)
    except HttpError as e:
        snap.blocked("ncei ghcn-daily", NCEI_URL, e.status, e.detail)
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO(r.text), dtype={"STATION": str, "DATE": str})
    snap.save("ncei_ghcn_daily_prcp.csv", r.content, NCEI_URL, params, rows=len(df))
    df["PRCP"] = pd.to_numeric(df["PRCP"], errors="coerce")
    return df.dropna(subset=["PRCP"])


def _fetch_observations(snap: RawSnapshot, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    isd = _fetch_isd(snap, start, end)
    if not isd:
        raise RuntimeError("no NCEI ISD observations could be downloaded")
    isd_end = min(df["ts_utc"].max() for df in isd.values())
    ghcnh = _fetch_ghcnh(snap, list(range(isd_end.year, end.year + 1))) if isd_end < end - pd.Timedelta(days=2) else {}
    per_source: dict[str, pd.DataFrame] = {}
    for call in STATIONS.values():
        if call in isd:
            per_source[f"isd:{call}"] = isd[call].assign(precip_zero_filled=False)
    for call, g in ghcnh.items():
        last_isd = isd[call]["ts_utc"].max() if call in isd else pd.Timestamp.min.tz_localize("UTC")
        per_source[f"ghcnh:{call}"] = zero_fill_routine_precip(g[g["ts_utc"] > last_isd])
    # independent check of every hourly precip series against GHCN-Daily PRCP
    daily = _fetch_ghcn_daily(snap, start, end)
    validation: dict[str, Any] = {}
    if not daily.empty:
        ids = {v: k for k, v in GHCNH_STATIONS.items()}
        for name, df in per_source.items():
            validation[name] = validate_monthly_precip(df, daily[daily["STATION"] == ids[name.split(":")[1]]])
    # priority: KSEA (ISD then GHCNh) before KBFI (ISD then GHCNh)
    order = sorted(per_source, key=lambda k: (k.split(":")[1] != "KSEA", k.split(":")[0] != "isd"))
    last = max(df["ts_utc"].max() for df in per_source.values())
    out = combine_stations({k: per_source[k] for k in order}, start, last)
    snap.meta["observations"] = {
        "isd_last_ts_utc": str(isd_end),
        "per_source": {
            k: {
                "hours": len(v),
                "first_ts_utc": str(v["ts_utc"].min()),
                "last_ts_utc": str(v["ts_utc"].max()),
                "precip_hours_non_null": int(v["precip_mm"].notna().sum()),
            }
            for k, v in per_source.items()
        },
        "precip_monthly_vs_ghcn_daily": validation,
        "citywide_hours": len(out),
    }
    return out


def _fetch_mos(snap: RawSnapshot, end: pd.Timestamp) -> pd.DataFrame:
    sess = session()
    frames = []
    for model, (first, freq, delay_h) in MOS_MODELS.items():
        starts = pd.date_range(first, end.tz_localize(None), freq=freq)
        if starts.empty or starts[0] != pd.Timestamp(first):
            starts = starts.insert(0, pd.Timestamp(first))
        bounds = list(starts) + [end.tz_localize(None) + pd.Timedelta(days=1)]
        n = 0
        for a, b in zip(bounds[:-1], bounds[1:], strict=True):
            params = {
                "station": MOS_STATION,
                "model": model,
                "sts": a.strftime("%Y-%m-%dT%H:%MZ"),
                "ets": b.strftime("%Y-%m-%dT%H:%MZ"),
                "format": "csv",
            }
            try:
                r = http_get(sess, IEM_MOS_URL, params, timeout=600, backoff_s=5)
            except HttpError as e:
                snap.blocked(f"iem mos {model} {a:%Y-%m}", IEM_MOS_URL, e.status, e.detail)
                continue
            df = parse_mos_csv(r.text, model, delay_h)
            snap.save(f"iem_mos_{model}_{a:%Y%m%d}.csv", r.content, IEM_MOS_URL, params, rows=len(df))
            n += len(df)
            frames.append(df)
            time.sleep(IEM_PAUSE_S)
        log.info("IEM MOS %s: %d forecast rows", model, n)
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["model", "run_utc", "valid_ts_utc"]).sort_values(["model", "run_utc", "valid_ts_utc"])
    snap.meta["forecast_archive"] = {
        m: {
            "rows": int((out["model"] == m).sum()),
            "runs": int(out.loc[out["model"] == m, "run_utc"].nunique()),
            "first_run_utc": str(out.loc[out["model"] == m, "run_utc"].min()),
            "last_run_utc": str(out.loc[out["model"] == m, "run_utc"].max()),
            "assumed_publication_delay_h": MOS_MODELS[m][2],
        }
        for m in MOS_MODELS
    }
    return out.reset_index(drop=True)


def _probe_open_meteo(snap: RawSnapshot) -> dict[str, Any]:
    """One polite request per Open-Meteo archive-of-forecasts API; record status only."""
    sess = session()
    lat, lon = NWS_POINT
    status: dict[str, Any] = {}
    params = {"latitude": lat, "longitude": lon, "start_date": "2024-01-01", "end_date": "2024-01-02"}
    hourly = {"historical_forecast": "temperature_2m", "previous_runs": "temperature_2m_previous_day1"}
    for name, url in OPEN_METEO_PROBES.items():
        p = {**params, "hourly": hourly[name]}
        try:
            r = http_get(sess, url, p, retries=1, backoff_s=10)
            status[name] = {"http_status": 200, "at_utc": datetime.now(UTC).isoformat()}
            snap.save(f"open_meteo_{name}_probe.json", r.content, url, p)
        except HttpError as e:
            status[name] = {"http_status": e.status, "detail": e.detail[:200], "at_utc": datetime.now(UTC).isoformat()}
            snap.blocked(f"open-meteo {name}", url, e.status, e.detail)
    return status


def fetch_weather(cfg: Config, start: str | None = None, include_forecast_archive: bool = True) -> dict[str, Path]:
    """Download observations (+ archived MOS forecasts) and write processed parquet files."""
    start_ts = pd.Timestamp(start or cfg["training"]["start"], tz="UTC")
    end_ts = pd.Timestamp(datetime.now(UTC).date(), tz="UTC")
    snap = RawSnapshot(cfg.raw_dir, "weather")
    snap.meta["license"] = (
        "NOAA/NCEI ISD and GHCNh: U.S. Government work, no restrictions for U.S. stations. "
        "NWS MOS/LAMP: U.S. Government work, public domain; archive served by Iowa Environmental Mesonet."
    )
    obs = _fetch_observations(snap, start_ts, end_ts)
    outputs = {"weather_hourly": write_parquet(obs, cfg.processed_dir / "weather_hourly.parquet")}
    if include_forecast_archive:
        snap.meta["open_meteo_probe"] = _probe_open_meteo(snap)
        fc = _fetch_mos(snap, end_ts)
        if not fc.empty:
            outputs["weather_forecast_hourly"] = write_parquet(fc, cfg.processed_dir / "weather_forecast_hourly.parquet")
    snap.finish(outputs={k: str(v) for k, v in outputs.items()})
    log.info("weather snapshot: %s", snap.dir)
    return outputs


def fetch_nws_forecast(cfg: Config, point: tuple[float, float] = NWS_POINT, store: bool = True) -> pd.DataFrame:
    """Current NWS hourly forecast for Seattle (used at live forecast-issue time).

    With ``store=True`` the raw responses are snapshotted under
    ``data/raw/nws_forecast/`` and the parsed rows appended (deduplicated on
    issued_at_utc × valid_ts_utc) to ``data/processed/weather_forecast_nws_live.parquet``
    so that live forecasts accumulate into a leakage-free archive over time.
    """
    sess = session(NWS_HEADERS)
    fetched_at = datetime.now(UTC)
    pts_url = f"https://api.weather.gov/points/{point[0]:.4f},{point[1]:.4f}"
    pts = http_get(sess, pts_url, timeout=60)
    meta = pts.json()["properties"]
    hourly_r = http_get(sess, meta["forecastHourly"], {"units": "si"}, timeout=60)
    try:
        grid_r = http_get(sess, meta["forecastGridData"], timeout=60)
        grid = grid_r.json()
    except HttpError as e:  # QPF is optional
        log.warning("NWS gridpoint data unavailable: %s", e)
        grid_r, grid = None, None
    df = parse_nws_hourly(hourly_r.json(), grid, fetched_at)
    df["grid"] = f"{meta['gridId']}/{meta['gridX']},{meta['gridY']}"
    if store:
        snap = RawSnapshot(cfg.raw_dir, "nws_forecast", fetched_at)
        snap.save("points.json", pts.content, pts_url)
        snap.save("forecast_hourly.json", hourly_r.content, meta["forecastHourly"], {"units": "si"}, rows=len(df))
        if grid_r is not None:
            snap.save("gridpoint.json", grid_r.content, meta["forecastGridData"])
        snap.finish(license="NWS API: U.S. Government work, public domain", issued_at_utc=str(df["issued_at_utc"].iloc[0]))
        path = cfg.processed_dir / "weather_forecast_nws_live.parquet"
        hist = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        allf = pd.concat([hist, df], ignore_index=True).drop_duplicates(["issued_at_utc", "valid_ts_utc"], keep="first")
        write_parquet(allf, path)
    return df


def refresh_mos_recent(cfg: Config, days: int = 3, model: str = "GFS") -> int:
    """Append the most recent MOS runs (live operation) to the forecast archive.

    Only the last ``days`` days are requested, so this is cheap enough to run
    every hourly cycle. Returns the number of new rows."""
    sess = session()
    end = pd.Timestamp(datetime.now(UTC)).floor("h")
    start = end - pd.Timedelta(days=days)
    params = {"station": MOS_STATION, "model": model, "sts": start.strftime("%Y-%m-%dT%H:%MZ"),
              "ets": (end + pd.Timedelta(hours=1)).strftime("%Y-%m-%dT%H:%MZ"), "format": "csv"}
    snap = RawSnapshot(cfg.raw_dir, "weather_mos_live")
    r = http_get(sess, IEM_MOS_URL, params, timeout=120, backoff_s=5)
    df = parse_mos_csv(r.text, model, MOS_MODELS[model][2])
    snap.save(f"iem_mos_{model}_recent.csv", r.content, IEM_MOS_URL, params, rows=len(df))
    snap.finish(license="NWS MOS: public domain; archive served by Iowa Environmental Mesonet")
    path = cfg.processed_dir / "weather_forecast_hourly.parquet"
    hist = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    n0 = len(hist)
    allf = pd.concat([hist, df], ignore_index=True).drop_duplicates(["model", "run_utc", "valid_ts_utc"], keep="first")
    write_parquet(allf.sort_values(["model", "run_utc", "valid_ts_utc"]), path)
    return len(allf) - n0


def fetch_nws_observations(cfg: Config, station: str = "KSEA", limit: int = 72) -> pd.DataFrame:
    """Recent real-time METAR observations from api.weather.gov (hourly frame with
    ts_utc, temp_c, precip_mm, wind_ms). NCEI's archives lag ~40 h, so live
    'previous 24 h' weather features come from here."""
    sess = session(NWS_HEADERS)
    url = f"https://api.weather.gov/stations/{station}/observations"
    start = (pd.Timestamp(datetime.now(UTC)) - pd.Timedelta(hours=limit)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = http_get(sess, url, {"start": start, "limit": 500}, timeout=60)
    rows = []
    for f in r.json().get("features", []):
        pr = f.get("properties", {})
        rows.append({
            "ts": pd.Timestamp(pr.get("timestamp")),
            "temp_c": (pr.get("temperature") or {}).get("value"),
            "precip_mm": (pr.get("precipitationLastHour") or {}).get("value"),
            "wind_ms": ((pr.get("windSpeed") or {}).get("value") or np.nan) * KMH_TO_MS if (pr.get("windSpeed") or {}).get("value") is not None else np.nan,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts_utc"] = pd.to_datetime(df["ts"], utc=True).dt.floor("h")
    for c in ("temp_c", "precip_mm", "wind_ms"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # METAR omits the precipitation group when none fell
    df["precip_mm"] = df["precip_mm"].fillna(0.0)
    return df.groupby("ts_utc")[["temp_c", "precip_mm", "wind_ms"]].mean().reset_index()


__all__ = [
    "fetch_weather",
    "fetch_nws_forecast",
    "fetch_nws_observations",
    "refresh_mos_recent",
    "isd_to_hourly",
    "ghcnh_to_hourly",
    "zero_fill_routine_precip",
    "validate_monthly_precip",
    "combine_stations",
    "parse_mos_csv",
    "parse_nws_hourly",
    "rel_humidity",
]
