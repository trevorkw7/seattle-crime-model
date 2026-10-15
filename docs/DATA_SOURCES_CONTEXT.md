# Contextual data sources (weather, events, transit, geography, news)

Companion to `docs/DATA_SOURCES.md` (crime / calls / fire). Everything below was
observed while building and running the ingesters on **2026-10-14 (UTC)** from this
environment (egress through a shared proxy IP). Row counts and date ranges are the
ones actually downloaded in that run; re-running will move the "latest" dates.

Code: `src/seacrime/ingest/{weather,events,gtfs,boundaries,news}.py` plus the shared
helper `src/seacrime/ingest/_context_common.py` (HTTP retry/backoff, immutable raw
snapshots, timezone conversion). Tests: `tests/test_context_ingest.py` (offline).

## Conventions

* **Raw snapshots** — every run writes `data/raw/<source>/<YYYYmmddTHHMMSSZ>/` with the
  payloads exactly as received (gzip with `mtime=0`, so stored bytes are
  deterministic; GTFS zips stored as-is) and a `manifest.json`: URL + query of every
  request, sha256 of the received bytes, `fetched_at_utc` (= our availability time),
  row counts, licence notes, cross-checks, and a `blockers` list (HTTP status + body
  excerpt for every request that failed). Snapshot directories are never modified.
* **Processed outputs** — `data/processed/*.parquet`; geographic layers are
  GeoParquet in EPSG:4326. All timestamps are tz-aware UTC.
* **Floating local times** (Seattle open data: dates/times without offset) are
  converted with `local_to_utc()`: nonexistent spring-forward times are shifted
  forward (`02:30 → 03:00 PDT`); ambiguous fall-back times (`01:00–01:59` on the
  first Sunday of November) are resolved to the **first occurrence (PDT)** by
  default (`ambiguous="earliest"`; `"latest"`/`"NaT"` available). The data carry no
  DST flag, so this choice cannot be verified from the data. In the sources below
  only the permit and holiday *dates* are floating local (converted at local
  midnight, never ambiguous); weather, MOS, ESPN, MLB, GDELT are UTC and RSS/NWS
  carry explicit offsets.
* CLI: `python -m seacrime.ingest.run weather events gtfs boundaries` (and `news`
  once wired). **Run `boundaries` before `events`** — permits get neighborhood
  centroid coordinates from `data/processed/neighborhoods.parquet`.

## Summary of what was downloaded

| Output | Rows | Coverage (UTC) | Source(s) |
|---|---|---|---|
| `weather_hourly.parquet` | 64,588 hours | 2019-06-01 00:00 → 2026-10-13 03:00 | NCEI ISD (to 2025-08-27 07:00) + NCEI GHCNh (after) |
| `weather_forecast_hourly.parquet` | 570,223 (GFS MOS 225,855; LAMP 344,368) | runs 2019-06-01 00Z → 2026-10-14 19Z | NWS MOS via Iowa Environmental Mesonet |
| `weather_forecast_nws_live.parquet` | 156 per fetch | +6.5 days from fetch | api.weather.gov (live; appends) |
| `events.parquet` | 3,209 | 2019-01-01 → 2027-12-25 | MLB Stats API, ESPN, Seattle permits, Nager.Date |
| `transit_stops.parquet` | 3,078 stops | representative week 2026-10-19 | KCM, Sound Transit, CT, PT, WSF, Monorail GTFS |
| `transit_stop_how.parquet` | 407,952 (non-zero stop×hour) | idem | idem |
| `transit_alerts.parquet` | 99 (KCM 88, ST 11) | live since 2026-10-14 19:30 | GTFS-realtime alerts |
| `city_limits.parquet` | 1 polygon (238.03 km²) | current | Seattle GIS |
| `census_tracts.parquet` | 177 tracts, pop 737,027 | 2020 census | Seattle GIS + Census TIGERweb |
| `zoning.parquet` | 3,627 polygons | current | Seattle GIS |
| `streets.parquet` | 23,888 segments | current | Seattle GIS (SDOT) |
| `spd_beats.parquet` | 55 beats | current | Seattle GIS |
| `neighborhoods.parquet` | 94 | current | Seattle GIS (Neighborhood Map Atlas) |
| `news_items.parquet` | 332 items (2 runs) | published 2026-08-21 → 2026-10-14 | 12 RSS/Atom feeds incl. Google News |
| `news_signals.parquet` | 332 | idem | keyword extractor |

Raw storage after all runs: ~67 MB on disk (weather 30 MB, GTFS 22 MB, events 8.3 MB,
boundaries 5.7 MB, rest < 1 MB). Bytes received per full run: weather 166 MB,
events 80 MB (ESPN JSON is verbose), GTFS 23 MB, boundaries ~15 MB.

---

## 1. Weather — `seacrime.ingest.weather`

### 1a. NOAA NCEI Integrated Surface Database (`global-hourly`) — observations

* **API**: `https://www.ncei.noaa.gov/access/services/data/v1?dataset=global-hourly&stations=<USAF-WBAN>&startDate=…&endDate=…&dataTypes=TMP,DEW,WND,AA1,AA2,AA3,AA4&format=csv`
  — one request per station-year, no key.
