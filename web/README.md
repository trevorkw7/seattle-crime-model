# Seattle Crime Forecast — dashboard (`web/`)

A static, client-side dashboard for the Seattle reported-crime forecasting research
platform. It shows **area-level (500 m grid cell)** forecasts of **publicly reported**
incidents, past frozen forecasts and how they scored, model evaluation, and data
freshness. It is a research and public-understanding tool. It makes no predictions
about individuals and is not meant for enforcement or deployment decisions. Those
limits, reporting bias, hundred-block location blurring and "associations, not causes"
are explained in the app's **About this map** drawer and in a footer line on every view.

- **Next.js 16** (App Router, TypeScript) with `output: 'export'`, so `npm run build`
  writes a plain static site to `out/` that any free static host can serve.
- **MapLibre GL JS 6** with the free, keyless **OpenFreeMap** basemaps (`positron` for
  light, `dark` for dark). Attribution stays visible.
- Charts are **hand-rolled SVG** components with no chart library.
- The app reads the JSON documents described in `../docs/API_CONTRACT.md`.

## Quick start

```bash
cd web
npm install
npm run dev          # http://localhost:3000
```

`npm run dev` and `npm run build` first run `prepare-assets`, which:

1. copies MapLibre's web worker to `public/vendor/maplibre-gl-worker.js` (MapLibre v6
   loads its worker from a separate file, set up with `setWorkerUrl`), and
2. regenerates the synthetic fixtures in `public/demo-data/` (deterministic, so the
   output is identical every time).

## Static export

```bash
npm run build        # -> out/
npm start            # serves out/ at http://localhost:4173 (scripts/serve-out.mjs)
```

Upload `out/` to any static host (GitHub Pages, Netlify, Cloudflare Pages, S3, …).
The export uses `trailingSlash: true`, so pages are `out/index.html`,
`out/evaluation/index.html` and `out/status/index.html`.

To host under a sub-path (for example a GitHub Pages project site), build with
`NEXT_PUBLIC_BASE_PATH=/repo-name npm run build`.

## Pointing at real data

The app fetches documents at runtime from `NEXT_PUBLIC_DATA_BASE`, which defaults to
`/data` (prefixed with the base path).

