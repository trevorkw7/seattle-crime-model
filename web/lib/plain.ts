/**
 * Plain-language wording for the simple (default) view. Everything here turns model
 * outputs into short everyday phrases; numbers are rounded on purpose.
 */

import { TIMEZONE } from './config';

export interface PlainCategory {
  id: string;
  /** short name, e.g. on buttons */
  name: string;
  /** what it includes */
  examples: string;
  /** used in sentences: "about 3 {noun} reports" */
  noun: string;
}

export const PLAIN_CATEGORIES: PlainCategory[] = [
  { id: 'property', name: 'Theft & break-ins', examples: 'Car break-ins, burglary, stolen cars, shoplifting', noun: 'theft or break-in' },
  { id: 'violent', name: 'Violent crime', examples: 'Assaults, robberies, sexual assaults', noun: 'violent crime' },
  { id: 'all_located', name: 'Any crime', examples: 'Every reported crime with a known location', noun: 'crime' },
];

export const plainCategory = (id: string): PlainCategory =>
  PLAIN_CATEGORIES.find((c) => c.id === id) ?? { id, name: id, examples: '', noun: 'incident' };

// ---------------------------------------------------------------------------
// Neighborhood names (SPD's neighborhood areas) in everyday spelling
// ---------------------------------------------------------------------------

const HOOD_NAMES: Record<string, string> = {
  BITTERLAKE: 'Bitter Lake',
  LAKECITY: 'Lake City',
  SANDPOINT: 'Sand Point',
  'SLU/CASCADE': 'South Lake Union',
  'DOWNTOWN COMMERCIAL': 'Downtown',
  'CHINATOWN/INTERNATIONAL DISTRICT': 'Chinatown–International District',
  UNIVERSITY: 'University District',
  'EASTLAKE - EAST': 'Eastlake',
  'EASTLAKE - WEST': 'Eastlake',
  'FAUNTLEROY SW': 'Fauntleroy',
  'COMMERCIAL DUWAMISH': 'Duwamish (industrial)',
  'COMMERCIAL HARBOR ISLAND': 'Harbor Island',
  SODO: 'SODO',
  'NEW HOLLY': 'NewHolly',
  'CENTRAL AREA/SQUIRE PARK': 'Central District',
  'BALLARD NORTH': 'Ballard (north)',
  'BALLARD SOUTH': 'Ballard (south)',
};

const SKIP_HOODS = new Set(['UNKNOWN', '-', '']);

export function hoodName(raw: string | null | undefined): string | null {
  if (!raw || SKIP_HOODS.has(raw.trim().toUpperCase())) return null;
  const key = raw.trim().toUpperCase();
  if (HOOD_NAMES[key]) return HOOD_NAMES[key];
  return key
    .toLowerCase()
    .split(/(\s+|\/|-)/)
    .map((w) => (/^[a-z]/.test(w) ? w[0].toUpperCase() + w.slice(1) : w))
    .join('')
    .replace(/\s*\/\s*/g, ' / ');
}

// ---------------------------------------------------------------------------
// Phrases
// ---------------------------------------------------------------------------

const ONE_IN = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50];

/** "about 1 in 4", "about 7 in 10" … */
export function chancePhrase(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return 'unknown';
  if (p < 0.02) return 'less than 1 in 50';
  if (p < 0.5) {
    const n = 1 / p;
    const nice = ONE_IN.reduce((a, b) => (Math.abs(b - n) < Math.abs(a - n) ? b : a));
    return `about 1 in ${nice}`;
  }
  if (p < 0.95) return `about ${Math.min(9, Math.max(5, Math.round(p * 10)))} in 10`;
  return 'more than 9 in 10';
}

/** "About 3 theft or break-in reports" / "Less than 1 …" */
export function countPhrase(expected: number, noun: string): string {
  if (expected < 0.5) return `Less than 1 ${noun} report`;
  if (expected < 1.5) return `About 1 ${noun} report`;
  const n = expected < 20 ? Math.round(expected) : Math.round(expected / 5) * 5;
  return `About ${n} ${noun} reports`;
}

export type Comparison = 'lower' | 'usual' | 'higher';

/** ratio of forecast to the usual (seasonal) level */
export function compareToUsual(ratio: number | null | undefined): Comparison | null {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio) || ratio <= 0) return null;
  if (ratio < 0.85) return 'lower';
  if (ratio > 1.15) return 'higher';
  return 'usual';
}