* **Stations**: KSEA Seattle-Tacoma Intl `72793024233` (primary; ~20 km south of
  downtown, outside city limits) and KBFI Boeing Field `72793524234` (gap filler;
  on the southern city boundary).
* **Observed coverage**: 2019-06-01 → **2025-08-27 07:00 UTC for both stations**,
  nothing after (requests for 2025-08-20..09-10 return data only up to that hour).
  NCEI's successor product GHCNh (1b) continues the record.
* **Columns used**: `DATE` (UTC), `REPORT_TYPE` (FM-15 routine METAR at HH:53,
  FM-16 SPECI, FM-12 SYNOP, SOD/SOM summaries), `TMP`/`DEW` (°C×10, quality
  flag), `WND` (dir, type, speed m/s×10), `AAn` (precip period h, depth mm×10).
  Quality codes 2/3/6/7 are dropped; 9999 = missing.
* **Hourly rules** (`isd_to_hourly`): `ts_utc` = hour start; the FM-15 METAR in the
  hour is used for temperature/dew point/wind (fallback: mean of other report
  types); `precip_mm` = the METAR's own 1-hour amount (`AAn` with period 01) —
  SPECI partial accumulations and 6-h SYNOP amounts are ignored; a METAR at
  HH:53 is assigned to hour HH. `rel_humidity` is computed from T and Td (Magnus).
* **Counts**: 84,006 KSEA and 67,962 KBFI reports → 54,670 / 54,659 station-hours.
* **Validation**: monthly sums of hourly precip vs GHCN-Daily PRCP (1c):
  KSEA 4,940.3 vs 4,969.2 mm over 64 complete months (max monthly rel. diff 15%);
  KBFI 2,944.5 vs 2,965.2 mm over 66 months (max 24%). GFS MOS 24 h temperature
  forecasts vs these observations: MAE 1.29 °C (n=10,745) — confirms time alignment.
* **Licence**: U.S. Government work (no restrictions for U.S. stations).
* **Spatial precision**: point stations; one citywide series (no intra-city variation).

### 1b. NOAA NCEI GHCNh (Global Historical Climatology Network – hourly)

* **Files**: `https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/access/by-year/<YYYY>/psv/GHCNh_<USW000…>_<YYYY>.psv`
  (pipe-separated, 329 columns; KSEA 2026 file ≈ 35 MB raw). Stations
  `USW00024233` (KSEA), `USW00024234` (KBFI). No key. Only years ≥ the ISD end
  year (2025, 2026) are downloaded.
* **Observed coverage**: through **2026-10-13 03:00 UTC** at fetch time
  2026-10-14 19:44 UTC → **~40 h publication lag**. Some gaps: 128 citywide hours
  have no temperature from either station (mostly 2025-08-30 → 2025-09-01).
* **Overlap with ISD** (2025-01-01 → 2025-08-27): temperature and precipitation are
  identical (5,559 KSEA / 5,590 KBFI overlapping routine hours; max abs diff 0).
* **Precipitation encoding changes after the ISD cutoff**: GHCNh then carries
  `precipitation` on only ~11–23% of routine METAR rows (values include explicit
  zeros/traces). Following METAR practice (the precipitation remark group is
  omitted when none fell), routine hours without a value are **zero-filled and
  flagged** (`precip_zero_filled`, 8,146 hours). Values whose own
  `precipitation_Report_Type` is not `FM15` (e.g. `FM94_1` in the KSEA 2026 file)
  are rejected — accepting them inflated Apr–Sep 2026 monthly totals 2–5×.
  Validation vs GHCN-Daily after these rules: KSEA 961.3 vs 974.4 mm over 12
  months (max monthly rel. diff 8.8%); KBFI 651.4 vs 676.3 mm (max 12.8%).
* **Feasibility**: yearly files are re-published continuously → ongoing collection
  works, but with ~1.5-day lag it is **not usable as a live feature** for 1–24 h
  forecasts (see live sources below).

### 1c. NOAA GHCN-Daily (`daily-summaries`) — validation only

* Same Access Data Service, `dataset=daily-summaries&dataTypes=PRCP&units=metric`.
  Last day available at fetch: 2026-10-11 (~3-day lag). Days are local standard
  time; compared at monthly resolution only. Not written to processed outputs.

### Processed: `weather_hourly.parquet`

`ts_utc` (hour start, UTC), `temp_c`, `dewpoint_c`, `rel_humidity` (%), `wind_ms`,
`precip_mm` (1-h total), `source` (e.g. `noaa_ncei:isd:KSEA`, `noaa_ncei:ghcnh:KSEA`,
or `…KBFI+…KSEA` when a column was gap-filled from KBFI), `temp_source`,
`precip_source`, `precip_zero_filled`. Rows: 64,588 hours (full hourly index; 128
hours all-NaN, 428 hours without precip). Source mix: ISD KSEA 52,930 h,
GHCNh KSEA 9,752 h, KBFI-filled 1,778 h. Annual totals (mm): 2019 (Jun–Dec) 500.0,
2020 1,055.3, 2021 1,114.6, 2022 1,044.9, 2023 883.3, 2024 829.6, 2025 836.3,
2026 (to 10-13) 532.8.

