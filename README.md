# Seattle crime forecasting research platform

Area-level, hourly forecasts of **publicly reported** Seattle Police Department
incidents on a 500 m grid, for 1, 3, 6 and 24-hour horizons, with a
time-aware evaluation harness, a prospective (frozen-forecast) scoring engine
and an interactive map.

> Research and public-understanding tool. Outputs are aggregated to grid cells;
> there are no person-level predictions and no enforcement recommendations.
> Reported crime reflects reporting and policing practices as well as crime;
> predictive associations are not causal.

**Results:** [reports/backtest_results.md](reports/backtest_results.md) ·
**Engineering report:** [reports/ENGINEERING_REPORT.md](reports/ENGINEERING_REPORT.md) ·
**Data validation:** [reports/data_validation.md](reports/data_validation.md)

## What is in the repository

| Path | Contents |
|---|---|
| `src/seacrime/ingest/` | Reproducible snapshot ingestion (manifests with publisher versions and SHA-256) for SPD crime, SPD CAD calls, SFD Fire/EMS 911, NOAA weather + archived NWS MOS forecasts, sports schedules and special-event permits, King County Metro / Sound Transit GTFS, city/census/zoning/street geography, news feeds |
| `src/seacrime/incidents.py` | Offense store with stable ids, snapshot reconciliation (`first_seen_at_utc`, change log, removed records retained) |
| `src/seacrime/grid.py`, `dataset.py`, `build.py` | Configurable grid (default 500 m), hourly count cubes, active cells |
| `src/seacrime/features.py`, `context.py` | Leakage-safe features with per-source availability lags |
| `src/seacrime/models/` | Hotspot KDE, seasonal hour-of-week (empirical Bayes), Poisson & NB GLMs, LightGBM, CatBoost, PyTorch TCN / ConvLSTM / ST-GNN / event transformer / neural Hawkes |
| `src/seacrime/evaluation/` | Identical-window backtests, rolling-origin backtests, ablations, lag sensitivity, metrics and bootstrap CIs |
| `src/seacrime/prospective/` | Production forecasting, immutable forecast store, maturity-aware scoring, replay, hourly cycle |
| `src/seacrime/export.py`, `api/` | Dashboard JSON export ([contract](docs/API_CONTRACT.md)) and read-only FastAPI |
| `web/` | Next.js + MapLibre dashboard (static export) |
| `infra/`, `Dockerfile`, `.github/workflows/` | Terraform (GCP, not applied), Docker, CI, optional scheduled cycle |
| `configs/` | Main config and GPU job configs |
| `docs/` | [Methodology](docs/METHODOLOGY.md), [data sources](docs/DATA_SOURCES.md) ([context sources](docs/DATA_SOURCES_CONTEXT.md)), [deployment & costs](docs/DEPLOYMENT.md) |

## Quick start

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev,catboost,deep]"     # torch: add --index-url https://download.pytorch.org/whl/cpu for CPU wheels
export PYTHONPATH=src

python -m seacrime.ingest.run spd_crime spd_calls fire_911 weather events gtfs boundaries news
python -m seacrime.ingest.process                 # reconcile + de-duplicate
python -m seacrime.build                          # cubes + active cells
python -m seacrime.validate                       # reports/data_validation.md

python -m seacrime.evaluation.backtest --category property                       # main backtest
python -m seacrime.evaluation.backtest --category property --horizons 6 24 --models seasonal lgbm \
       --ablations no_weather no_events no_transit no_calls no_fire no_static no_other_cats history_only \
       --lag-sensitivity 0 24 72 168 --tag ablation
python -m seacrime.evaluation.backtest --category property --horizons 6 24 --models seasonal hotspot lgbm \
       --rolling-origins 2025-07-01 2025-10-01 2026-01-01 2026-04-01 2026-07-01
python scripts/make_report.py                     # reports/backtest_results.md

python -m seacrime.train                          # production models
python -m seacrime.prospective.cycle              # ingest → forecast (frozen) → score → export
cd web && npm ci && npm run build && npm start    # dashboard at http://localhost:3000
# or: uvicorn seacrime.api.app:app --port 8000    # API + built dashboard
pytest -q                                         # unit, leakage, integration tests
```

Deep models on a GPU: `python scripts/train_deep.py --job configs/gpu/deep_property.yaml`
(see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for costs). `configs/gpu/cpu_smoke.yaml`
runs the same code path on CPU in a few minutes.

## Forecast definition (short)

For every hourly issue time *t*, horizon *h* and cell: expected number of SPD
incident reports (distinct `report_number`, by report time) in [*t*, *t + h*), P(≥ 1),
relative risk vs. the seasonal baseline and a 90 % negative-binomial interval.
Features only use data available at *t* (crime reports older than 72 h, CAD calls
older than 96 h, Fire/EMS dispatches older than 1 h, weather forecasts issued
before *t*, schedules already published). Details: [docs/METHODOLOGY.md](docs/METHODOLOGY.md).
