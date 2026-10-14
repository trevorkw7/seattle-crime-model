# Methodology

## 1. Forecast task

* **Spatial unit.** A square lattice in UTM 10N (EPSG:32610), 500 m cells by default
  (`grid.cell_size_m`, any size works). Of the 33 × 55 lattice, the 924 cells that
  intersect Seattle's city limits (or have ≥ 50 located incidents) are forecast;
  they contain 99.9 % of located incidents since mid-2019.
* **Temporal unit.** Forecasts are issued hourly. For issue time *t* and horizon
  *h* ∈ {1, 3, 6, 24} h the forecast window is [*t*, *t + h*).
* **Target.** Number of distinct SPD incident *reports* (stable id `report_number`)
  in the category whose **report time** (`report_date_time`) falls in the window
  and whose location falls in the cell. One report with several offenses in the
  same category counts once.
  * Why report time: it is recorded to the second (98 % of reports since 2020 have
    non-zero seconds) and is what is *publicly reported*. Offense start times are
    date-only for ~9 % of offenses (exact midnight) and arrive with long delays
    (median 12.5 h, 90th pct 10 days after the offense), which makes hourly
    occurrence targets unreliable. An occurrence-time target (`event_time: offense`)
    is supported and evaluated as a secondary experiment.
  * Report time also carries administrative patterns (business-hour reporting,
    batch approvals; weekly totals swing ±25 % in 2026). These are part of the
    target and are disclosed in the UI.
* **Categories.** `property` (SPD "PROPERTY CRIME": larceny-theft incl. car prowls
  and shoplifting, burglary, motor-vehicle theft, arson), `violent` (SPD "VIOLENT
  CRIME"; ~27 % unlocatable because of redaction, so this target only covers
  located violent incidents) and `all_located`.
* **Outputs per cell × window.** Expected count μ; P(N ≥ 1); relative risk
  μ / seasonal-baseline μ; 90 % predictive interval from a negative-binomial
  predictive distribution whose dispersion α is estimated on held-out data.

## 2. Information available at forecast time (leakage controls)

| Source | Publication behaviour observed | Rule used at issue time *t* |
|---|---|---|
| SPD crime (tazs-3rd5) | daily refresh, finalized reports only; last day ~0.5 % complete | only reports with report time < *t* − 72 h |
| SPD CAD calls (33kz-ixgy) | daily refresh; newest record 84 h old at fetch | only calls queued < *t* − 96 h |
| SFD Fire/EMS 911 (kzjm-xkqj) | updated every ~5 min; newest record 10 min old | only dispatches < *t* − 1 h |
| Weather | NWS MOS (GFS) archive with run/issue times | latest MOS run *issued* ≤ *t*; observed weather only from the past |
| Events | season schedules published months ahead; permits ahead of date | events overlapping the window (cancelled/postponed excluded) |
| Transit, zoning, census, streets | static | time-invariant attributes |

* Features are always computed from **report-time** cubes even when the target uses
  offense time, so late reports of past offenses cannot leak.
* `tests/test_leakage.py` perturbs every hour at or after each source's cutoff and
  asserts that no feature changes (while the target does).
* Weather "oracle" use of observed window weather exists only as a labelled
  ablation; the default uses archived forecasts.
* News items may be used only after their `fetched_at_utc` (live) or
  `published_at + margin` (labelled backfill).

## 3. Models

| id | family | description |
|---|---|---|
| `hotspot` | baseline | Gaussian-KDE of trailing-year counts (bandwidth tuned on validation) × trailing-28-day citywide rate |
| `seasonal` | baseline | Empirical-Bayes per-cell hour-of-week rate: the cell's same-window counts over the past 52 weeks shrunk (κ tuned) toward cell-share × citywide hour-of-week rate |
| `poisson_glm` | GLM | L2 Poisson regression on log-transformed history/context features + cyclic calendar |
| `negbin_glm` | GLM | NB2 maximum-likelihood regression (statsmodels) on the same design |
| `lgbm` | GBM | LightGBM, Poisson objective, early stopping on validation |
| `catboost` | GBM | CatBoost, Poisson objective |
| `tcn`, `convlstm`, `stgnn`, `transformer`, `hawkes` | deep | PyTorch research models, residual over the seasonal baseline (see `src/seacrime/models/deep/nets.py`) |

Feature groups: target-category history (1 d … 365 d), neighbouring-cell history
(rings 1–2), same-hour-of-week history (4 and 52 weeks), other crime categories,
SPD calls, Fire/EMS dispatches, calendar & holidays, static geography (zoning
shares, population, street length, distance to downtown, cell coordinates), transit
scheduled departures in the window, MOS weather forecast, and scheduled events
(sports at the stadiums/arenas, permitted special events) near the cell.

## 4. Evaluation protocol

* **Chronological split.** Train windows end before 2025-01-01; validation windows
  (early stopping, dispersion, κ/bandwidth) end before 2025-07-01 − 72 h; the test
  period runs from 2025-07-01 to the *maturity cutoff* (data end − 30 days).
  Windows after the cutoff are scored separately as **provisional**.
* **Identical windows.** Every model is scored on exactly the same hourly issue
  times × all forecast cells.
* **Metrics.** Capture of incidents in the top 5/10/20 % of area (equal-area cells,
  fractional tie handling), predictive accuracy index (PAI), lift over the hotspot
  and seasonal baselines, Poisson deviance, NB negative log-likelihood, MAE/RMSE,
  Brier score and expected calibration error of P(N ≥ 1), calibration-in-the-large,
  per-precinct and per-month stability, metrics restricted to windows with major
  events, and next-incident ranking (`hit_topK`: share of incidents in the K
  highest-ranked cells).
* **Uncertainty.** 90 % intervals by day-block bootstrap; model differences by
  paired day-block bootstrap on identical windows.
* **Ablations.** LightGBM is re-trained with each context group removed (weather,
  events, transit, calls, fire, static, other categories, all context). Lag
  sensitivity re-trains with crime availability lags of 0/24/72/168 h (0 h is a
  hypothetical instant-publication counterfactual).

## 5. Prospective evaluation

1. Each cycle issues forecasts whose earliest window starts at the next whole hour
   after the wall-clock issue time.
2. Predictions are written once, made read-only and their SHA-256 is stored with
   the model version, data snapshot ids and per-source feature cutoffs.
3. New snapshots are reconciled into the offense store (`first_seen_at_utc`,
   changed/removed records retained with a change log).
4. Every window is (re)scored against each new snapshot: *provisional* until
   `window_end + 30 days`, then *mature* and final. Absence of an incident in an
   immature snapshot is never treated as final.
5. Historical **replay** forecasts (as-of reconstructions with pre-test models) use
   the same freeze → score path but are always labelled `replay`.

## 6. Ethical scope

Area-level only; no person-level features or outputs; no enforcement
recommendations. Reported crime is shaped by reporting and policing practices, so
forecasts can reproduce those biases; the dashboard states this, shows uncertainty
and suppresses small historical counts (< 3 per cell).