**Leakage**: observed weather for the target hour is hindsight. For an honest
backtest use the archived forecasts (1d) with `issued_at_utc <= issue time`, or
observations lagged by at least the publication delay (~40 h for GHCNh).

### 1d. NWS MOS / LAMP point forecasts (Iowa Environmental Mesonet archive)

* **API**: `https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py?station=KSEA&model=GFS|LAV&sts=…&ets=…&format=csv`
  (no key; GFS by quarter, LAMP by month). IEM answers bursts with HTTP 429; the
  fetcher pauses 2 s between requests and backs off on 429 (all requests
  eventually succeeded).
* **Models / observed coverage**:
  * `GFS` (GFS MOS, "MAV"): runs 00/06/12/18Z every day from 2019-06-01 00Z to
    2026-10-14 12Z — 10,755 runs, 225,855 rows, valid times every 3 h at leads
    6–72 h.
  * `LAV` (GFS LAMP): IEM archive starts **2020-07-11 18Z**; historically only the
    00/06/12/18Z runs are retained (hourly runs exist only for the most recent
    ~week) — 9,210 runs, 344,368 rows, hourly valid times at leads 1–38 h.
* **Columns**: `model`, `run_utc` (model cycle), `issued_at_utc`
  (= run + assumed delay: **GFS +6 h, LAMP +1.5 h**), `valid_ts_utc`, `lead_h`,
  `temp_c`, `dewpoint_c`, `rel_humidity`, `wind_ms` (from knots), `wind_dir_deg`,
  `cloud_cover`, `pop_6h_pct` / `pop_12h_pct` (probability of ≥0.01 in during the
  6/12 h ending at `valid_ts_utc`), `qpf_6h_cat` (GFS MOS QPF category, not mm),
  `ppo_pct` (LAMP probability of precipitation on the hour), `precip_type`.
* **Publication delay (observed bounds, not exact)**: at 19:14 UTC the 18Z LAMP
  run was in the archive while the 18Z GFS MOS was not; at ~19:40 UTC the 19Z
  LAMP run was present; the 12Z GFS MOS was present by 19:03 UTC. The assumed
  delays above are conservative choices consistent with these observations.
* **Sanity**: LAMP 6-h temperature MAE vs observations 1.07 °C (n=9,108).
* **Licence**: NWS products are public domain; IEM provides the archive freely.
* **Leakage**: use rows with `issued_at_utc <= forecast issue time` only; the
  GFS MOS `valid_ts_utc` grid is 3-hourly — interpolate to hours.
* **Spatial precision**: point forecast for KSEA only.

### 1e. NWS API — live hourly forecast (`fetch_nws_forecast(cfg)`)

* `https://api.weather.gov/points/47.6062,-122.3321` → grid **SEW/125,68**;
  `…/gridpoints/SEW/125,68/forecast/hourly?units=si` (156 hourly periods, ~6.5
  days) + `…/gridpoints/SEW/125,68` for QPF (6-h amounts, spread evenly per hour).
  No key; a User-Agent is required.
* Returns `valid_ts_utc, temp_c, dewpoint_c, rel_humidity, precip_prob, wind_ms,
  wind_dir, is_daytime, short_forecast, qpf_mm, issued_at_utc (updateTime),
  generated_at_utc, fetched_at_utc, lead_h, source, grid`. Observed: at 19:48 UTC
  `updateTime` was 18:26 UTC. With `store=True` (default) the raw JSON is
  snapshotted under `data/raw/nws_forecast/` and rows are appended to
  `weather_forecast_nws_live.parquet` (dedup on issued_at × valid_ts) — the only
  way to build a NWS forecast archive (NWS keeps no history in this API).
* **Spatial precision**: 2.5 km grid cell over downtown.

### 1f. Open-Meteo — probed, not ingested

* `archive-api.open-meteo.com` (ERA5 reanalysis) returned 200 (not used; NCEI
  observations are preferred).
* `historical-forecast-api.open-meteo.com` and `previous-runs-api.open-meteo.com`:
  **HTTP 429 `{"reason":"Daily API request limit exceeded. Please try again
  tomorrow."}`** at 19:01–19:02 UTC, 200 at 19:2x UTC, 429 again at 19:35 and
  19:44 UTC. The free tier quota is per IP and this egress IP is shared, so it is
  unreliable here. They are also not ingested because values are not tagged with
  a model run / issue time (only "previous day N"), so leakage-safe issue times
  would be approximate. The fetcher records the probe status in the manifest.

---

## 2. Events — `seacrime.ingest.events`

Output `events.parquet` columns: `event_id`, `name`, `category`
(`mlb, nfl, mls, wnba, nhl, ncaaf, nwsl, soccer_intl, permit, holiday`),
`competition`, `season`, `is_postseason`, `home_team`, `away_team`, `venue`,
`venue_city`, `lat`, `lon`, `location_text`, `start_utc`, `end_utc`, `all_day`,
`time_tbd`, `attendance`, `attendance_kind`, `status`
(`scheduled/final/postponed/cancelled/suspended/delayed/pending/…`), `status_raw`,
`schedule_known_at_utc`, `actual_end_utc`, `source`, `source_id`, `fetched_at_utc`.