export const COMPARISON_TEXT: Record<Comparison, string> = {
  lower: 'Quieter than usual',
  usual: 'About usual',
  higher: 'Busier than usual',
};

export function comparisonDetail(ratio: number): string {
  const c = compareToUsual(ratio);
  if (c === 'usual') return 'within 15% of the usual level';
  const pct = Math.round(Math.abs(ratio - 1) * 100);
  return c === 'higher' ? `about ${pct}% more than usual` : `about ${pct}% fewer than usual`;
}

export type CityRank = 'top' | 'busier' | 'middle' | 'quieter';

/** share = fraction of areas that are busier (0 = busiest) */
export function cityRank(share: number): CityRank {
  if (share < 0.1) return 'top';
  if (share < 0.3) return 'busier';
  if (share < 0.7) return 'middle';
  return 'quieter';
}

export const CITY_RANK_TEXT: Record<CityRank, string> = {
  top: 'Among the busiest parts of Seattle',
  busier: 'Busier than most of Seattle',
  middle: 'About average for Seattle',
  quieter: 'Quieter than most of Seattle',
};

const partFmt = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, weekday: 'long', hour: 'numeric', hourCycle: 'h23' });

function localParts(iso: string) {
  const parts = partFmt.formatToParts(new Date(iso));
  return {
    weekday: parts.find((p) => p.type === 'weekday')?.value ?? '',
    hour: Number(parts.find((p) => p.type === 'hour')?.value ?? 0) % 24,
  };
}

function partOfDay(hour: number): string {
  if (hour >= 5 && hour < 12) return 'morning';
  if (hour >= 12 && hour < 17) return 'afternoon';
  if (hour >= 17 && hour < 21) return 'evening';
  return 'night';
}

/** "for a Wednesday evening" (6 h) or "for a Wednesday" (24 h) */
export function usualFor(windowStart: string, hours: number): string {
  const { weekday, hour } = localParts(windowStart);
  if (hours >= 24) return `for a ${weekday}`;
  // name the part of day at the middle of the window
  const midMs = new Date(windowStart).getTime() + (hours / 2) * 3600e3;
  const m = localParts(new Date(midMs).toISOString());
  // the small hours belong to the evening before ("Wednesday night", not "Thursday night")
  const day = m.hour < 5 ? localParts(new Date(midMs - 6 * 3600e3).toISOString()).weekday : m.weekday;
  return `for a ${day || weekday} ${partOfDay(m.hour ?? hour)}`;
}

const timeFmt = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, hour: 'numeric', minute: '2-digit' });
const dayFmt = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, weekday: 'long' });
const dateKey = new Intl.DateTimeFormat('en-CA', { timeZone: TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit' });

function shortTime(d: Date): string {
  return timeFmt.format(d).replace(':00', '').replace(/\u202f/g, ' ');
}

function dayWord(d: Date, now: Date): string {
  const k = dateKey.format(d);
  if (k === dateKey.format(now)) return 'today';
  if (k === dateKey.format(new Date(now.getTime() + 864e5))) return 'tomorrow';
  if (k === dateKey.format(new Date(now.getTime() - 864e5))) return 'yesterday';
  return dayFmt.format(d);
}

/** "3 PM – 9 PM today", "10 PM tonight – 4 AM tomorrow" */
export function windowPhrase(start: string, end: string, now: Date = new Date()): string {
  const a = new Date(start);
  const b = new Date(end);
  const da = dayWord(a, now);
  const db = dayWord(b, now);
  if (da === db) return `${shortTime(a)} – ${shortTime(b)} ${da}`;
  return `${shortTime(a)} ${da} – ${shortTime(b)} ${db}`;
}

/** "12 minutes ago", "3 hours ago", "2 days ago" */
export function agoPhrase(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return 'at an unknown time';
  const mins = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60000));
  if (mins < 2) return 'just now';
  if (mins < 60) return `${mins} minutes ago`;
  const h = Math.round(mins / 60);
  if (h < 36) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

/** "about 3 reports", "about 1 report", "under 1 report" */
export function reportsShort(expected: number): string {
  if (expected < 0.5) return 'under 1 report';
  if (expected < 1.5) return 'about 1 report';
  return `about ${expected < 20 ? Math.round(expected) : Math.round(expected / 5) * 5} reports`;
}
