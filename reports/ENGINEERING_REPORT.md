# Engineering report

Date: 2026-10-14. Environment: cloud development container, 4 vCPU, 16 GB RAM,
**no GPU**, outbound HTTPS via proxy. Data snapshot: SPD crime `20261014T185811Z`
(publisher update 2026-10-14 15:08 UTC). Every number below comes from code and
runs in this repository; see [backtest_results.md](backtest_results.md) for the full
tables, [runs/](runs/) for the raw run reports, and
[data_validation.md](data_validation.md) for data checks.

## 1. What works (verified)

| Capability | Status | Evidence |
|---|---|---|
| Real data ingestion: SPD offenses (1,568,054 rows), SPD CAD (4,350,038 rows → 2,471,750 events), SFD Fire/EMS (844,581), NOAA hourly weather (64.6 k h), archived NWS MOS forecasts (570 k rows), 3,209 scheduled events, 6 GTFS feeds, city/census/zoning/streets/beats, 332 news items | ✅ | immutable snapshots + `manifest.json` in `data/raw/*/`; `docs/DATA_SOURCES*.md` |
| Stable ids, de-duplication, snapshot reconciliation (first-seen / changed / removed kept with change log) | ✅ | `src/seacrime/incidents.py`, `tests/test_pipeline.py` |
| Data validation report (missing fields, duplicates, DST, coordinate uncertainty, gaps, publication completeness) | ✅ | `reports/data_validation.md` |
| Configurable grid (500 m), hourly count cubes, 924 forecast cells | ✅ | `grid.py`, `build.py` |
| Leakage-safe features (per-source availability lags, archived-forecast weather, ex-ante event attributes, schedule-publication times) | ✅ | `features.py`, `context.py`, `tests/test_leakage.py` |
| Baselines (KDE hotspot, empirical-Bayes hour-of-week seasonal), Poisson & NB GLMs, LightGBM (+ seasonal-offset variant), CatBoost | ✅ trained and backtested | `reports/backtest_results.md` |
| Identical-window chronological backtest, day-block bootstrap CIs, paired model deltas, ablations, data-timeliness counterfactual, rolling-origin retraining, event windows, precinct / month stability, calibration | ✅ | `src/seacrime/evaluation/` |
| Neural research models: TCN, ConvLSTM, ST-GNN, event-sequence transformer, neural Hawkes | ✅ implemented; reduced-scale CPU comparison (§3.5) | `models/deep/`, `configs/gpu/`, `docs/GPU_RESEARCH.md` |
| Prospective engine: forecasts frozen before their window (read-only file + SHA-256), model version, data snapshot ids and per-source feature cutoffs stored, maturity-aware re-scoring per snapshot, replay | ✅ | `prospective/`, `tests/test_end_to_end.py` |
| Hourly cycle (incremental Fire 911, publisher-aware SPD refresh, MOS refresh, cubes, forecasts, scoring, export) with job log and health | ✅ executed locally (`20261014T213316Z`) | `prospective/cycle.py` |
| Dashboard: heat map, horizon/category/model/metric selectors, legend + P(≥1), suppressed historical overlay, replay timeline, evaluation, status; demo / actual / provisional / replay labelling; mobile layout | ✅ built and screenshotted with real data | `web/`, `web/screenshots/real/` |
| Read-only API + static hosting of the dashboard | ✅ | `src/seacrime/api/app.py` |
| CI workflow, Dockerfile, docker-compose scheduler, Terraform (GCP; `terraform validate` passes), cost estimate | ✅ written, not deployed | `.github/workflows/`, `infra/`, `docs/DEPLOYMENT.md` |

## 2. What was tested

* `pytest`: 64 tests pass (unit, leakage, chronology, DST, metrics, NB
  dispersion/intervals, model fitting, reconciliation, frozen-store tamper detection,
  maturity labelling, weather-forecast availability, event schedule/attendance
  leakage, API, context-ingestion parsers, and an end-to-end synthetic lifecycle:
  snapshot → train → freeze → new snapshot → provisional score → export).
