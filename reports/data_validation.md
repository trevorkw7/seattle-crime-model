# Data validation report

Generated: 2026-10-14T19:30:26.972027+00:00 from the snapshots listed below. All numbers are computed by `python -m seacrime.validate` on the actual downloads.

## SPD Crime Data (tazs-3rd5)

* Snapshot `20261014T185811Z` fetched 2026-10-14T18:58:11.272844+00:00; publisher `rowsUpdatedAt` 2026-10-14T15:08:28+00:00.
* Rows (offenses): **1,568,054**; distinct report numbers: 1,380,758; offenses per report (mean/median/max): {'mean': 1.14, '50%': 1.0, 'max': 18.0}.
* Report-time range (local): 1975-12-16 15:00:00 → 2026-10-14 00:08:05; 355 records dated before 2008 (legacy/erroneous dates).
* Identifiers: `offense_id` duplicates 6; duplicate `(report_number, offense_id)` keys 6 (same key, differing content; resolved deterministically), exact duplicate rows 0.
* Time zones: Timestamps are floating local time (America/Los_Angeles), no offset; converted to UTC. DST-ambiguous report times (fall-back hour): 222 (assigned to the first, PDT occurrence); nonexistent spring-forward times: 0 (shifted forward).
* Timestamp precision: share of report times with non-zero seconds since 2020: 0.984 (Mark43 RMS records to the second). Offense start times at exactly 00:00:00 since 2019-06: 0.091 (date-only entries) — the reason the primary target uses report time.
* Report − offense delay (hours) quantiles since 2019-06: {'0.1': 1.0, '0.25': 2.1, '0.5': 12.5, '0.75': 60.5, '0.9': 244.1, '0.95': 641.6, '0.99': 5446.0}; report before offense: 0.0029.
* Location status (all years): {'ok': 1166409, 'redacted': 228134, 'unknown': 173210, 'out_of_area': 295}.
* Location status share by offense category:

| category | ok | redacted | unknown (-1,-1) | out_of_area |
|---|---|---|---|---|
| ALL OTHER | 0.652 | 0.256 | 0.092 | 0.0001 |
| PROPERTY CRIME | 0.850 | 0.013 | 0.137 | 0.0003 |
| VIOLENT CRIME | 0.684 | 0.266 | 0.050 | 0.0000 |

* Location status share by report year:

| year | ok | redacted | unknown | out_of_area |
|---|---|---|---|---|
| 2015 | 0.652 | 0.139 | 0.209 | 0.0003 |
| 2016 | 0.648 | 0.148 | 0.204 | 0.0002 |
| 2017 | 0.824 | 0.166 | 0.010 | 0.0003 |
| 2018 | 0.821 | 0.168 | 0.011 | 0.0002 |
| 2019 | 0.826 | 0.164 | 0.009 | 0.0007 |
| 2020 | 0.868 | 0.127 | 0.005 | 0.0000 |
| 2021 | 0.857 | 0.137 | 0.005 | 0.0000 |
| 2022 | 0.864 | 0.132 | 0.003 | 0.0002 |
| 2023 | 0.854 | 0.143 | 0.003 | 0.0000 |
| 2024 | 0.859 | 0.137 | 0.004 | 0.0001 |
| 2025 | 0.837 | 0.159 | 0.004 | 0.0000 |
| 2026 | 0.813 | 0.168 | 0.019 | 0.0000 |

