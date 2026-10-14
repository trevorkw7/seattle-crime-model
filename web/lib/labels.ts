/** Human-readable labels for identifiers that appear in the data documents. */

const SOURCE_LABELS: Record<string, string> = {
  spd_crime: 'SPD crime reports',
  spd_cad_calls: 'SPD calls for service (CAD)',
  spd_calls: 'SPD calls for service',
  fire_911: 'Seattle Fire 911 calls',
  sfd_fire_911: 'Seattle Fire 911 calls',
  noaa_weather: 'NOAA weather observations',
  weather: 'Weather observations',
  city_events_calendar: 'City special-events calendar',
  census_acs: 'Census ACS context',
};

const JOB_LABELS: Record<string, string> = {
  hourly_forecast: 'Hourly forecast',
  ingest_sources: 'Source ingest',
  daily_ingest: 'Daily ingest',
  score_forecasts: 'Forecast scoring',
  export_dashboard: 'Dashboard export',
  weekly_retrain: 'Weekly retrain',
};

const ABLATION_LABELS: Record<string, string> = {
  no_recent_counts: 'Without recent incident counts',
  no_spatial_lags: 'Without neighbouring-cell features',
  no_calls_for_service: 'Without calls for service',
  no_calendar: 'Without calendar & holidays',
  no_fire_911: 'Without Fire 911 calls',
  no_weather: 'Without weather',
  no_census_context: 'Without census context',
};

function humanize(id: string): string {
  const s = id.replace(/^no_/, 'without ').replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const MODEL_FALLBACK: Record<string, string> = {
  seasonal: 'Seasonal baseline',
  hotspot: 'Hotspot baseline',
  hotspot_kde: 'KDE hotspot baseline',
  lgbm: 'LightGBM',
};
export const modelFallbackLabel = (id: string) => MODEL_FALLBACK[id] ?? humanize(id);

export const sourceLabel = (id: string) => SOURCE_LABELS[id] ?? humanize(id);
export const jobLabel = (id: string) => JOB_LABELS[id] ?? humanize(id);
export const ablationLabel = (id: string) => ABLATION_LABELS[id] ?? humanize(id);

export function horizonLabel(h: number | string): string {
  const n = Number(h);
  if (n === 24) return '24 h';
  return `${n} h`;
}

export function horizonLong(h: number | string): string {
  const n = Number(h);
  return n === 1 ? 'next hour' : n === 24 ? 'next 24 hours' : `next ${n} hours`;
}