* Leakage tests perturb every hour at or after each source's cutoff (crime 72 h,
  calls 96 h, fire 1 h, weather issue time, schedule publication) and assert that
  no feature changes.
* `ruff` lint clean; `terraform validate` clean; `npm run build` + Playwright smoke
  test (11 checks, written by the dashboard build) and real-data screenshots.
* Bugs found and fixed by these checks: a forecast-metadata/horizon mismatch
  (end-to-end test), post-event sports attendance used as a feature (review of the
  events table), GLM transform decided per batch, memory blow-up (OOM) in the GLM
  design matrix, a dashboard crash with zero scored prospective forecasts, and
  neural-model checkpoints that serialized cached cube arrays (3.6 GB each, filled
  the disk mid-experiment; now weights/config only, < 2 MB, with a regression test).

## 3. Measured accuracy relative to baselines

Primary target: SPD **property-crime reports** (distinct report numbers, by report
time) per 500 m cell. Test period 2025-07-01 → 2026-09-14 (mature; reports in the last
30 days are scored separately as provisional). ≈10,550 hourly issue times per horizon
× 924 cells, identical for every model; crime data available only 72 h after report.

### 3.1 Main comparison (single chronological split, trained on 2019-07 → 2024-12)

Capture@10 % = share of reports falling in the 10 % of cells ranked highest (random
ranking ≈ 10 %). Δ = paired day-block-bootstrap difference vs the seasonal baseline
(90 % CI). Deviance = mean Poisson deviance per cell-window (lower is better).

| h | seasonal cap@10 | hotspot Δ | Poisson GLM Δ | NB GLM Δ | LightGBM Δ | CatBoost Δ | best deviance (vs seasonal) |
|---|---|---|---|---|---|---|---|
| 1 | 52.0 % | −0.29 pp [−0.49, −0.12] | +0.24 [+0.08, +0.40] | +0.10 [−0.07, +0.25] | +0.08 [−0.07, +0.23] | +0.10 [−0.06, +0.28] | Poisson GLM 0.03799 (0.03801) |
| 3 | 52.0 % | −0.04 [−0.09, +0.01] | +0.25 [+0.10, +0.39] | +0.23 [+0.09, +0.39] | +0.19 [+0.06, +0.31] | +0.15 [+0.01, +0.28] | Poisson GLM 0.08813 (0.08833) |
| 6 | 52.1 % | −0.09 [−0.15, −0.02] | +0.17 [+0.02, +0.31] | +0.18 [+0.03, +0.32] | +0.24 [+0.13, +0.35] | +0.13 [+0.03, +0.25] | NB GLM 0.14635 (0.14697) |
| 24 | 52.0 % | −0.29 [−0.45, −0.14] | +0.29 [+0.16, +0.43] | +0.33 [+0.20, +0.47] | +0.27 [+0.15, +0.38] | +0.31 [+0.20, +0.43] | Poisson GLM 0.37287 (0.37625) |

Calibration-in-the-large (predicted / observed): seasonal 1.04, hotspot 1.00, GLMs
0.97–1.07, LightGBM 1.03–1.10, CatBoost 1.03–1.09. Expected calibration error of
P(N ≥ 1) is ≤ 0.008 for every model. Top-25-cell "next incident" hit rate
(2.7 % of area): 23.3–24.0 % for all models.

### 3.2 Rolling origin (quarterly retraining, 5 folds 2025-07 → 2026-09)

| h | seasonal | hotspot Δ | LightGBM Δ | LightGBM pred/obs | LightGBM deviance vs seasonal |
|---|---|---|---|---|---|
| 6 | 52.0 % | −0.15 pp [−0.24, −0.05] | +0.21 pp [+0.08, +0.34] | 1.028 (single split: 1.096) | 0.14605 vs 0.14662 (−0.4 %) |
| 24 | 52.0 % | −0.05 pp [−0.11, +0.00] | +0.29 pp [+0.18, +0.40] | 1.053 (single split: 1.090) | 0.37272 vs 0.37543 (−0.7 %) |

