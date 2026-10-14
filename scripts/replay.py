#!/usr/bin/env python
"""Freeze and score historical replay forecasts from the latest main backtest run.

    python scripts/replay.py --days 45 [--categories property] [--models lgbm seasonal hotspot]

Replay forecasts are as-of reconstructions (models trained before the test
period, features limited to data available at each past issue time). They are
stored with provenance 'replay' and never mixed with live results.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from seacrime.config import load_config  # noqa: E402
from seacrime.prospective.replay import default_replay_times, replay_forecasts  # noqa: E402
from seacrime.prospective.score import score_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--categories", nargs="+", default=["property"])
    ap.add_argument("--models", nargs="+", default=["lgbm", "seasonal", "hotspot"])
    ap.add_argument("--local-hour", type=int, default=17)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    for cat in args.categories:
        d = cfg.artifacts_root / "backtests" / f"{cat}_report"
        run = d / (d / "LATEST").read_text().strip()
        import json

        rep = json.loads((run / "report.json").read_text())
        data_end = pd.Timestamp(rep["split"]["data_end"])
        t0s = default_replay_times(data_end, days=args.days, local_hour=args.local_hour)
        ids = replay_forecasts(cfg, run, cat, args.models, t0s)
        logging.info("%s: %d replay forecasts from run %s", cat, len(ids), run.name)
    logging.info("scored %d windows", score_all(cfg))


if __name__ == "__main__":
    main()
