"""Turn raw snapshots into tidy, typed, de-duplicated processed tables."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config
from ..incidents import clean_spd, reconcile
from ..timeutil import local_to_utc
from .socrata import latest_snapshot, read_snapshot

log = logging.getLogger(__name__)


def _manifest(snap: Path) -> dict:
    return json.loads((snap / "manifest.json").read_text())


def process_spd_crime(cfg: Config, snap: Path | None = None) -> dict:
    snap = snap or latest_snapshot(cfg.raw_dir, "spd_crime")
    if snap is None:
        raise FileNotFoundError("no spd_crime snapshot; run ingest first")
    man = _manifest(snap)
    raw = read_snapshot(snap)
    df = clean_spd(raw, tuple(cfg["grid"]["bbox"]))
    summary = reconcile(cfg.processed_dir, df, man["snapshot_id"], pd.Timestamp(man["fetched_at_utc"]))
    summary["n_duplicate_keys_dropped"] = df.attrs.get("n_duplicate_keys_dropped", 0)
    log.info("spd_crime reconciled: %s", summary)
    return summary


def _coords(lat: pd.Series, lon: pd.Series, bbox) -> tuple[pd.Series, pd.Series]:
    lat = pd.to_numeric(lat, errors="coerce")
    lon = pd.to_numeric(lon, errors="coerce")
    lon0, lat0, lon1, lat1 = bbox
    ok = lat.between(lat0 - 0.05, lat1 + 0.05) & lon.between(lon0 - 0.05, lon1 + 0.05)
    return lat.where(ok), lon.where(ok)


def process_spd_calls(cfg: Config, snap: Path | None = None) -> dict:
    snap = snap or latest_snapshot(cfg.raw_dir, "spd_calls")
    if snap is None:
        raise FileNotFoundError("no spd_calls snapshot")
    man = _manifest(snap)
    raw = read_snapshot(snap)
    n_rows = len(raw)
    # one row per dispatched unit -> one row per CAD event
    raw = raw.drop_duplicates("cad_event_number")
    lat, lon = _coords(raw["dispatch_latitude"], raw["dispatch_longitude"], cfg["grid"]["bbox"])
    out = pd.DataFrame(
        {
            "event_id": raw["cad_event_number"].astype("string"),
            "ts_utc": local_to_utc(raw["cad_event_original_time_queued"]),
            "priority": pd.to_numeric(raw["priority"], errors="coerce").astype("Int8"),
            "event_group": raw["event_group"].astype("string"),
            "final_call_type": raw["final_call_type"].astype("string"),
            "initial_call_type": raw["initial_call_type"].astype("string"),
            "call_type": raw["call_type"].astype("string"),
            "call_type_indicator": raw["call_type_indicator"].astype("string"),
            "lat": lat.astype("float64"),
            "lon": lon.astype("float64"),
            "beat": raw["dispatch_beat"].astype("string"),
        }
    )
    out["available_at_utc"] = pd.Timestamp(man["fetched_at_utc"])
    out = out.dropna(subset=["ts_utc"]).sort_values("ts_utc").reset_index(drop=True)
    p = cfg.processed_dir / "spd_calls.parquet"
    out.to_parquet(p, index=False)
    summary = {
        "snapshot_id": man["snapshot_id"],
        "raw_rows": n_rows,
        "events": len(out),
        "located": int(out["lat"].notna().sum()),
        "min_ts": str(out["ts_utc"].min()),
        "max_ts": str(out["ts_utc"].max()),
    }
    log.info("spd_calls processed: %s", summary)
    return summary


def process_fire_911(cfg: Config, snap: Path | None = None) -> dict:
    snap = snap or latest_snapshot(cfg.raw_dir, "fire_911")
    if snap is None:
        raise FileNotFoundError("no fire_911 snapshot")
    man = _manifest(snap)
    raw = read_snapshot(snap)
    n_rows = len(raw)
    lat, lon = _coords(raw["latitude"], raw["longitude"], cfg["grid"]["bbox"])
    out = pd.DataFrame(
        {
            "incident_number": raw["incident_number"].astype("string"),
            "ts_utc": local_to_utc(raw["datetime"]),
            "type": raw["type"].astype("string"),
            "lat": lat.astype("float64"),
            "lon": lon.astype("float64"),
        }
    )
    n_dup = int(out.duplicated(["incident_number", "type"]).sum())
    out = out.drop_duplicates(["incident_number", "type"]).dropna(subset=["ts_utc"])
    t = out["type"].str.lower().fillna("")
    out["kind"] = np.select(
        [t.str.contains("medic|aid|mvi|injur|overdose|patient"), t.str.contains("fire|smoke|burn"), t.str.contains("alarm")],
        ["medical", "fire", "alarm"],
        default="other",
    )
    out = out.sort_values("ts_utc").reset_index(drop=True)
    out.to_parquet(cfg.processed_dir / "fire_911.parquet", index=False)
    summary = {
        "snapshot_id": man["snapshot_id"],
        "raw_rows": n_rows,
        "rows": len(out),
        "duplicate_rows_dropped": n_dup,
        "located": int(out["lat"].notna().sum()),
        "min_ts": str(out["ts_utc"].min()),
        "max_ts": str(out["ts_utc"].max()),
    }
    log.info("fire_911 processed: %s", summary)
    return summary


def main() -> None:
    """python -m seacrime.ingest.process  — process the latest snapshot of each core source."""
    import argparse

    from ..config import load_config

    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", default=["spd_crime", "spd_calls", "fire_911"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    fns = {"spd_crime": process_spd_crime, "spd_calls": process_spd_calls, "fire_911": process_fire_911}
    for s in args.sources:
        print(s, fns[s](cfg))


if __name__ == "__main__":
    main()
