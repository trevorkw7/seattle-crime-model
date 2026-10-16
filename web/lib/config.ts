/** Runtime configuration (inlined at build time by Next.js). */

export const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? '').replace(/\/$/, '');

/** Where the Python backend's exported documents live. */
export const CONFIGURED_DATA_BASE = (process.env.NEXT_PUBLIC_DATA_BASE || `${BASE_PATH}/data`).replace(/\/$/, '');

/**
 * Live data published every hour by the `live-forecast` GitHub Actions workflow (GitHub Pages).
 * The site uses it whenever it is newer than the bundled snapshot; set the variable to an
 * empty string to use only the bundled data.
 */
export const LIVE_DATA_BASE = (process.env.NEXT_PUBLIC_LIVE_DATA_BASE ?? 'https://trevorkw7.github.io/seattle-crime-model/data').replace(/\/$/, '');

/** How often an open page checks for newer forecasts. */
export const REFRESH_MS = 5 * 60 * 1000;

/** Synthetic fixtures shipped with the app (scripts/make-demo-data.mjs). */
export const DEMO_DATA_BASE = `${BASE_PATH}/demo-data`;

export const TIMEZONE = 'America/Los_Angeles';

export const BASEMAP_STYLES = {
  light: 'https://tiles.openfreemap.org/styles/positron',
  dark: 'https://tiles.openfreemap.org/styles/dark',
} as const;

export const MAPLIBRE_WORKER_URL = `${BASE_PATH}/vendor/maplibre-gl-worker.js`;