| category | rows | first start | last start | postseason | notes |
|---|---|---|---|---|---|
| mlb | 604 | 2019-03-26 | 2026-09-27 | 7 | T-Mobile Park incl. 2023 All-Star Game (47,159), 2020 games without fans; 3 postponed |
| nfl | 83 | 2019-08-09 | 2026-12-26 | 3 | incl. preseason; 2 cancelled (2020 preseason) |
| mls | 160 | 2019-03-03 | 2026-10-02 | 25 | Sounders: MLS + CONCACAF Champions Cup + Leagues Cup + US Open Cup |
| wnba | 124 | 2019-06-23 | 2026-09-24 | 5 | 2019 at Alaska Airlines Arena (UW); 2020 (bubble) and 2021 (Everett) correctly absent |
| nhl | 270 | 2021-10-24 | 2027-04-03 | 6 | Kraken from 2021-22; 2024 Winter Classic at T-Mobile Park (47,313); 2026-27 schedule included |
| ncaaf | 53 | 2019-08-31 | 2026-11-21 | 0 | UW at Husky Stadium + Apple Cup at Lumen Field |
| nwsl | 69 | 2021-08-29 | 2026-10-03 | 3 | Reign: games in Tacoma (2019–21) excluded by venue city |
| soccer_intl | 12 | 2025-06-16 | 2026-07-07 | 2 | FIFA Club World Cup 2025 (6) + FIFA World Cup 2026 (6) at Lumen Field |
| permit | 1,733 | 2019-01-15 | 2025-10-10 | – | Seattle Special Events Permits ("Special Event" type only) |
| holiday | 101 | 2019-01-01 | 2027-12-25 | – | federal + Washington State, observed and actual dates |

### 2a. MLB Stats API

* `https://statsapi.mlb.com/api/v1/schedule?sportId=1&venueIds=680&season=<Y>&gameType=S,E,R,F,D,L,W,A&hydrate=gameInfo`
  (venue 680 = T-Mobile Park); `…/venues/680?hydrate=location` for coordinates
  (47.591333, −122.33251). No key; 8 requests.
* Fields: `gameDate` (UTC scheduled start), status (`detailedState`), game type,
  teams, `gameInfo.attendance`, `firstPitch`, `gameDurationMinutes`,
  `delayDurationMinutes` → `actual_end_utc`. Postponed games stay as rows with
  status `postponed` and their make-up appears as a separate game (e.g. the
  2020-09-15/16 Giants games relocated to San Diego appear only as postponed).
* Terms: "Copyright MLB Advanced Media … use acknowledges agreement to
  gdx.mlb.com/components/copyright.txt" — research use; do not redistribute.
* Real time (results within minutes); schedules for future seasons are present.

### 2b. ESPN site API (undocumented, public)

* Team schedules `https://site.api.espn.com/apis/site/v2/sports/<sport>/<league>/teams/<team>/schedule?season=<Y>[&seasontype=1|2|3]`:
  NFL `sea`, MLS `9726` (+ `concacaf.champions`, `concacaf.leagues.cup`,
  `usa.open`), WNBA `sea`, NHL `sea` (ESPN season 2022 = 2021-22), college
  football `264`, NWSL `15363` (+ `usa.nwsl.cup`). Month scoreboards
  `…/soccer/fifa.cwc/scoreboard?dates=202506|202507`,
  `…/soccer/fifa.world/scoreboard?dates=202606|202607`. 164 ESPN payloads, no key;
  HTTP 400 for team/competition/season combinations that do not exist (skipped).
* **Home filter**: a game is kept when ESPN's venue city contains "Seattle"
  (fallback: known Seattle venue names when the city is missing) — not the
  home/away flag. Neutral-site games in Seattle (Apple Cup, Winter Classic, World
  Cup) are kept; nominal home games elsewhere (Storm in Everett, Reign in Tacoma)
  are dropped.
* `attendance` is ESPN's announced figure, set to null unless the game is final
  (future games report 0). Some values look copied between games (e.g. two 2019
  Sounders playoff games both 37,722) — treat as noisy.
* Terms: no published licence or terms for this API; research use only, do not
  redistribute raw payloads. The endpoint shape may change without notice.

### 2c. Seattle Special Events Permits (data.seattle.gov `dm95-f8w5`)

* Found via the Socrata catalog (`https://data.seattle.gov/api/catalog/v1?q=special%20event`;
  the suggested `api.us.socrata.com/api/catalog/v1?domains=data.seattle.gov&q=…`
  returned 0 results). Downloaded with the existing Socrata snapshotter
  (`data/raw/special_events_permits/`), 2,018 rows, licence **Public Domain**.
* Publisher `rowsUpdatedAt` = **2025-01-08 17:26 UTC** — the dataset has not been
  refreshed since; events run 2019-01-01 → 2025-10-10 but only applications filed
  before Jan 2025 exist (2025 has 45 Special Event rows vs 285–319 in 2022–2024).