* Coordinate uncertainty: 42,056 distinct coordinates; median 6 records per coordinate (p99 366). SPD blurs offense locations to the hundred block (block_address e.g. '24XX BLOCK OF 4TH AVE S'); coordinates are block centroids, so positional uncertainty is roughly +/-50-150 m along the street. Some incidents with unknown location are coded as (-1, -1) and others as REDACTED (sensitive offense types).
* Temporal gaps (report day, since 2019-06): 0 empty days of 2693; longest empty runs []; 6 anomalously low days (<30% of rolling median), e.g. ['2020-06-06', '2020-06-07', '2020-06-08', '2020-06-09', '2020-06-14', '2022-12-23'].
* **Publication completeness (single-snapshot estimate)** — ratio of reports present vs. same-weekday expectation, by age of report day: {'0-1d': 0.005, '1-3d': 1.021, '3-7d': 1.18, '7-14d': 0.97, '14-21d': 1.508, '21-35d': 1.235}. Method: Estimate how complete recent report-days are in a single snapshot by     comparing each recent day's count with the same weekday's mean over weeks     5-12 before the snapshot (assumed mature). Single-snapshot estimate; the     prospective engine measures true publication delays across snapshots.
  The most recent day is heavily incomplete; daily counts are also volatile (batch approval), so feature availability is set conservatively to 72 h and scoring maturity to 30 days.
* Reconciliation: [{'snapshot_id': '20261014T185811Z', 'new': 1568048, 'changed': 0, 'removed': 0, 'initial_backfill': True}]

## SPD Call Data (33kz-ixgy)

* Snapshot `20261014T185908Z` fetched 2026-10-14T18:59:08.575620+00:00; raw rows 4,350,038 → processed rows 2,471,750.
* Time range (UTC): ['2019-06-01 07:00:06+00:00', '2026-10-11 06:57:38+00:00']; latest record age at fetch: **84.03 h** (observed publication delay).
* Located share: 0.754; duplicate keys after processing: 0.
* Daily gaps: 0 empty days of 2689; longest []; anomalously low days: 0.

## Seattle Real Time Fire 911 Calls (kzjm-xkqj)

* Snapshot `20261014T185811Z` fetched 2026-10-14T18:58:11.284283+00:00; raw rows 844,581 → processed rows 844,581.
* Time range (UTC): ['2019-06-01 07:01:00+00:00', '2026-10-14 18:48:00+00:00']; latest record age at fetch: **0.17 h** (observed publication delay).
* Located share: 0.997; duplicate keys after processing: None.
* Daily gaps: 0 empty days of 2693; longest []; anomalously low days: 1.

## Contextual sources

```json
{
 "weather_hourly": {
  "rows": 54704,
  "range": [
   "2019-06-01 00:00:00+00:00",
   "2025-08-27 07:00:00+00:00"
  ],
  "missing_hours": 0,
  "null_share": {
   "ts_utc": 0.0,
   "temp_c": 0.0001,
   "dewpoint_c": 0.0001,
   "rel_humidity": 0.0001,
   "wind_ms": 0.0001,
   "precip_mm": 0.0052,
   "source": 0.0001,
   "temp_source_station": 0.0001,
   "precip_source_station": 0.0052
  }
 },
 "events": {
  "rows": 3209,
  "by_category": {
   "permit": 1733,
   "mlb": 604,
   "nhl": 270,
   "mls": 160,
   "wnba": 124,
   "holiday": 101,
   "nfl": 83,
   "nwsl": 69,
   "ncaaf": 53,
   "soccer_intl": 12
  },
  "range": [
   "2019-01-01 08:00:00+00:00",
   "2027-12-25 08:00:00+00:00"
  ]
 },
 "transit_stops": {
  "rows": 3078
 },
 "transit_stop_how": {
  "rows": 407952
 },
 "census_tracts": {
  "rows": 177
 },
 "zoning": {
  "rows": 3627
 },
 "streets": {
  "rows": 23888
 },
 "city_limits": {
  "rows": 1
 },
 "spd_beats": {
  "rows": 55
 },
 "weather_forecast_hourly": {
  "rows": 570185
 }
}
```

## Leakage controls derived from this report

* Feature availability lags: crime 72 h after report time, SPD calls 96 h, Fire 911 1 h (see configs/default.yaml).
* Backfilled rows have unknown true publication times (`backfill=True` in the store); live operation records `first_seen_at_utc` per snapshot.
* Scores for windows younger than 30 days are labelled provisional and recomputed on every new snapshot.
