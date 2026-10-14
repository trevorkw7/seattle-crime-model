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

## Key findings so far (executed experiments)

Primary target: SPD property-crime reports per 500 m cell, test period
2025-07-01 → 2026-09-14 (≈10,500 hourly issue times per horizon × 924 cells,
identical for all models). Full tables: [reports/backtest_results.md](reports/backtest_results.md).

* **Strong concentration, strong baseline.** The top 10 % of cells receive ≈52 % of
  reports (PAI ≈ 5.2). The empirical-Bayes hour-of-week **seasonal baseline** reaches
  capture@10 % = 52.0–52.1 % at every horizon; the KDE hotspot map is slightly worse
  (−0.04 to −0.29 pp).
* **Learned models add little.** Best learned model vs seasonal (paired day-block
  bootstrap, 90 % CI): h=1 Poisson GLM +0.24 pp [+0.08, +0.40]; h=3 Poisson GLM
  +0.25 pp [+0.10, +0.39]; h=6 LightGBM +0.24 pp [+0.13, +0.35]; h=24 NB GLM
  +0.33 pp [+0.20, +0.47]. Poisson deviance improves by ≤0.9 % (h=24 Poisson GLM).
* **Calibration drift dominates error.** All models over-predict in the test period
  (pred/obs 1.04 seasonal, ≈1.09 for the gradient-boosted models at 6–24 h), most in
  the South/Southwest precincts — reported property crime declined relative to the
  training years.
* **Context barely helps.** Removing weather, events, transit, CAD calls, Fire/EMS,
  static geography or other crime categories changes LightGBM capture@10 % by
  ≤0.08 pp (all 90 % CIs include 0); removing all context costs ≈0.3 % deviance at
  6 h. Event windows show no specific gain.
* **Data timeliness matters little at this resolution.** Re-training with
  hypothetical instant crime publication (lag 0 h vs 72 h) raises 24 h capture@10 %
  by ≈0.17 pp.
* **Retraining fixes most of the drift.** Quarterly rolling-origin retraining keeps
  LightGBM's gain (+0.21 / +0.29 pp at 6 / 24 h) and brings its 6 h pred/obs from
  1.10 to 1.03.
* **Sparse categories benefit more.** For located violent incidents (top 10 % of cells
  hold 64 % of reports) LightGBM gains +0.77 pp [+0.23, +1.26] at 6 h and cuts
  deviance by 4–6 %; all located offenses behave like property (+0.2–0.3 pp).
  An occurrence-time property target (24 h) shows the same pattern (+0.26 pp,
  −1.6 % deviance).
* **Neural models (CPU, reduced scale) are on par with LightGBM at best.** At 6 h none
  of TCN, ConvLSTM, ST-GNN, the event-sequence transformer or the neural Hawkes process
  matched LightGBM; at 24 h the neural Hawkes process and TCN were ≈0.1 % better on
  deviance and better calibrated, with capture gains indistinguishable from LightGBM's.

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