* Columns: application date, permit status (Complete 642, Issued 666, Cancelled
  339, Withdrawn 37, Denied 5, In Process 43, On Hold 1 — among the 1,733 "Special
  Event" rows), permit type (Special Event / Valet Parking / Charter Vessel — only
  Special Event is kept), category (Community, Commercial, Athletic, Free Speech,
  Mixed Free Speech, Citywide), event name, **start/end dates (no times)**, park,
  neighborhood(s) (`;`-separated), council district, precinct, applicant-reported
  attendance (e.g. Pride Parade 2023: 308,650).
* Geocoding: no coordinates in the data. Rows naming exactly one neighborhood that
  matches the Neighborhood Map Atlas get that neighborhood's representative point
  (78% of rows); multi-neighborhood events (e.g. Pride Parade) have null lat/lon
  and keep `location_text`.
* `start_utc` = local midnight of the start date, `end_utc` = local midnight after
  the end date, `all_day=True`, `time_tbd=True`. Permit `status` "scheduled" means
  "issued" (not proof the event happened); "cancelled" covers
  Cancelled/Withdrawn/Denied.
* **Feasibility**: historical only (stale since Jan 2025); not usable for live
  forecasting unless the City resumes publishing.

### 2d. Holidays — Nager.Date

* `https://date.nager.at/api/v3/PublicHolidays/<Y>/US`, 2019–2027, no key. Kept:
  type `Public` and (`global` or counties include `US-WA`) — e.g. Native American
  Heritage Day (WA). Nager returns *observed* dates (e.g. Independence Day
  2021-07-05); for fixed-date federal holidays an extra "(actual date)" row is
  added (2021-07-04). 101 rows. Notable Seattle annual events (Pride, Seafair,
  Bite of Seattle, …) come from the permit dataset, not from a hand-typed list.

### 2e. Venue coordinates — OpenStreetMap Nominatim

* `https://nominatim.openstreetmap.org/search?q=<venue>, Seattle, Washington&viewbox=<bbox>&bounded=1`
  (1 request/s, 4 venues): Lumen Field (47.59535, −122.33164), Climate Pledge Arena
  (47.62190, −122.35399), Husky Stadium (47.65037, −122.30198), Alaska Airlines
  Arena (47.65218, −122.30212). ODbL. Venue-level precision (stadium centroid).

### Timing columns and leakage

* `end_utc` = `start_utc` + estimated duration (h): MLB 3.0, NFL 3.25, NCAAF 3.5,
  NHL 2.75, WNBA 2.25, MLS/NWSL/international soccer 2.0. `actual_end_utc` (MLB
  only, from game duration) is known only after the game.
* `schedule_known_at_utc` — conservative (late) estimate of when date+time were
  public; **not observed**, rule-based: regular/preseason fixtures start − 7 d
  (NFL & NCAAF − 5 d: flex scheduling and 6/12-day kickoff-time announcements);
  postseason/knockout games (incl. "if necessary") start − 12 h; holidays start −
  365 d; permits max(application date, start − 24 h) — 1 permit was applied for
  after its start date and is therefore never "known" before the event.
* `attendance` for games is post-event information — never use it as a
  forecast-time feature (use capacity/category/team instead). Permit attendance is
  an advance estimate.
* `time_tbd` flags games whose time was not set in the source (MLB 2, NCAAF 2).

### Blocked / not ingested (observed responses)

* **Ticketmaster Discovery API** (concerts): HTTP 401 `Failed to resolve API Key
  variable request.queryparam.apikey` — needs a key.
* **SeatGeek API**: HTTP 403 `Client is required - visit seatgeek.com/account/develop`.
* **Eventbrite** `v3/events/search`: HTTP 404 "The path you requested does not exist".
* **Seattle Street Closures** (`ium9-iqtc`): reachable, Public Domain, but only 111
  *current* rows of block parties/play streets/farmers markets; the dataset
  description says parades, festivals and construction are excluded — not ingested.
* Without a ticketing API, **arena/stadium concerts and non-sports events at
  Climate Pledge Arena, Lumen Field, T-Mobile Park, WaMu Theater, Seattle Center
  are missing** (e.g. a 2-night Garth Brooks run at Climate Pledge appears in the
  news feed but not in `events.parquet`).

---

## 3. Transit — `seacrime.ingest.gtfs`

### 3a. GTFS static feeds (current versions only)