### 3.3 Context ablations (LightGBM retrained without each group; Δ = full − ablated)

All single-group removals change capture@10 % by between −0.05 and +0.07 pp with
90 % CIs that include zero, at both 6 h and 24 h (weather, events, transit, CAD calls,
Fire/EMS, static geography, other crime categories). Removing *all* context
("history only") changes capture by +0.08 pp [−0.00, +0.14] (6 h) / −0.01 pp (24 h)
and raises deviance by 0.3 % (6 h) / 0.2 % (24 h). Windows overlapping major events
(sports, ≥10 k-attendance permits) show the same pattern; no event-specific gain was
detected. Caveat: the permit feed stops in early 2025, so the test period sees almost
no permits (sports schedules are complete).

### 3.4 Data timeliness counterfactual (24 h, LightGBM retrained per lag)

| crime data lag | 0 h (hypothetical) | 24 h | 72 h (used) | 168 h |
|---|---|---|---|---|
| capture@10 % | 52.33 % | 52.28 % | 52.17 % | 52.10 % |
| deviance | 0.37470 | 0.37496 | 0.37451 | 0.37407 |

Fresher data would add ≈0.2 pp of capture at most; near-repeat information is weak at
500 m / 24 h for reported property crime.

### 3.5 Other categories and targets (same protocol, LightGBM vs baselines)

| target | h | seasonal cap@10 | hotspot Δ | LightGBM Δ [90% CI] | deviance: LightGBM vs seasonal | LightGBM pred/obs |
|---|---|---|---|---|---|---|
| violent (located) | 6 | 64.2 % | −0.41 pp [−0.98, +0.15] | **+0.77 pp [+0.23, +1.26]** | 0.02420 vs 0.02519 (−3.9 %) | 1.085 |
| violent (located) | 24 | 64.2 % | −0.50 pp [−0.94, −0.05] | **+0.74 pp [+0.31, +1.15]** | 0.06955 vs 0.07367 (−5.6 %) | 1.066 |
| all located offenses | 1 | 53.7 % | −0.04 pp | +0.25 pp [+0.15, +0.36] | 0.05663 vs 0.05670 (−0.1 %) | 1.044 |
| all located offenses | 6 | 53.7 % | −0.07 pp | +0.29 pp [+0.22, +0.38] | 0.20553 vs 0.20620 (−0.3 %) | 1.080 |
| all located offenses | 24 | 53.6 % | −0.00 pp | +0.21 pp [+0.15, +0.29] | 0.47912 vs 0.48039 (−0.3 %) | 1.084 |
| property, **occurrence time** | 24 | 52.1 % | −0.43 pp [−0.58, −0.25] | +0.26 pp [+0.12, +0.40] | 0.36755 vs 0.37366 (−1.6 %) | 1.034 |
| property, LightGBM with seasonal log-offset | 24 | 52.0 % | – | +0.24 pp [+0.13, +0.35] | 0.37591 vs 0.37625 (−0.1 %) | 1.116 |

Violent incidents are sparser and more concentrated; there the learned model's gain is
about three times larger and the KDE hotspot beats the per-cell seasonal baseline on
deviance (smoothing helps sparse counts). Violent results cover only located incidents
(≈27 % of violent offenses are redacted or unlocated).

### 3.6 Neural models (CPU, reduced scale; property, report time)

Same leakage-safe inputs, 2,000 training issue times, ≤ 8 epochs, test every 3rd hour
(3,519 issue times at 6 h), seasonal and LightGBM re-scored on the identical windows:

<!-- DEEP_TABLE -->

