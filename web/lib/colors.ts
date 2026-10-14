/**
 * Colour parameters (hex) for the WebGL map, which cannot read CSS variables.
 * Charts use the matching CSS custom properties defined in app/globals.css.
 * Ramps follow the validated reference palette (blue sequential, blue↔red diverging
 * with a neutral grey midpoint). In dark mode the sequential anchor flips so low values
 * recede into the dark basemap.
 */
export type Theme = 'light' | 'dark';

export const SEQUENTIAL: Record<Theme, string[]> = {
  // lower 50% → top 5%
  light: ['#cde2fb', '#9ec5f4', '#5598e7', '#256abf', '#0d366b'],
  dark: ['#104281', '#1c5cab', '#3987e5', '#86b6ef', '#cde2fb'],
};

export const DIVERGING: Record<Theme, string[]> = {
  // <0.5×, 0.5–0.8×, 0.8–1.25× (neutral), 1.25–2×, ≥2×
  light: ['#256abf', '#9ec5f4', '#e6e5df', '#f2a19a', '#c93a36'],
  dark: ['#86b6ef', '#1c5cab', '#3a3a37', '#a33b38', '#f08b86'],
};

export const MAP_INK: Record<Theme, { ink: string; halo: string; observed: string; gap: string; hatch: [number, number, number] }> = {
  light: { ink: '#0b0b0b', halo: '#ffffff', observed: '#d9480f', gap: '#ffffff', hatch: [60, 60, 56] },
  dark: { ink: '#f0efec', halo: '#0d0d0d', observed: '#ff8a4c', gap: '#0d0d0d', hatch: [220, 219, 212] },
};

/** Per-class fill opacity: low classes recede so the basemap stays readable. */
export const CLASS_OPACITY: Record<'quantile' | 'diverging', Record<Theme, number[]>> = {
  quantile: { light: [0.55, 0.68, 0.78, 0.86, 0.9], dark: [0.5, 0.62, 0.76, 0.86, 0.92] },
  diverging: { light: [0.86, 0.72, 0.45, 0.72, 0.86], dark: [0.88, 0.75, 0.5, 0.75, 0.9] },
};

export const FALLBACK_BACKGROUND: Record<Theme, string> = { light: '#eceae4', dark: '#1d1d1b' };
