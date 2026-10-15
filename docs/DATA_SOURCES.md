# Data sources

Core public-safety sources are documented here; contextual sources (weather,
events, transit, boundaries, news, traffic) are in
[DATA_SOURCES_CONTEXT.md](DATA_SOURCES_CONTEXT.md). Every figure below was
measured on the snapshots downloaded on 2026-10-14 (see
[reports/data_validation.md](../reports/data_validation.md)).

## SPD Crime Data: 2008-Present — primary target

| | |
|---|---|
| URL / API | https://data.seattle.gov/d/tazs-3rd5 · SODA `https://data.seattle.gov/resource/tazs-3rd5.json` (paged by `:id`) |
| Coverage | report dates 2008 → present (355 legacy records dated earlier); City of Seattle |
| Rows | 1,568,054 offenses, 1,380,758 report numbers (mean 1.14 offenses/report) |
| Key columns | `report_number` (text, report id), `offense_id` (text), `report_date_time` (floating local timestamp), `offense_date` (offense start, floating local), `offense_category` (PROPERTY CRIME / VIOLENT CRIME / ALL OTHER), `nibrs_offense_code(_description)`, `nibrs_crime_against_category`, `block_address`, `latitude`/`longitude` (text), `beat`/`sector`/`precinct`, `neighborhood` (MCPP), `reporting_area`, `census_block_2020` |
| Timestamps | floating America/Los_Angeles without offset; report time to the second since 2020; offense start date-only for ~9 % |
| Update frequency | once per 24 h (publisher `rowsUpdatedAt` 2026-10-14 15:08 UTC); entire dataset is replaced (Socrata `:created_at` identical for all rows), so historical publication times are **not** recoverable |
| Observed delay | only finalized (UCR-approved) reports; the latest report day was ~0.5 % complete at fetch; daily counts in the following 1–3 weeks fluctuate strongly (batch approvals) |
| Spatial precision | hundred-block centroid (42,056 distinct coordinates); unknown locations coded `-1,-1`; sensitive offenses `REDACTED` (≈27 % of violent offenses) |
| Identifiers | `(report_number, offense_id)` is unique except 6 conflicting duplicates (resolved deterministically); incidents are counted per `report_number` |
| License | Public Domain (dataset metadata) |
| Leakage risks | (1) recent reports missing at forecast time → 72 h feature lag; (2) reclassification/removal over time → reconciliation store with change log; (3) offense-time targets reported late → features always from report-time data |
| Feasibility | historical: full back to 2008 (RMS change May 2019 → training from mid-2019); ongoing: daily snapshot + reconciliation records true `first_seen_at_utc` |

## SPD Call Data (CAD calls for service)

| | |
|---|---|
| URL / API | https://data.seattle.gov/d/33kz-ixgy · SODA `.../resource/33kz-ixgy.json` |
| Coverage | 2009-06 → present (we ingest ≥ 2019-06-01: 4,350,038 rows → 2,471,750 CAD events) |
| Key columns | `cad_event_number`, `cad_event_original_time_queued`, `initial_call_type`, `final_call_type`, `priority`, `call_type_indicator`, `event_group`, `dispatch_latitude/longitude`, `dispatch_beat`, `dispatch_neighborhood`, response-time fields |
| Granularity | one row per dispatched call sign → de-duplicated by `cad_event_number` |
| Update frequency / delay | daily; newest event 84 h old at fetch → 96 h feature lag |
| Spatial precision | dispatch coordinates for 75 % of events (the publisher withholds some locations) |
| License | Public Domain |
| Leakage risks | final call type is assigned after the call; only counts by queue time are used, with the 96 h lag |

## Seattle Real Time Fire 911 Calls

| | |
|---|---|
| URL / API | https://data.seattle.gov/d/kzjm-xkqj · SODA `.../resource/kzjm-xkqj.json` |
| Coverage | 2003-11 → present (we ingest ≥ 2019-06-01: 844,581 dispatches) |
| Columns | `incident_number`, `datetime` (floating local), `type` (e.g. Aid Response, Medic Response, Rubbish Fire), `latitude`, `longitude`, `address` |
| Update frequency / delay | every ~5 min; newest record 10 min old at fetch → 1 h feature lag |
| Spatial precision | address-level geocode (99.7 % located) |
| License | Public Domain |
| Use | the only near-real-time public activity signal; incremental fetch each hourly cycle |

## Derived and secondary products

* `data/processed/spd_offenses.parquet` — reconciled offense store (`first_seen_at_utc`, `last_seen_at_utc`, `present_in_latest`, `n_changes`, `backfill`).
* `data/processed/spd_offense_changes.parquet` — field-level change log between snapshots.
* `data/processed/cubes_<size>m/` — hourly count cubes on the lattice (rebuildable).
* Every raw snapshot directory contains `manifest.json` (query, fetch time, publisher version, row count, SHA-256 per part) — these snapshot ids are the *data versions* stored with each forecast.

## Blocked or not used

See DATA_SOURCES_CONTEXT.md for sources requiring credentials (Ticketmaster/SeatGeek
event APIs, WSDOT traveler API, OneBusAway real-time) and how to enable them.
