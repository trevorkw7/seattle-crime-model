#!/usr/bin/env bash
# Reproduce every result in reports/ from scratch (≈ 3–5 h on 4 CPU cores, 16 GB RAM).
# Run steps one at a time on small machines: concurrent backtests can exceed memory.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=${PY:-.venv/bin/python}

$PY -m seacrime.ingest.run spd_crime spd_calls fire_911 weather events gtfs boundaries news
$PY -m seacrime.ingest.process
$PY -m seacrime.build
$PY -m seacrime.validate

# 1. main identical-window backtest (all baselines + GLMs + GBMs), primary target
$PY -m seacrime.evaluation.backtest --category property --horizons 1 3 6 24 \
    --models seasonal hotspot poisson_glm negbin_glm lgbm catboost --n-train 3000 --n-val 700
# 2. ablations + data-timeliness counterfactual
$PY -m seacrime.evaluation.backtest --category property --horizons 6 24 --models seasonal lgbm \
    --ablations no_weather no_events no_transit no_calls no_fire no_static no_other_cats history_only \
    --lag-sensitivity 0 24 72 168 --tag ablation
# 3. rolling-origin (quarterly retraining)
$PY -m seacrime.evaluation.backtest --category property --horizons 6 24 --models seasonal hotspot lgbm \
    --rolling-origins 2025-07-01 2025-10-01 2026-01-01 2026-04-01 2026-07-01 --n-train 2000 --n-val 500
# 4. other categories and the occurrence-time target
$PY -m seacrime.evaluation.backtest --category violent --horizons 6 24 --models seasonal hotspot lgbm
$PY -m seacrime.evaluation.backtest --category all_located --horizons 1 6 24 --models seasonal hotspot lgbm
$PY -m seacrime.evaluation.backtest --category property --event-time offense --horizons 24 --models seasonal hotspot lgbm
# 5. neural research models (CPU-sized; use configs/gpu/deep_property.yaml on a GPU)
$PY scripts/train_deep.py --job configs/gpu/deep_cpu.yaml
$PY scripts/make_report.py

# 6. production models, replay + live forecasts, dashboard export
$PY -m seacrime.train --categories property violent all_located --models seasonal hotspot lgbm
$PY scripts/replay.py --days 45
$PY -m seacrime.prospective.cycle --no-ingest
