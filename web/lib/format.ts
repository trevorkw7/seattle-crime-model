import { TIMEZONE } from './config';

const dtFull = new Intl.DateTimeFormat('en-US', {
  timeZone: TIMEZONE,
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
});
const dtNoTz = new Intl.DateTimeFormat('en-US', {
  timeZone: TIMEZONE,
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
});
const dtDate = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, month: 'short', day: 'numeric', year: 'numeric' });
const dtShortDate = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, month: 'short', day: 'numeric' });
const dtRange = new Intl.DateTimeFormat('en-US', {
  timeZone: TIMEZONE,
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
});

function toDate(v: string | number | Date): Date {
  return v instanceof Date ? v : new Date(v);
}

/** "Tue, Oct 14, 12:00 PM PDT" (Seattle local time). */
export function fmtDateTime(v: string | null | undefined, withTz = true): string {
  if (!v) return '—';
  const d = toDate(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return (withTz ? dtFull : dtNoTz).format(d);
}

const dtCompact = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

/** "Oct 8, 12:00 AM" (Seattle local time, no weekday/zone) for tight tables. */
export function fmtDateTimeCompact(v: string | null | undefined): string {
  if (!v) return '—';
  const d = toDate(v);
  return Number.isNaN(d.getTime()) ? String(v) : dtCompact.format(d);
}

export function fmtDate(v: string | null | undefined): string {
  if (!v) return '—';
  return dtDate.format(toDate(v));
}

export function fmtShortDate(v: string | number | Date): string {
  return dtShortDate.format(toDate(v));
}

/** "Tue, Oct 14, 12:00 – 6:00 PM PDT" — collapses shared parts of the range. */
export function fmtWindow(start: string, end: string): string {
  const a = toDate(start);
  const b = toDate(end);
  try {
    return dtRange.formatRange(a, b);
  } catch {
    return `${dtFull.format(a)} – ${dtFull.format(b)}`;
  }
}

export function fmtUtc(v: string | null | undefined): string {
  if (!v) return '—';
  return v.replace('T', ' ').replace(/:00Z$|Z$/, ' UTC').replace(/:00 UTC$/, ' UTC');
}

/** "3 h ago" / "in 20 min" relative to now (or a supplied reference). */
export function fmtRelative(v: string | null | undefined, ref: number = Date.now()): string {
  if (!v) return '—';
  const diff = toDate(v).getTime() - ref;
  const abs = Math.abs(diff);
  const min = abs / 60e3;
  let s: string;
  if (min < 1) return 'just now';
  if (min < 60) s = `${Math.round(min)} min`;
  else if (min < 48 * 60) s = `${Math.round(min / 60)} h`;
  else s = `${Math.round(min / 1440)} days`;
  return diff < 0 ? `${s} ago` : `in ${s}`;
}

/** Duration in hours between two instants, e.g. "72 h". */
export function fmtLagHours(fromIso: string, toIso: string): string {
  const h = (toDate(toIso).getTime() - toDate(fromIso).getTime()) / 3600e3;
  if (!Number.isFinite(h)) return '—';
  if (Math.abs(h) < 1) return `${Math.round(h * 60)} min`;
  if (Math.abs(h) < 72) return `${Number(h.toFixed(1))} h`;
  return `${Number((h / 24).toFixed(1))} days`;
}

export function fmtHours(h: number | null | undefined): string {
  if (h === null || h === undefined || !Number.isFinite(h)) return '—';
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 72) return `${Number(h.toFixed(1))} h`;
  return `${Number((h / 24).toFixed(1))} days`;
}

/** Expected counts: small values need more precision (2 significant digits below 0.1). */
export function fmtCount(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  if (v < 0.0001) return '<0.0001';
  if (v < 0.1) return String(Number(v.toPrecision(2)));
  if (v < 10) return v.toFixed(2);
  if (v < 100) return v.toFixed(1);
  return Math.round(v).toLocaleString('en-US');
}

export function fmtPct(v: number | null | undefined, digits?: number): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const pct = v * 100;
  const d = digits ?? (Math.abs(pct) < 1 && pct !== 0 ? 1 : 0);
  if (pct > 0 && pct < 0.1 && d <= 1) return '<0.1%';
  return `${pct.toFixed(d)}%`;
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

export function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v < 10 ? v.toFixed(2) : v.toFixed(1)}×`;
}

/** Percentage points, signed: "+1.2 pp". */
export function fmtPp(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const pp = v * 100;
  const s = Math.abs(pp) < 0.05 ? '0.0' : pp.toFixed(1);
  return `${pp > 0.05 ? '+' : pp < -0.05 ? '−' : '±'}${s.replace('-', '')} pp`;
}

/** "CAPITOL HILL" -> "Capitol Hill"; keeps short all-caps tokens like SLU or SW. */
export function titleCase(s: string | null | undefined): string {
  if (!s) return '';
  const keep = new Set(['SLU', 'SW', 'NE', 'NW', 'SE', 'ID', 'UW', 'SODO']);
  return s
    .toLowerCase()
    .replace(/([a-z]+)/g, (w) => (keep.has(w.toUpperCase()) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)));
}
