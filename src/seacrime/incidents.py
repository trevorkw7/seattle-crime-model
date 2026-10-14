"""SPD offense store: cleaning, stable identifiers, and snapshot reconciliation.

Unit of record = one NIBRS *offense* row, keyed by ``(report_number, offense_id)``.
Unit of forecasting = one *incident report* per category: a report with several
property offenses counts once for "property" (stable id ``report_number``).

Each snapshot is reconciled into ``data/processed/spd_offenses.parquet``:

* ``first_seen_at_utc`` — our fetch time of the first snapshot containing the row
  (the availability timestamp, kept separate from occurrence/report times).
  Rows already present in the initial backfill have ``backfill=True``: their true
  publication time is unknown, so backtests use a modelled availability lag.
* ``last_seen_at_utc`` / ``present_in_latest`` — rows that disappear (e.g.
  unfounded or reclassified) are retained and flagged, never silently dropped.
* ``n_changes`` — number of snapshots in which classification/location fields
  changed (provisional → finalized reconciliation).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .timeutil import local_to_utc

log = logging.getLogger(__name__)

STORE_NAME = "spd_offenses.parquet"
CHANGELOG_NAME = "spd_offense_changes.parquet"
TRACKED_FIELDS = [
    "report_ts_utc",
    "offense_ts_utc",
    "lat",
    "lon",
    "offense_category",
    "nibrs_offense_code",
    "nibrs_crime_against_category",
]
BBOX_PAD = 0.05  # degrees around configured bbox considered "plausible Seattle"


def clean_spd(raw: pd.DataFrame, bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    """Normalise a raw SPD snapshot into typed offense rows."""
    df = pd.DataFrame(
        {
            "report_number": raw["report_number"].astype("string"),
            "offense_id": raw["offense_id"].astype("string"),
        }
    )
    df["key"] = df["report_number"] + "|" + df["offense_id"]
    df["report_ts_utc"] = local_to_utc(raw["report_date_time"])
    df["offense_ts_utc"] = local_to_utc(raw["offense_date"])
    ot = pd.to_datetime(raw["offense_date"], errors="coerce", format="mixed")
    df["offense_time_is_midnight"] = ((ot.dt.hour == 0) & (ot.dt.minute == 0) & (ot.dt.second == 0)).to_numpy()
    lat = pd.to_numeric(raw["latitude"], errors="coerce")
    lon = pd.to_numeric(raw["longitude"], errors="coerce")
    redacted = raw["latitude"].astype("string").str.upper().eq("REDACTED").fillna(False)
    unknown = (lat <= 0) | (lon >= 0) | lat.isna() | lon.isna()
    lon0, lat0, lon1, lat1 = bbox
    in_box = lat.between(lat0 - BBOX_PAD, lat1 + BBOX_PAD) & lon.between(lon0 - BBOX_PAD, lon1 + BBOX_PAD)
    status = np.select(
        [redacted.to_numpy(), unknown.fillna(True).to_numpy(), ~in_box.fillna(False).to_numpy()],
        ["redacted", "unknown", "out_of_area"],
        default="ok",
    )
    df["loc_status"] = pd.Categorical(status)
    ok = df["loc_status"] == "ok"
    df["lat"] = lat.where(ok).astype("float64")
    df["lon"] = lon.where(ok).astype("float64")
    for c in [
        "offense_category",
        "nibrs_crime_against_category",
        "nibrs_offense_code",
        "nibrs_offense_code_description",
        "offense_sub_category",
        "precinct",
        "sector",
        "beat",
        "neighborhood",
        "block_address",
        "shooting_type_group",
    ]:
        df[c] = raw[c].astype("string") if c in raw else pd.NA
    # deterministic de-duplication of the (rare) repeated keys: keep the row with
    # the lexicographically smallest content hash so reruns are stable.
    df["row_hash"] = _row_hash(df)
    n0 = len(df)
    df = df.sort_values(["key", "row_hash"]).drop_duplicates("key", keep="first").reset_index(drop=True)
    df.attrs["n_duplicate_keys_dropped"] = n0 - len(df)
    return df


def _row_hash(df: pd.DataFrame) -> pd.Series:
    cols = TRACKED_FIELDS + ["shooting_type_group", "block_address"]
    s = df[cols].astype("string").fillna("").agg("|".join, axis=1)
    return s.map(lambda x: hashlib.md5(x.encode()).hexdigest()[:16]).astype("string")


def reconcile(processed_dir: Path, snap_df: pd.DataFrame, snapshot_id: str, fetched_at: pd.Timestamp) -> dict:
    """Upsert a cleaned snapshot into the offense store; returns a summary."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    store_p = processed_dir / STORE_NAME
    fetched_at = pd.Timestamp(fetched_at).tz_convert("UTC")
    snap = snap_df.copy()
    if not store_p.exists():
        snap["first_seen_snapshot"] = snapshot_id
        snap["first_seen_at_utc"] = fetched_at
        snap["last_seen_snapshot"] = snapshot_id
        snap["last_seen_at_utc"] = fetched_at
        snap["present_in_latest"] = True
        snap["backfill"] = True
        snap["n_changes"] = 0
        snap.to_parquet(store_p, index=False)
        summary = {"snapshot_id": snapshot_id, "new": len(snap), "changed": 0, "removed": 0, "initial_backfill": True}
        _append_log(processed_dir, summary)
        return summary

    store = pd.read_parquet(store_p)
    if snapshot_id in set(store["last_seen_snapshot"].astype(str)) and (store["last_seen_snapshot"] == snapshot_id).all():
        return {"snapshot_id": snapshot_id, "skipped": "already reconciled"}
    s_idx = store.set_index("key")
    n_idx = snap.set_index("key")
    common = s_idx.index.intersection(n_idx.index)
    new_keys = n_idx.index.difference(s_idx.index)
    gone_keys = s_idx.index.difference(n_idx.index)

    changed_mask = s_idx.loc[common, "row_hash"].to_numpy() != n_idx.loc[common, "row_hash"].to_numpy()
    changed_keys = common[changed_mask]
    if len(changed_keys):
        old = s_idx.loc[changed_keys, TRACKED_FIELDS].astype("string")
        new = n_idx.loc[changed_keys, TRACKED_FIELDS].astype("string")
        diff = old.ne(new) & ~(old.isna() & new.isna())
        changes = (
            diff.stack()
            .loc[lambda x: x]
            .reset_index()
            .rename(columns={"level_1": "field"})[["key", "field"]]
        )
        changes["old"] = [old.at[k, f] for k, f in zip(changes["key"], changes["field"], strict=True)]
        changes["new"] = [new.at[k, f] for k, f in zip(changes["key"], changes["field"], strict=True)]
        changes["snapshot_id"] = snapshot_id
        changes["detected_at_utc"] = fetched_at
        cl = processed_dir / CHANGELOG_NAME
        if cl.exists():
            changes = pd.concat([pd.read_parquet(cl), changes], ignore_index=True)
        changes.to_parquet(cl, index=False)

    # update existing rows with latest content but keep provenance columns
    keep = ["first_seen_snapshot", "first_seen_at_utc", "backfill", "n_changes"]
    upd = n_idx.loc[common].copy()
    for k in keep:
        upd[k] = s_idx.loc[common, k]
    upd.loc[changed_keys, "n_changes"] = upd.loc[changed_keys, "n_changes"] + 1
    upd["last_seen_snapshot"] = snapshot_id
    upd["last_seen_at_utc"] = fetched_at
    upd["present_in_latest"] = True

    newr = n_idx.loc[new_keys].copy()
    newr["first_seen_snapshot"] = snapshot_id
    newr["first_seen_at_utc"] = fetched_at
    newr["last_seen_snapshot"] = snapshot_id
    newr["last_seen_at_utc"] = fetched_at
    newr["present_in_latest"] = True
    newr["backfill"] = False
    newr["n_changes"] = 0

    gone = s_idx.loc[gone_keys].copy()
    gone["present_in_latest"] = False

    out = pd.concat([upd, newr, gone]).reset_index()
    out.to_parquet(store_p, index=False)
    summary = {
        "snapshot_id": snapshot_id,
        "fetched_at_utc": fetched_at.isoformat(),
        "new": int(len(new_keys)),
        "changed": int(len(changed_keys)),
        "removed": int(len(gone_keys)),
        "total": int(len(out)),
    }
    _append_log(processed_dir, summary)
    return summary


