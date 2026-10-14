# Engineering report

Date: 2026-10-14. Environment: cloud development container, 4 vCPU, 16 GB RAM,
**no GPU**, outbound HTTPS via proxy. Data snapshot: SPD crime `20261014T185811Z`
(publisher update 2026-10-14 15:08 UTC). Every number below comes from code and
runs in this repository; see [backtest_results.md](backtest_results.md) for full
tables and [data_validation.md](data_validation.md) for data checks.

## 1. What works (verified)

| Capability | Status | Evidence |
|---|---|---|
| Real data ingestion (SPD offenses 1.57 M rows, SPD CAD 4.35 M rows → 2.47 M events, SFD Fire/EMS 845 k, NOAA weather 64.6 k h, archived MOS forecasts 570 k rows, 3,209 events, GTFS 6 feeds, boundaries/census/zoning/streets, 332 news items) | ✅ | immutable snapshots with manifests in `data/raw/*/`, `reports/data_validation.md` |
| Stable ids, de-duplication, snapshot reconciliation (first-seen / changed / removed) | ✅ | `incidents.py`, `tests/test_pipeline.py` |
| Data validation report (missing fields, duplicates, DST, coordinates, gaps, publication completeness) | ✅ | `reports/data_validation.md` |
| Configurable grid (500 m default) and hourly cubes; 924 forecast cells | ✅ | `grid.py`, `build.py`, `tests/test_core.py` |
| Leakage-safe features with per-source availability lags; archived-forecast weather; ex-ante event attributes | ✅ | `features.py`, `context.py`, `tests/test_leakage.py` (10 tests) |
| Baselines (hotspot KDE, EB seasonal), Poisson/NB GLM, LightGBM (+ seasonal-offset variant), CatBoost | ✅ trained & backtested | `reports/backtest_results.md` |
| Identical-window chronological backtest with bootstrap CIs, ablations, timeliness counterfactual, rolling-origin, event windows, precinct/month stability | ✅ | `evaluation/backtest.py` |
| Neural models (TCN, ConvLSTM, ST-GNN, event transformer, neural Hawkes) | ✅ implemented, CPU-trained at reduced scale | `models/deep/`, `configs/gpu/`, `docs/GPU_RESEARCH.md` |
| Prospective engine: freeze (read-only + SHA-256) before window opens, provenance (model version, snapshot ids, feature cutoffs), maturity-aware re-scoring, replay | ✅ | `prospective/`, `tests/test_end_to_end.py` |
| Hourly cycle with job logging + health export | ✅ ran locally | `prospective/cycle.py`, `logs/` |
| Dashboard (map, horizons, categories, models, legend, history overlay with suppression, replay, evaluation, status, demo/actual/provisional/replay labelling, mobile) | ✅ built & screenshotted with real data | `web/`, `web/screenshots/real/` |
| Read-only API | ✅ | `api/app.py`, `tests/test_pipeline.py` |
| CI, Docker, Terraform (validated, not applied), cost estimate | ✅ written | `.github/workflows/`, `infra/`, `docs/DEPLOYMENT.md` |
