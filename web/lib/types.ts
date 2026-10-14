/**
 * Types for the dashboard data contract v1 (docs/API_CONTRACT.md).
 * Fields marked optional/nullable are ones the contract leaves open; the UI
 * degrades gracefully when they are absent.
 */

/**
 * `replay` = forecast reconstructed for a past issue time using only data available
 * then (historical simulation from real data + real models). Not a live forecast.
 */
export type ProvenanceKind = 'actual' | 'provisional' | 'demo' | 'replay';

export interface Provenance {
  kind: ProvenanceKind;
  generated_at: string;
  note?: string | null;
}

export interface Category {
  id: string;
  label: string;
}

export interface ModelMeta {
  id: string;
  label: string;
  version: string;
  family?: string;
  trained_at?: string | null;
  is_default?: boolean;
  /** evaluated in backtests only — no live/replay forecasts are published */
  backtest_only?: boolean;
}

export interface Meta {
  provenance: Provenance;
  categories: Category[];
  horizons_h: number[];
  models: ModelMeta[];
  grid: {
    cell_size_m: number;
    n_cells: number;
    crs: string;
    bbox: [number, number, number, number];
  };
  event_time: string;
  disclaimers: string[];
}

export interface GridProperties {
  cell_id: string;
  row: number;
  col: number;
  neighborhood: string | null;
  in_city: boolean;
  precinct?: string | null;
}

export interface GridFeature {
  type: 'Feature';
  properties: GridProperties;
  geometry: { type: 'Polygon'; coordinates: number[][][] } | { type: 'MultiPolygon'; coordinates: number[][][][] };
}

export interface GridCollection {
  type: 'FeatureCollection';
  features: GridFeature[];
}

/** `awaiting_data`: frozen live forecast whose window SPD has not yet published. */
export type ForecastStatus = 'frozen' | 'awaiting_data' | 'scored_provisional' | 'scored_mature';

export interface ForecastIndexItem {
  forecast_id: string;
  issued_at: string;
  model_id: string;
  category: string;
  status: ForecastStatus;
  provenance_kind?: ProvenanceKind;
}

export interface ForecastIndex {
  provenance: Provenance;
  latest: Record<string, Record<string, string>>;
  items: ForecastIndexItem[];
}

export interface ForecastHorizon {
  window_start: string;
  window_end: string;
  total_expected: number;
  cells: Record<string, (number | null)[]>;
}

export interface HorizonScore {
  observed_total?: number;
  expected_total?: number;
  capture_at_5pct?: number | null;
  capture_at_10pct?: number | null;
  capture_at_20pct?: number | null;
  /** Accept the evaluation-summary spelling too. */
  capture_5?: number | null;
  capture_10?: number | null;
  capture_20?: number | null;
  pai_at_10pct?: number | null;
  pai_10?: number | null;
  brier?: number | null;
  [key: string]: unknown;
}

export interface ForecastScore {
  maturity: 'provisional' | 'mature';
  scored_at: string;
  per_horizon: Record<string, HorizonScore & { maturity?: 'provisional' | 'mature' }>;
  observed: Record<string, Record<string, number>>;
}

export interface ForecastDoc {
  provenance: Provenance;
  forecast_id: string;
  issued_at: string;
  model_id: string;
  model_version: string;
  category: string;
  data_versions: Record<string, string>;
  feature_cutoffs: Record<string, string>;
  columns: string[];
  horizons: Record<string, ForecastHorizon>;
  score: ForecastScore | null;
  sha256?: string;
}

export interface HistoryPeriod {
  start: string;
  end: string;
  cells: Record<string, number | null>;
}

export interface HistoryDoc {
  provenance: Provenance;
  category: string;
  min_count: number;
  periods: Record<string, HistoryPeriod>;
  hour_of_week_citywide?: number[];
}

export interface MetricRow {
  model_id: string;
  category: string;
  horizon_h: number;
  capture_5?: number | null;
  capture_10?: number | null;
  capture_20?: number | null;
  pai_10?: number | null;
  lift_vs_seasonal_10?: number | null;
  poisson_deviance?: number | null;
  nll?: number | null;
  mae?: number | null;
  rmse?: number | null;
  brier?: number | null;
  ece?: number | null;
  ci?: Record<string, [number, number]>;
  /** 'report' | 'offense' — rows may be published for both event-time definitions */
  event_time?: string;
  observed_total?: number;
  maturity?: 'provisional' | 'mature';
  n_forecasts?: number;
  /** prospective rows only: live (frozen in real time) vs historical replay */
  source?: 'live' | 'replay';
}

export interface TimeseriesPoint {
  issued_at: string;
  model_id: string;
  category: string;
  horizon_h: number;
  capture_10: number | null;
  maturity: 'provisional' | 'mature';
  source?: 'live' | 'replay';
  event_time?: string;
  observed_total?: number;
}

export interface AblationRow {
  name: string;
  model_id: string;
  category: string;
  horizon_h: number;
  event_time?: string;
  capture_10?: number | null;
  delta_vs_full?: number | null;
}

export interface CalibrationEntry {
  model_id: string;
  category: string;
  horizon_h: number;
  event_time?: string;
  bins: { p_mean: number; obs_rate: number; n: number }[];
}

/** Stability rows are not pinned down by the contract; we accept any of these keys. */
export interface StabilityRow {
  model_id?: string;
  category?: string;
  horizon_h?: number;
  precinct?: string;
  month?: string;
  group?: string;
  capture_10?: number | null;
  n_events?: number;
  [key: string]: unknown;
}

export interface EvaluationSummary {
  provenance: Provenance;
  backtest?: { test_window?: { start: string; end: string }; rows: MetricRow[] };
  prospective?: { rows: MetricRow[]; timeseries: TimeseriesPoint[] };
  ablations?: AblationRow[];
  calibration?: CalibrationEntry[];
  stability?: { by_precinct?: StabilityRow[]; by_month?: StabilityRow[] };
}

export type SourceStatus = 'ok' | 'stale' | 'missing';
export type ModelStatus = 'ok' | 'degraded' | 'missing';

export interface HealthDoc {
  provenance: Provenance;
  sources: {
    name: string;
    snapshot_id?: string | null;
    fetched_at?: string | null;
    publisher_updated_at?: string | null;
    latest_event_ts?: string | null;
    lag_hours?: number | null;
    status: SourceStatus;
  }[];
  models: {
    model_id: string;
    version?: string | null;
    last_forecast_at?: string | null;
    status: ModelStatus;
    notes?: string | null;
  }[];
  jobs: {
    name: string;
    last_run?: string | null;
    last_status: 'ok' | 'error' | string;
    message?: string | null;
  }[];
}
