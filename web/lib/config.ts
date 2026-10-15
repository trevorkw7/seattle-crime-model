/** Runtime configuration (inlined at build time by Next.js). */

export const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? '').replace(/\/$/, '');

/** Where the Python backend's exported documents live. */
export const CONFIGURED_DATA_BASE = (process.env.NEXT_PUBLIC_DATA_BASE || `${BASE_PATH}/data`).replace(/\/$/, '');

/** Synthetic fixtures shipped with the app (scripts/make-demo-data.mjs). */
export const DEMO_DATA_BASE = `${BASE_PATH}/demo-data`;

export const TIMEZONE = 'America/Los_Angeles';

export const BASEMAP_STYLES = {
  light: 'https://tiles.openfreemap.org/styles/positron',
  dark: 'https://tiles.openfreemap.org/styles/dark',
} as const;

export const MAPLIBRE_WORKER_URL = `${BASE_PATH}/vendor/maplibre-gl-worker.js`;