| Setup | What to do |
| --- | --- |
| Same site, built together | `python -m seacrime.export` writes `web/public/data/…`; `npm run build` copies it into `out/data/`. |
| Same site, data refreshed hourly | Build once, then have the export job overwrite `out/data/` (or the host's `/data/`) directly. Documents are fetched with `cache: 'no-cache'`, so no rebuild is needed. |
| Separate data host / bucket | `NEXT_PUBLIC_DATA_BASE=https://example.org/seacrime/data npm run build`. The data host must send CORS headers (`Access-Control-Allow-Origin`). |
| FastAPI (`seacrime.api`) | Serve the export from the API's `/data/...` routes, or set `NEXT_PUBLIC_DATA_BASE` to the API's `/data` URL. |

`NEXT_PUBLIC_*` variables are inlined at **build** time. Set them before `npm run build`.

### Demo fallback and provenance

- If `${NEXT_PUBLIC_DATA_BASE}/meta.json` is missing or malformed, the app loads
  `/demo-data` and shows a non-dismissable **"DEMO DATA — synthetic"** banner.
- Every document's `provenance.kind` is shown where that document is used:
  - `actual`: a subtle "Actual" chip.
  - `provisional`: an amber "Provisional — reports still arriving" chip.
  - `replay`: a blue "Historical replay (simulated as-of)" chip. Replay runs are drawn as squares in the replay timeline.
  - `demo`: a violet **DEMO** badge, plus a note in every map popup.
- The banner also appears when real data's `meta.json` itself says `demo`.

## Regenerating demo data

```bash
npm run demo-data    # node scripts/make-demo-data.mjs  [--out dir]
```

The script uses a seeded PRNG and a fixed "now" (2026‑10‑14T19:00Z), so its output is
byte-for-byte reproducible. It writes about 5 MB:

- a 54 × 31 grid of 500 m cells (1,066 cells kept, cut to a hand-traced city outline);
- a smooth synthetic risk surface;
- 2 models (`lgbm`, `hotspot_kde`) × 3 categories × 4 horizons;
- the 6 latest forecasts plus 24 past runs, with statuses `frozen`, `awaiting_data`,
  `scored_provisional` and `scored_mature`;
- history with suppressed (`null`) cells;
- an evaluation summary with backtest, live and replay prospective rows, calibration,
  ablations and stability;
- a health report covering ok, stale and missing sources, a degraded model and a failed job.

Every document has `provenance.kind = "demo"`.

## Smoke test (Playwright)

```bash
npm run build
npm run test:e2e
```

The test serves `out/` with `scripts/serve-out.mjs` and loads `/`, `/evaluation/` and
`/status/` at 1440×900 and 390×844 in Chromium. It fails on any console error and writes
screenshots to `screenshots/`. It also checks hover and tap popups, the mobile bottom
sheet, the About drawer, a scored past forecast with overlays, and two contract variants
(replay provenance, and zero live scores).

Two kinds of console message are tolerated and reported instead of failing the test:

- the expected 404 for `/data/meta.json` that triggers the demo fallback;
- basemap requests to `tiles.openfreemap.org` when that host is unreachable. The forecast
  grid is drawn independently. If the style cannot load at all, the map switches to a
  plain background and shows "Basemap unavailable — grid only".

`@playwright/test` is pinned to 1.56.1, which matches the pre-installed Chromium build
1194 under `PLAYWRIGHT_BROWSERS_PATH`. To use a different binary, set
`PW_CHROMIUM_EXECUTABLE`. If `HTTPS_PROXY` is set, the browser uses it, so tiles load
in sandboxed CI.

## Layout

```
web/
  app/                    routes: / (map), /evaluation, /status; globals.css (tokens, light+dark)
  components/
    Providers.tsx         data-source resolution (real → demo fallback), useDoc(), theme, About state
    AppHeader, DemoBanner, AboutDrawer, SiteFooter, ui.tsx (badges, chips, segmented, switch)
    map/                  MapExplorer (state, URL sync, panel), MapView (MapLibre), Legend, PanelParts
    charts/               SVG charts: CaptureBars, DotRows, Reliability, SeriesChart, common
    eval/EvaluationView   backtest vs prospective (live / replay), mature vs provisional
    status/StatusView     source freshness, model health, job status
  lib/                    contract types, formatting (America/Los_Angeles), colour ramps, scales
  scripts/                make-demo-data.mjs, copy-maplibre-worker.mjs, serve-out.mjs
  e2e/smoke.spec.ts       Playwright smoke test
  public/demo-data/       synthetic fixtures (generated)
  screenshots/            output of the smoke test
```

Map state is kept in the URL (`?cat=property&model=lgbm&h=6&metric=expected&fc=<id>&hist=1&period=last_30d`),
so a view can be shared by copying its link.

## How the UI reads the contract

These are interpretations of places where `API_CONTRACT.md` leaves room:

- **Forecast cells.** Values are read by position using the document's `columns`. A metric
  missing from `columns` is disabled in the UI. A cell missing from `horizons[h].cells` is
  drawn as "no forecast". Colour classes come from quantiles over the cells that are present.
- **Windows.** `window_start` and `window_end` are shown as given, in Seattle local time.
  The demo uses cumulative windows `[issued_at, issued_at + h)`.
- **Score metrics.** `score.per_horizon[h]` can use either `capture_at_10pct` or
  `capture_10` spellings (also `_5`/`_20`), plus `observed_total` and an optional
  `expected_total`. The default falls back to `horizons[h].total_expected`.
- **Observed counts.** `score.observed[h]` may be sparse; a missing cell means 0.
- **History.** A number is a count, `null` means suppressed (fewer than `min_count`) and is
  hatched, and a missing cell is not drawn. The demo omits zero-count cells.
- **Relative risk.** `relative_risk` may be `null` when there is no baseline; those cells
  are left blank. It is shown on a diverging scale with breaks at 0.5, 0.8, 1.25 and 2×.
- **Prospective rows.** A row or time-series point without `source` is treated as `live`.
  The page notes when there are no live scores yet.
- **Stability.** `stability.by_precinct` and `by_month` rows are read as
  `{model_id, category, horizon_h, precinct | month ("YYYY-MM") | group, capture_10, n_events}`.
  Rows without `category` or `horizon_h` apply to every selection.
- **Health.** Fields can be `null` for a `missing` source.
- **Timeline.** Index items are sorted by `issued_at`. `latest[model][category]` is
  treated as the newest run.
