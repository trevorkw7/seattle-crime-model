"""One prospective forecasting cycle (run hourly by the scheduler).

    python -m seacrime.prospective.cycle            # full cycle
    python -m seacrime.prospective.cycle --no-ingest

Steps (each logged to the jobs table; a failure in one step is recorded and
later steps still run when they can):
  1. ingest   — refresh Fire 911 (incremental, every cycle); SPD crime and SPD
                calls only when the publisher's ``rowsUpdatedAt`` changed.
  2. build    — rebuild hourly cubes, padded 26 h into the future.
  3. forecast — issue + freeze forecasts for all production models/categories.
  4. score    — (re)score every forecast window against the latest snapshot.
  5. export   — write dashboard JSON (web/public/data) incl. health.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import traceback
from datetime import UTC, datetime

import pandas as pd

from ..build import build_all
from ..config import Config, load_config
from ..ingest.process import process_fire_911, process_spd_calls, process_spd_crime
from ..ingest.socrata import SocrataQuery, dataset_metadata, latest_snapshot, snapshot
from ..ingest.sources import FIRE_911, SPD_CALLS, SPD_CRIME
from .forecast import issue_forecasts, prospective_root
from .score import score_all
from .store import ForecastStore

log = logging.getLogger(__name__)


def _publisher_changed(cfg: Config, source: str, dataset_id: str) -> bool:
    snap = latest_snapshot(cfg.raw_dir, source)
    if snap is None:
        return True
    prev = json.loads((snap / "manifest.json").read_text())["dataset_metadata"].get("rowsUpdatedAt")
    return dataset_metadata(dataset_id).get("rowsUpdatedAt") != prev


def _incremental(cfg: Config, source: str, base: SocrataQuery, ts_field: str, processed: str, ts_col: str, days: int, process) -> dict:
    """Fetch only recent rows, then merge into the processed table."""
    p = cfg.processed_dir / processed
    if not p.exists():
        snap = snapshot(source, base, cfg.raw_dir)
        return process(cfg, snap)
    prev = pd.read_parquet(p)
    since = (pd.Timestamp(prev[ts_col].max()) - pd.Timedelta(days=days)).tz_convert("America/Los_Angeles").strftime("%Y-%m-%dT%H:%M:%S")
    q = SocrataQuery(base.dataset_id, select=base.select, where=f"{ts_field} >= '{since}'", order=base.order, page_size=base.page_size)
    snap = snapshot(source, q, cfg.raw_dir)
    man = json.loads((snap / "manifest.json").read_text())
    man["incremental_since_local"] = since
    (snap / "manifest.json").write_text(json.dumps(man, indent=2))
    tmp = cfg.processed_dir / f"_{processed}"
    p.rename(tmp)
    try:
        summary = process(cfg, snap)  # writes only the recent rows to `p`
        new = pd.read_parquet(p)
        key = [c for c in ("incident_number", "event_id") if c in new.columns][:1] + (["type"] if "type" in new.columns else [])
        merged = pd.concat([prev[prev[ts_col] < new[ts_col].min()], new]).drop_duplicates(key, keep="last").sort_values(ts_col)
        merged.to_parquet(p, index=False)
        tmp.unlink()
        summary["merged_rows"] = int(len(merged))
        return summary
    except Exception:
        if tmp.exists():
            tmp.replace(p)
        raise


def run_cycle(cfg: Config, ingest: bool = True, categories: list[str] | None = None) -> dict:
    store = ForecastStore(prospective_root(cfg))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    categories = categories or list(cfg["categories"])
    results: dict = {"run_id": run_id}

    def step(name, fn):
        store.job_start(run_id, name)
        try:
            out = fn()
            store.job_end(run_id, name, "ok", json.dumps(out, default=str)[:1500])
            results[name] = out
            return out
        except Exception as e:  # recorded for monitoring; cycle continues
            log.error("step %s failed: %s", name, e)
            store.job_end(run_id, name, "error", f"{e}\n{traceback.format_exc()[-1500:]}")
            results[name] = {"error": str(e)}
            return None

    if ingest:
        step("ingest_fire_911", lambda: _incremental(cfg, "fire_911", FIRE_911, "datetime", "fire_911.parquet", "ts_utc", 2, process_fire_911))

        def spd():
            if not _publisher_changed(cfg, "spd_crime", SPD_CRIME.dataset_id):
                return {"skipped": "publisher not updated"}
            return process_spd_crime(cfg, snapshot("spd_crime", SPD_CRIME, cfg.raw_dir))

        step("ingest_spd_crime", spd)

        def calls():
            if not _publisher_changed(cfg, "spd_calls", SPD_CALLS.dataset_id):
                return {"skipped": "publisher not updated"}
            return _incremental(cfg, "spd_calls", SPD_CALLS, "cad_event_original_time_queued", "spd_calls.parquet", "ts_utc", 14, process_spd_calls)

        step("ingest_spd_calls", calls)
    step("build_cubes", lambda: {"active_cells": build_all(cfg, extend_to=pd.Timestamp.now("UTC") + pd.Timedelta(hours=27))["active_cells"]})
    step("hourly_forecast", lambda: {"frozen": issue_forecasts(cfg, categories)})
    step("score", lambda: {"scored_windows": score_all(cfg)})

    def export():
        from ..export import export_all

        return {"out": str(export_all(cfg))}

    step("export", export)
    return results


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ingest", action="store_true")
    ap.add_argument("--categories", nargs="*")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    res = run_cycle(load_config(), ingest=not args.no_ingest, categories=args.categories)
    print(json.dumps(res, indent=2, default=str)[:4000])
    if any(isinstance(v, dict) and "error" in v for v in res.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