def _append_log(processed_dir: Path, summary: dict) -> None:
    with open(processed_dir / "spd_reconcile_log.jsonl", "a") as f:
        f.write(json.dumps(summary, default=str) + "\n")


def load_store(processed_dir: Path) -> pd.DataFrame:
    return pd.read_parquet(processed_dir / STORE_NAME)


def category_mask(store: pd.DataFrame, cat_def: dict) -> pd.Series:
    m = pd.Series(True, index=store.index)
    oc = cat_def.get("offense_category")
    if oc:
        m &= store["offense_category"].isin(oc)
    codes = cat_def.get("nibrs_offense_code")
    if codes:
        m &= store["nibrs_offense_code"].isin(codes)
    return m


def incidents(
    store: pd.DataFrame,
    cat_def: dict,
    event_time: str = "report",
    only_present: bool = True,
) -> pd.DataFrame:
    """One row per (report_number) incident in the category, with a single
    location and event timestamp. Unlocatable incidents are kept (cell=-1 later)
    so that totals and coverage can be reported."""
    m = category_mask(store, cat_def)
    if only_present:
        m &= store["present_in_latest"]
    s = store.loc[m]
    tcol = "report_ts_utc" if event_time == "report" else "offense_ts_utc"
    g = (
        s.sort_values(["report_number", "offense_id"])
        .groupby("report_number", sort=False)
        .agg(
            ts_utc=(tcol, "first"),
            report_ts_utc=("report_ts_utc", "first"),
            offense_ts_utc=("offense_ts_utc", "first"),
            offense_time_is_midnight=("offense_time_is_midnight", "first"),
            lat=("lat", "first"),
            lon=("lon", "first"),
            loc_status=("loc_status", "first"),
            precinct=("precinct", "first"),
            neighborhood=("neighborhood", "first"),
            first_seen_at_utc=("first_seen_at_utc", "min"),
            backfill=("backfill", "all"),
            n_offenses=("offense_id", "size"),
        )
        .reset_index()
        .rename(columns={"report_number": "incident_id"})
    )
    return g