| feed | URL used | Last-Modified | validity | version | stops in bbox | weekly departures in bbox |
|---|---|---|---|---|---|---|
| kcm King County Metro (+ Seattle Streetcar, KCM-operated ST Express) | `https://metro.kingcounty.gov/GTFS/google_transit.zip` (fallback `gtfs.sound.obaweb.org/prod/1_gtfs.zip`) | 2026-09-28 | 2026-09-28 → 2027-03-26 | FAL26-161.2 | 2,921 | 1,751,754 |
| st Sound Transit (Link 1/2/T, Sounder, rail shuttles) | `https://www.soundtransit.org/GTFS-rail/40_gtfs.zip` → redirects to `gtfs.sound.obaweb.org/prod/40_gtfs.zip` | 2026-10-05 | 2026-09-01 → 2027-03-26 | SC-Fall-2026.4 | 87 (42 with service in the week) | 52,512 |
| ct Community Transit (ST Express 510/512/513 etc.) | `https://www.communitytransit.org/docs/default-source/open-data/gtfs/current.zip` | 2026-10-06 | 2026-10-02 → 2026-12-11 | 20261002-20261211 | 20 | 1,112 |
| pt Pierce Transit (ST Express 59x) | `https://gtfs.sound.obaweb.org/prod/3_gtfs.zip` | 2026-10-05 | 2026-08-30 → 2026-12-12 | S1000138 | 46 | 17,197 |
| wsf Washington State Ferries | `https://gtfs.sound.obaweb.org/prod/95_gtfs.zip` | 2026-10-05 | 2026-10-01 → 2026-11-22 | – | 2 | 1,114 |
| monorail Seattle Center Monorail | `https://gtfs.sound.obaweb.org/prod/96_gtfs.zip` (GitHub source returned 403 in this sandbox) | 2026-10-05 | 2025-05-23 → 2026-11-30 | 2026-Apr-A | 2 | 2,344 |

* **Method** (`summarize_feed`): representative week = first Monday ≥ today inside
  the feed window whose Mon–Sun has no US federal holiday and no DST change → all
  feeds used **2026-10-19 … 2026-10-25**. Active services come from `calendar.txt`
  + `calendar_dates.txt` exceptions. Every stop_time with `pickup_type != 1` of an
  active trip counts one departure at local hour `HH` of its service day
  (`hour_of_week = (weekday*24 + HH) % 168`, Monday 00 = 0; times ≥ 24:00 roll
  into the next day, Sunday-night trips wrap into Monday 00–03). Stops with
  missing times inherit the previous timepoint. Stops are restricted to the config
  bbox + 0.01° buffer; `stop_times.txt` is streamed in 1M-row chunks (peak RSS
  512 MB). Sum over hours equals `trips_per_week` for every stop (checked).
* `transit_stops.parquet`: `stop_id` (`<feed>:<gtfs stop_id>`), `stop_name`, `lat`,
  `lon`, `parent_station`, `agency` (agencies serving the stop, `|`-joined),
  `modes` (`bus`, `light_rail`, `rail`, `ferry`, `monorail`…; GTFS route_type 0
  covers both Link and the Seattle Streetcar), `n_routes`, `trips_per_week`, `feed`,
  `rep_week_start`. `transit_stop_how.parquet`: `stop_id`, `hour_of_week`,
  `scheduled_departures` (zero combinations omitted). Busiest stop: 3rd Ave &
  Virginia St (7,427 departures/week).
* **Historical feasibility**: only current feeds are downloadable without keys.
  TransitFeeds (`transitfeeds.com`) answered 403; its S3 archive has old feeds but
  no listing (403 on list); the Mobility Database historical API needs a token.
  The profile is therefore a **static proxy** applied to all hours since 2019 —
  it does not reflect COVID-era cuts (2020–21), Link extensions (Northgate 2021,
  Lynnwood/2 Line 2024–25) or service changes; treat it as structural
  (where/when service is dense), not as a time-varying covariate.
* **Licences**: King County Metro developer terms of use; Sound Transit Open
  Transit Data terms; other agencies via the Puget Sound OneBusAway GTFS mirror.
* **Leakage**: none for the current week; anachronism risk for history (above).

### 3b. GTFS-realtime service alerts (`fetch_gtfs_rt_alerts(cfg)`, also run by `fetch_gtfs`)

* KCM `https://s3.amazonaws.com/kcm-alerts-realtime-prod/alerts.pb` (public) and
  Sound Transit `https://api.pugetsound.onebusaway.org/api/gtfs_realtime/alerts-for-agency/40.pb`
  (HTTP 200 **without a key**, observed; agencies 23 streetcar, 95 WSF, 96
  monorail answered 401; the OBA REST API `api/where/*` answered 401
  "permission denied"). Decoded with a small pure-Python protobuf reader (no
  protobuf package needed).
* `transit_alerts.parquet`: `feed, alert_id, cause, effect, header_text,
  description_text, url, route_ids, stop_ids, n_active_periods, active_start_utc,
  active_end_utc, feed_header_utc, first_seen_at_utc, last_seen_at_utc`. Observed:
  KCM 88 alerts (cause CONSTRUCTION 64, UNKNOWN 16, OTHER 5, MAINTENANCE 3;
  effect NO_SERVICE 42, STOP_MOVED 26, DETOUR 14, MODIFIED_SERVICE 3, OTHER 2,
  ACCESSIBILITY_ISSUE 1), ST 11 (no cause/effect set).
  `DEMONSTRATION` / `POLICE_ACTIVITY` causes exist in the spec but none were active.
* **Feasibility**: forward-only — no history is published; schedule the fetcher
  (e.g. every 15–60 min) to build a record. Leakage-safe time = `first_seen_at_utc`.
  Spatial precision: route/stop ids (join to `transit_stops`).

