import { DIVERGING, SEQUENTIAL, type Theme } from './colors';
import { fmtCount, fmtPct, fmtRatio } from './format';

export type MetricId = 'expected' | 'p_any' | 'relative_risk';

export const METRICS: { id: MetricId; short: string; label: string; help: string }[] = [
  {
    id: 'expected',
    short: 'Expected',
    label: 'Expected reports',
    help: 'Average number of incident reports the model expects in each 500 m cell during the window.',
  },
  {
    id: 'p_any',
    short: 'P(≥1)',
    label: 'Chance of at least one report',
    help: 'Probability that at least one incident is reported in the cell during the window.',
  },
  {
    id: 'relative_risk',
    short: 'vs typical',
    label: 'Relative to typical',
    help: 'Expected reports divided by the seasonal baseline for this cell, hour of week and time of year. 1× = typical.',
  },
];

export function formatMetric(metric: MetricId, v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  if (metric === 'expected') return fmtCount(v);
  if (metric === 'p_any') return fmtPct(v, v < 0.1 ? 1 : 0);
  return fmtRatio(v);
}

export interface LegendClass {
  cls: number;
  color: string;
  label: string;
  detail: string;
  count: number;
}

export interface ClassScheme {
  kind: 'quantile' | 'diverging';
  /** colours indexed by class number */
  colors: string[];
  /** rows in display order (highest first) */
  rows: LegendClass[];
  cellClass: Map<string, number>;
  /** quantile thresholds (top 5/10/20/50 %) for quantile schemes */
  thresholds?: { t5: number; t10: number; t20: number; t50: number };
  /** share of the citywide total held by the top-10% cells (expected metric only) */
  top10Share?: number;
  top5Share?: number;
  nullCount: number;
  n: number;
}

export const RR_BREAKS = [0.5, 0.8, 1.25, 2];

export function buildScheme(metric: MetricId, values: Map<string, number | null>, theme: Theme): ClassScheme {
  const entries: [string, number][] = [];
  let nullCount = 0;
  for (const [id, v] of values) {
    if (v === null || v === undefined || !Number.isFinite(v)) nullCount++;
    else entries.push([id, v]);
  }
  const n = entries.length;
  const cellClass = new Map<string, number>();

  if (metric === 'relative_risk') {
    const colors = DIVERGING[theme];
    const counts = [0, 0, 0, 0, 0];
    for (const [id, v] of entries) {
      let c = 0;
      while (c < RR_BREAKS.length && v >= RR_BREAKS[c]) c++;
      cellClass.set(id, c);
      counts[c]++;
    }
    const labels = [
      ['Well below typical', '< 0.5×'],
      ['Below typical', '0.5 – 0.8×'],
      ['Near typical', '0.8 – 1.25×'],
      ['Above typical', '1.25 – 2×'],
      ['Well above typical', '≥ 2×'],
    ];
    const rows = [4, 3, 2, 1, 0].map((c) => ({ cls: c, color: colors[c], label: labels[c][0], detail: labels[c][1], count: counts[c] }));
    return { kind: 'diverging', colors, rows, cellClass, nullCount, n };
  }

  const colors = SEQUENTIAL[theme];
  const sorted = entries.map((e) => e[1]).sort((a, b) => b - a);
  const at = (f: number) => (n === 0 ? 0 : sorted[Math.max(0, Math.min(n - 1, Math.ceil(f * n) - 1))]);
  const t5 = at(0.05);
  const t10 = at(0.1);
  const t20 = at(0.2);
  const t50 = at(0.5);
  const counts = [0, 0, 0, 0, 0];
  let total = 0;
  for (const [id, v] of entries) {
    let c = 0;
    if (v > 0 || t50 === 0) {
      if (v >= t5 && t5 > 0) c = 4;
      else if (v >= t10 && t10 > 0) c = 3;
      else if (v >= t20 && t20 > 0) c = 2;
      else if (v >= t50 && t50 > 0) c = 1;
    }
    cellClass.set(id, c);
    counts[c]++;
    total += v;
  }
  const f = (v: number) => formatMetric(metric, v);
  const rows: LegendClass[] = [
    { cls: 4, color: colors[4], label: 'Top 5% of cells', detail: `≥ ${f(t5)}`, count: counts[4] },
    { cls: 3, color: colors[3], label: 'Top 5–10%', detail: `${f(t10)} – ${f(t5)}`, count: counts[3] },
    { cls: 2, color: colors[2], label: 'Top 10–20%', detail: `${f(t20)} – ${f(t10)}`, count: counts[2] },
    { cls: 1, color: colors[1], label: 'Top 20–50%', detail: `${f(t50)} – ${f(t20)}`, count: counts[1] },
    { cls: 0, color: colors[0], label: 'Lower 50%', detail: `< ${f(t50)}`, count: counts[0] },
  ];
  let top10Share: number | undefined;
  let top5Share: number | undefined;
  if (metric === 'expected' && total > 0) {
    const k10 = Math.max(1, Math.round(0.1 * n));
    const k5 = Math.max(1, Math.round(0.05 * n));
    top10Share = sorted.slice(0, k10).reduce((a, b) => a + b, 0) / total;
    top5Share = sorted.slice(0, k5).reduce((a, b) => a + b, 0) / total;
  }
  return { kind: 'quantile', colors, rows, cellClass, thresholds: { t5, t10, t20, t50 }, top10Share, top5Share, nullCount, n };
}

/** Rank percentile of a value among the forecast cells (0 = highest). */
export function rankShare(values: number[], v: number): number {
  if (values.length === 0) return 1;
  let above = 0;
  for (const x of values) if (x > v) above++;
  return above / values.length;
}

// ---------------------------------------------------------------------------
// Generic chart helpers
// ---------------------------------------------------------------------------

export function linear(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

export function logScale(d0: number, d1: number, r0: number, r1: number) {
  const l0 = Math.log10(d0);
  const l1 = Math.log10(d1);
  const k = l1 === l0 ? 0 : (r1 - r0) / (l1 - l0);
  return (v: number) => r0 + (Math.log10(Math.max(v, d0 * 1e-3)) - l0) * k;
}

export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!(max > min)) return [min];
  const span = max - min;
  const step0 = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const err = step0 / mag;
  const step = (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
  const start = Math.ceil(min / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let v = start; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

export function niceMax(v: number, step = 0.1): number {
  return Math.ceil(v / step - 1e-9) * step;
}

export function logTicks(min: number, max: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(min)); e <= Math.ceil(Math.log10(max)); e++) {
    for (const m of [1, 2, 5]) {
      const v = m * Math.pow(10, e);
      if (v >= min * 0.999 && v <= max * 1.001) out.push(v);
    }
  }
  return out;
}
