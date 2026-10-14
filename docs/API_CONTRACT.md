# Dashboard data contract (v1)

The dashboard is a static-exportable Next.js app. It reads JSON documents from a
base URL (`NEXT_PUBLIC_DATA_BASE`, default `/data`). The same documents are written
to `web/public/data/` by `python -m seacrime.export` and are also served by the
FastAPI app (`seacrime.api`) under `/data/...`. All timestamps are ISO‑8601 UTC
(`...Z`). Cell ids are strings `"r{row}_c{col}"`.

Every document carries a `"provenance"` object:

```json
{"kind": "actual" | "provisional" | "demo",
 "generated_at": "2026-10-14T19:00:00Z",
 "note": "free text shown to the user"}
```

* `actual` — computed from real downloaded data and real model runs.
* `provisional` — real, but the evaluation window has not matured (late reports
  may still arrive); must be visually flagged.
* `demo` — synthetic/fixture data. The UI must show a prominent "DEMO DATA" badge.

## `meta.json`
```json
{
  "provenance": {...},
  "categories": [{"id": "property", "label": "Property crime"}, ...],
  "horizons_h": [1, 3, 6, 24],
  "models": [{"id": "lgbm", "label": "LightGBM", "version": "lgbm-2026-10-14T1900Z-ab12cd",
              "family": "gbm", "trained_at": "...", "is_default": true}],
  "grid": {"cell_size_m": 500, "n_cells": 912, "crs": "EPSG:32610",
           "bbox": [-122.44, 47.495, -122.235, 47.735]},
  "event_time": "report",
  "disclaimers": ["...", "..."]
}
```

## `grid.geojson`
GeoJSON FeatureCollection (WGS84). One Polygon feature per cell with properties
`{"cell_id": "r10_c12", "row": 10, "col": 12, "neighborhood": "CAPITOL HILL" | null,
  "in_city": true}`.

## `forecasts/index.json`
```json
{"provenance": {...},
 "latest": {"lgbm": {"property": "fc_20261014T1900Z_lgbm_property", ...}, ...},
 "items": [{"forecast_id": "fc_...", "issued_at": "...", "model_id": "lgbm",
            "category": "property", "status": "frozen" | "scored_provisional" | "scored_mature",
            "provenance_kind": "actual"}]}
```

## `forecasts/<forecast_id>.json`
```json
{
  "provenance": {...},
  "forecast_id": "fc_20261014T1900Z_lgbm_property",
  "issued_at": "2026-10-14T19:00:00Z",          // frozen before window start
  "model_id": "lgbm", "model_version": "lgbm-...", "category": "property",
  "data_versions": {"spd_crime": "20261014T185500Z", "fire_911": "...", ...},
  "feature_cutoffs": {"spd_crime": "2026-10-11T19:00:00Z", ...},  // latest data actually usable
  "columns": ["expected", "p_any", "relative_risk", "lo90", "hi90"],
  "horizons": {
    "1":  {"window_start": "...", "window_end": "...", "total_expected": 12.3,
           "cells": {"r10_c12": [0.21, 0.19, 1.8, 0, 1], ...}},
    "3":  {...}, "6": {...}, "24": {...}
  },
  "score": null | {"maturity": "provisional" | "mature", "scored_at": "...",
                   "per_horizon": {"1": {"observed_total": 10, "capture_at_10pct": 0.31, ...}},
                   "observed": {"1": {"r10_c12": 1, ...}}}
}
```
`lo90`/`hi90` are 90% predictive intervals on the count (may be `null` if
unsupported by the model).

## `history/<category>.json`
Aggregated historical counts (never individual incidents). Cells with counts
below `min_count` (k-anonymity style suppression, default 3) are reported as
`null`.
```json
{"provenance": {...}, "category": "property", "min_count": 3,
 "periods": {"last_7d": {"start": "...", "end": "...", "cells": {"r10_c12": 14, ...}},
             "last_30d": {...}, "last_365d": {...}},
 "hour_of_week_citywide": [168 numbers, local time, Monday 00:00 first]}
```

## `evaluation/summary.json`
```json
{
  "provenance": {...},
  "backtest": {
    "test_window": {"start": "...", "end": "..."},
    "rows": [{"model_id": "lgbm", "category": "property", "horizon_h": 6,
              "capture_5": 0.21, "capture_10": 0.34, "capture_20": 0.52,
              "pai_10": 3.4, "lift_vs_seasonal_10": 1.08, "poisson_deviance": 0.123,
              "nll": 0.21, "mae": 0.05, "rmse": 0.2, "brier": 0.04, "ece": 0.003,
              "ci": {"capture_10": [0.33, 0.35]}}]
  },
  "prospective": {
    "rows": [... same fields ..., "maturity": "provisional" | "mature", "n_forecasts": 12],
    "timeseries": [{"issued_at": "...", "model_id": "lgbm", "category": "property",
                    "horizon_h": 24, "capture_10": 0.3, "maturity": "provisional"}]
  },
  "ablations": [{"name": "no_weather", "model_id": "lgbm", "category": "property",
                 "horizon_h": 6, "capture_10": ..., "delta_vs_full": ...}],
  "calibration": [{"model_id": "lgbm", "category": "property", "horizon_h": 6,
                   "bins": [{"p_mean": 0.05, "obs_rate": 0.048, "n": 10234}]}],
  "stability": {"by_precinct": [...], "by_month": [...]}
}
```

## `health.json`
```json
{"provenance": {...},
 "sources": [{"name": "spd_crime", "snapshot_id": "...", "fetched_at": "...",
              "publisher_updated_at": "...", "latest_event_ts": "...",
              "lag_hours": 18.2, "status": "ok" | "stale" | "missing"}],
 "models": [{"model_id": "lgbm", "version": "...", "last_forecast_at": "...",
             "status": "ok" | "degraded" | "missing", "notes": "..."}],
 "jobs": [{"name": "hourly_forecast", "last_run": "...", "last_status": "ok" | "error",
           "message": "..."}]}
```