---

## 4. Geography — `seacrime.ingest.boundaries`

All layers from the City of Seattle GIS ArcGIS Online organisation
(`services.arcgis.com/ZOyb2t4B0UYuYNYH`, discovered with
`https://data-seattlecitygis.opendata.arcgis.com/api/search/v1/collections/dataset/items?q=…`)
and Census TIGERweb (`tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Census2020/MapServer`),
queried as GeoJSON in EPSG:4326 with 6-decimal precision and paged by 2,000.
Invalid polygons (city polygon, 26 zoning polygons) are repaired with
`shapely.make_valid(method="structure")`. Licence: City of Seattle open data
(public domain) and U.S. Census (public domain). These are **current vintages**.

| output | source layer | rows | columns |
|---|---|---|---|
| `city_limits` | `Seattle_Area_Polygon` ("City of Seattle") | 1 | name, source, area_km2 = **238.03** |
| `census_tracts` | `2020_Census_Tracts_Seattle` layer 3 (geometry, City's 177 tracts, shoreline-clipped) + TIGERweb layer 6 (`POP100`, `HU100`, `AREALAND`, `AREAWATER`) | 177 | GEOID, name, population (1,457–7,639; total 737,027), housing_units, aland_m2, awater_m2, cra_no, cra_group, neighborhood_alias, council_district, area_km2, share_in_city (min 0.886), pop_density_km2 |
| `zoning` | `Current_Land_Use_Zoning_Detail_2` | 3,627 | zone_id, zoning, base_zone, **zone_category** (10 values: Lowrise Multi-Family 1,270; Neighborhood Commercial 973; Neighborhood Residential 336; Major Institutions 295; Industrial 238; Commercial 210; Seattle Mixed 119; Downtown 99; High-Density Multi-Family 85; Master Planned Community 2), zone_class, zonelut, zonelut_desc, overlay, light_rail_overlay, major_institution, effective |
| `streets` | `Seattle_Streets_1` (SDOT) | 23,888 | compkey, street_name, **road_class** (local 16,986; minor_arterial 2,470; principal_arterial 2,238; collector_arterial 1,890; interstate_freeway 260; state_route_freeway 38; county_arterial 1; unknown 5), artclass, artdescript, speed_limit_mph, oneway, status, owner, surface_width_ft, length_m |
| `spd_beats` | `Current_Beats` | 55 | beat, precinct, sector (beat `99` has no precinct) |
| `neighborhoods` | `nma_nhoods_sub` (Neighborhood Map Atlas) | 94 | l_hood, s_hood, s_hood_alt_names, centroid_lat/lon (representative point) |

* **Cross-check** (manifest): Census place "Seattle city" (GEOID 5363000) area
  367.69 km² incl. water, land 217.62 km², POP100 737,015; 99.97% of the City
  polygon lies inside it. The City polygon excludes most of Elliott Bay/Lake
  Washington but includes some inland water.
* **Blocked**: `api.census.gov` (ACS 5-year, decennial PL) redirects to
  `missing_key.html` (HTTP 302) — a key is required now, so ACS covariates
  (income, age, …) are not included; decennial POP100/HU100 come from TIGERweb
  instead. OSM Overpass mirrors (`overpass-api.de`, `overpass.kumi.systems`,
  `maps.mail.ru`) reset/timed out/504 — the SDOT street layer is used instead.
* **Time variation / leakage**: zoning changes over time (the layer carries
  `EFFECTIVE` and `*_PREV` fields for rezones) and Seattle GIS also publishes
  "Beats 2008–2015" and "Beats 2015–2017" layers, i.e. beats have been redrawn;
  applying current polygons to 2019+ data is an anachronism to keep in mind (the
  SPD crime records carry their own `beat` attribute). Census 2020 population is
  fixed.

---

## 5. News — `seacrime.ingest.news`

### Sources (first fetch 2026-10-14 19:36 UTC; second fetch ~20:00 UTC)

| source | URL | status | items | newest item age at fetch |
|---|---|---|---|---|
| KING 5 local | `king5.com/feeds/syndication/rss/news/local` | 200 | 40 | 82 min |
| KOMO local | `komonews.com/news/local.rss` | 200 | 41 | 57 min |
| KIRO 7 local | `kiro7.com/arc/outboundfeeds/rss/category/news/local/?outputType=xml` | 200 | 15 | 6 min |
| Seattle Times local | `seattletimes.com/seattle-news/feed/` | 200 (403 on an earlier probe) | 30 | 45 min |
| The Stranger | `thestranger.com/feed` | 200 | 10 | 83 min |
| Capitol Hill Seattle Blog | `capitolhillseattle.com/feed/` | 200 | 5 | 222 min |
| PubliCola | `publicola.com/feed/` | 200 | 10 | ~28 h |
| My Ballard | `myballard.com/feed/` | 200 | 30 | 145 min |
| South Seattle Emerald | `southseattleemerald.org/feed/` | 200 | 2 | ~53 h |
| Seattle Met (Atom) | `seattlemet.com/feed` | 200 | 20 | 217 min |
| West Seattle Blog | `westseattleblog.com/feed/` | 200 (timed out on an earlier probe) | 15 | 59 min |
| Google News search "Seattle" | `news.google.com/rss/search?q=Seattle&hl=en-US&gl=US&ceid=US:en` | 200 on the first run; **503** on an earlier probe and on the second run (20:0x UTC) | 112 (aggregates many outlets) | 18 min |
| MyNorthwest / KIRO Newsradio | `mynorthwest.com/feed/` | **403** | 0 | – |
| GDELT DOC 2.0 | `api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json&query=seattle sourcelang:english` | **HTTP 429** ("Please limit requests to one every 5 seconds…") on 6 of 7 day-windows despite 6–30 s spacing, `{}` on the 7th, 429 again on a single retry later | 0 | – |

Not reachable / not usable: `web.archive.org` (Wayback CDX — connection reset, so
no archived-feed backfill), GDELT via BigQuery (no GCP credentials), KUOW
(`/feeds/news.rss` returns HTML), FOX 13 (empty feed), Seattle PI (404).

### Output

* `news_items.parquet` (330 items after the first fetch, 332 after the second —
  first-seen times preserved, `last_seen_at_utc` advanced for 216 items;
  published 2026-08-21 → 2026-10-14; Google News carries the oldest items): `item_id` (sha1 of canonical URL),
  `url`, `title`, `summary` (HTML stripped, ≤1,000 chars), `source` (feed key),
  `outlet` (Google News publisher / GDELT domain / feed key),
  `published_at_utc`, `fetched_at_utc` (first time we saw it),
  `last_seen_at_utc`, `available_at_utc` (= `fetched_at_utc`),
  `backfill_available_at_utc` (= published + 1 h), `observed_live`
  (published ≤ 6 h before first fetch; 76 of 330), `first_snapshot_id`.
  Appends dedupe by canonical URL (tracking parameters and fragments stripped) and
  keep the **first-seen** title/summary. Google News links are Google redirect
  URLs, so the same story from Google News and from the outlet's own feed is not
  merged.
* `news_signals.parquet`: `extract_news_signals(items, llm=None)` → keyword flags
  (`protest, parade, festival, closure, power_outage, shooting, fire,
  police_activity, sports_event, severe_weather`), `neighborhoods` (Neighborhood
  Map Atlas names found in title/summary, minus ambiguous names like "Broadway",
  "Mann", "Northwest"), `mentions_seattle`, `extraction_method`. In this fetch:
  shooting 24, police 31, sports 26, closure 10, fire 7, festival 5, protest 2,
  power outage 1; 70 items name a neighborhood. `llm="auto"` uses the Anthropic
  API (model `claude-opus-5-5`, low effort, server-side fallback) only when
  `ANTHROPIC_API_KEY` is set **and** the `anthropic` package is installed (it is
  not in the venv); otherwise it silently returns the keyword result. Any
  `prompt -> text` callable can be passed instead.

### Leakage rule

A news item may feed a forecast issued at `t` only if `available_at_utc <= t`
(`usable_news(items, t)`, default `mode="strict"`). Items that were already old when
first fetched, or that come from a historical backfill, may be used only in
explicitly labelled backfill experiments with availability
`published_at_utc + 1 h` (`usable_news(items, t, mode="backfill")`, which adds an
`availability_basis` column). Publisher timestamps can be revised and feeds can
drop or edit items, so strict mode is the only leakage-proof option.

### Feasibility

* **Historical**: effectively none from here — feeds expose only their latest
  10–112 items, GDELT DOC (which keeps ~3 months) is rate-limited from this IP,
  Wayback is unreachable. A news covariate cannot be trained on 2019–2026 history.
* **Ongoing**: run `fetch_news(cfg)` hourly (≈13 HTTP requests + optional GDELT)
  to accumulate a leakage-safe archive; spatial precision is at best a
  neighborhood mention (no coordinates).
* **Licence**: headlines, summaries and links only, for research; copyright stays
  with the publishers (do not republish full text). GDELT is open with
  attribution.

---

## Other blocked sources (observed)

| source | purpose | observed response |
|---|---|---|
| WSDOT Traveler Information API (`wsdot.wa.gov/Traffic/api/TrafficFlow/...`) | traffic flow / incidents | HTTP 401 "The supplied access code was missing or invalid" |
| OneBusAway Puget Sound REST API (`api.pugetsound.onebusaway.org/api/where/...`) | real-time arrivals, stops | HTTP 401 "permission denied" (GTFS-rt alerts endpoints for agencies 1 and 40 are open) |
| `api.census.gov` | ACS covariates | HTTP 302 → `missing_key.html` |
| Ticketmaster / SeatGeek | concerts, non-sports venue events | 401 / 403 (API key / client id required) |
| Open-Meteo historical forecasts | gridded forecast archive | intermittent HTTP 429 daily quota (shared IP) |
| GDELT DOC API | news backfill | HTTP 429 |
| web.archive.org | archived RSS backfill | connection reset |
| transitfeeds.com / GitHub raw | historical GTFS / monorail GTFS | HTTP 403 |
