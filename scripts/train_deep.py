#!/usr/bin/env python
"""Run a deep-model research job described by a YAML file (configs/gpu/*.yaml).

    python scripts/train_deep.py --job configs/gpu/deep_property.yaml

The job trains each neural architecture with the leakage-safe feature pipeline,
then scores it together with the comparison models on the *same* test windows
(one backtest run per category, tagged ``deep``). Results land in
artifacts/backtests/<category>_<event_time>_deep/<run_id>/report.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seacrime.config import load_config  # noqa: E402
from seacrime.evaluation.backtest import run_backtest  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    job = yaml.safe_load(Path(args.job).read_text())
    cfg = load_config()
    try:
        import torch

        logging.info("torch %s, cuda available=%s, device=%s", torch.__version__, torch.cuda.is_available(),
                     torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu")
        if job.get("device") == "cuda" and not torch.cuda.is_available():
            logging.warning("job requests CUDA but none is available; falling back to CPU (expect long runtimes)")
            job["device"] = "cpu"
    except ImportError:
        raise SystemExit("PyTorch is required: pip install -e '.[deep]'") from None
    cats = job["category"] if isinstance(job["category"], list) else [job["category"]]
    deep_kw = dict(job.get("deep", {}))
    deep_kw["device"] = job.get("device")
    deep_kw["seed"] = job.get("seed", 0)
    for cat in cats:
        rep = run_backtest(
            cfg,
            cat,
            job["horizons"],
            list(job.get("compare_with", [])) + list(job["archs"]),
            cfg.artifacts_root / "backtests",
            event_time=job.get("event_time", "report"),
            n_train=job["n_train_issue_times"],
            n_val=job["n_val_issue_times"],
            test_stride=job.get("test_stride_h", 1),
            deep_kw=deep_kw,
            tag={"cpu_smoke": "deep_smoke", "deep_cpu": "deep_cpu"}.get(job["job"], "deep"),
        )
        for h, hr in rep["horizons"].items():
            for m, r in hr["models"].items():
                print(json.dumps({"category": cat, "h": h, "model": m, "capture_10": round(r["capture_10"], 4),
                                  "deviance": round(r["poisson_deviance"], 5), "brier": round(r["brier"], 6)}))


if __name__ == "__main__":
    main()