No neural architecture beat LightGBM at this scale. Their residual-over-seasonal design
keeps them better calibrated (pred/obs 0.98–1.03 vs 1.06 for LightGBM). These are
reduced-scale CPU runs; the GPU configuration is in `configs/gpu/deep_property.yaml`.


## 4. Prospective forecasting status

* Live forecasts are frozen for all three categories × {seasonal, hotspot, LightGBM}
  production models (trained on windows ending 30 days before the snapshot) — e.g.
  `fc_20261014T2200Z_lgbm_property`, frozen 21:34 UTC for windows from 22:00 UTC.
* **No live forecast has been scored yet, and none can be within this session:** SPD
  publishes finalized reports once a day with multi-day delay, so the first
  *provisional* live scores appear after the next publisher refresh, and *mature*
  scores 30 days later. This requires the scheduled cycle to keep running
  (deployment, §5).
* The full freeze → score path is exercised by 132 **historical replay** forecasts
  (daily 17:00 local issue times over the last 44 days, pre-test models): 528 windows
  scored, labelled mature or provisional and shown separately from live results.

## 5. What remains blocked

| Item | Blocker | What is needed |
|---|---|---|
| Pull request | The GitHub repository has no base branch (the work branch is its only branch); creating a `main` branch was denied by this session's permission policy | Repository owner creates a base branch (e.g. an empty `main`) or authorizes it; then the PR can be opened from `claude/magical-darwin-8sbpll` |
| Cloud deployment (scheduled cycle, persistent storage, public dashboard) | No authorization to provision paid resources or expose services | Approve a GCP project + budget (≈$16–25/month, docs/DEPLOYMENT.md) or a small VM; `terraform apply` |
| Live prospective scores | Time: SPD data for the frozen windows is not yet published | Keep the hourly cycle running ≥ 1 day (provisional) / 30 days (mature) |
| Full-scale neural experiments | No GPU in the environment | Run `scripts/train_deep.py --job configs/gpu/deep_property.yaml` on a GPU (≈1–3 GPU-hours) |
| Concert/arena events, ticketing | Ticketmaster (HTTP 401) and SeatGeek (403) need API keys; Eventbrite endpoint gone | API keys |
| Traffic data | WSDOT Traveler API needs an access code | Access code |
| Historical transit schedules / disruptions | TransitFeeds 403, Mobility Database needs a token; OneBusAway API needs a key | Tokens; forward collection of GTFS-RT alerts already implemented |
| Census ACS covariates | api.census.gov now requires a key | Census API key |
| Historical news; LLM news extraction | GDELT returned 429; RSS feeds only cover recent weeks; `ANTHROPIC_API_KEY` not configured | Forward collection (implemented) + API key for optional extraction; no historical news signal could be evaluated |
| Special-event permits after early 2025 | Publisher stopped updating the dataset (2025-01-08) | Alternative permit source |

## 6. Next three highest-impact improvements

1. **Fix level drift before adding complexity.** The largest error is calibration
   (models over-predict by 4–10 % in 2025–26, unevenly by precinct). Weekly retraining
   (already scheduled) plus an online recalibration of the citywide level from the
   most recent mature weeks — and evaluating forecasts against that — should improve
   deviance and P(≥1) more than any architecture change seen so far.
2. **Run the prospective loop for real.** Deploy the hourly cycle (or the GitHub
   Actions schedule) so live forecasts accumulate provisional and mature scores; this
   also measures the true publication-delay curve from successive snapshots instead of
   the single-snapshot estimate, which in turn sets the feature lag and maturity window
   from data.
3. **Better targets and resolution rather than more context.** Context features did
   not help at 500 m; next test (a) an occurrence-time target with explicit
   reporting-delay modelling, (b) coarser/finer grids and beat-level aggregation, and
   (c) category-specific models (car prowl, burglary, shoplifting) whose spatial
   drivers differ — with GPU runs of the neural models on the same identical windows.
