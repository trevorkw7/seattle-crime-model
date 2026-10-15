'use client';

import { useMemo, useState } from 'react';
import { fmtCount, fmtDateTime, fmtDateTimeCompact, fmtLagHours, fmtPct, fmtRelative, fmtShortDate, fmtUtc, titleCase } from '@/lib/format';
import { horizonLabel, sourceLabel } from '@/lib/labels';
import { formatMetric, type MetricId } from '@/lib/scales';
import type { ForecastDoc, ForecastIndexItem, HorizonScore, ModelMeta } from '@/lib/types';
import { ChevronLeft, ChevronRight } from '../Icons';
import { MaturityChip, ProvenanceBadge, Switch } from '../ui';

// ---------------------------------------------------------------------------
// Forecast-run timeline (replay of frozen forecasts)
// ---------------------------------------------------------------------------

const STATUS_TEXT: Record<string, string> = {
  frozen: 'Frozen · window still open',
  awaiting_data: 'Awaiting SPD data',
  scored_provisional: 'Scored · provisional',
  scored_mature: 'Scored · mature',
};

const tickClass = (it: ForecastIndexItem) => `st-${it.status}${it.provenance_kind === 'replay' ? ' is-replay' : ''}`;

export function ForecastTimeline({
  items,
  currentId,
  latestId,
  onSelect,
}: {
  items: ForecastIndexItem[];
  currentId: string | null;
  latestId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const idx = Math.max(
    0,
    items.findIndex((i) => i.forecast_id === currentId),
  );
  const cur = items[idx];
  if (items.length === 0 || !cur) return <p className="muted small">No forecast runs listed for this model and category.</p>;
  const go = (i: number) => {
    const it = items[Math.max(0, Math.min(items.length - 1, i))];
    onSelect(it.forecast_id === latestId ? null : it.forecast_id);
  };
  const isLatest = cur.forecast_id === latestId;
  const hasReplay = items.some((i) => i.provenance_kind === 'replay');
  return (
    <div className="timeline">
      <div className="timeline-head">
        <div>
          <div className="timeline-when">{fmtDateTime(cur.issued_at)}</div>
          <div className="timeline-status">
            <span className={`tick-key ${tickClass(cur)}`} aria-hidden="true" />
            {isLatest ? 'Latest forecast · ' : ''}
            {STATUS_TEXT[cur.status] ?? cur.status}
          </div>
        </div>
        <span className="timeline-side">
          {cur.provenance_kind === 'replay' ? <ProvenanceBadge provenance={{ kind: 'replay', generated_at: '' }} compact /> : null}
          <span className="timeline-count">
            {idx + 1} / {items.length}
          </span>
        </span>
      </div>
      <div className="timeline-row">
        <button type="button" className="icon-btn sm" onClick={() => go(idx - 1)} disabled={idx <= 0} aria-label="Previous forecast run">
          <ChevronLeft size={16} />
        </button>
        <div className="timeline-track">
          <div className="timeline-ticks" aria-hidden="true">
            {items.map((it, i) => (
              <span
                key={it.forecast_id}
                className={`tick ${tickClass(it)}${i === idx ? ' is-current' : ''}`}
                style={{ left: items.length === 1 ? '50%' : `${(i / (items.length - 1)) * 100}%` }}
              />
            ))}
          </div>
          <input
            type="range"
            className="timeline-range"
            min={0}
            max={Math.max(0, items.length - 1)}
            step={1}
            value={idx}
            onChange={(e) => go(Number(e.target.value))}
            aria-label="Forecast run (issue time)"
            aria-valuetext={`${fmtDateTime(cur.issued_at)}, ${STATUS_TEXT[cur.status] ?? cur.status}`}
          />
        </div>
        <button
          type="button"
          className="icon-btn sm"
          onClick={() => go(idx + 1)}
          disabled={idx >= items.length - 1}
          aria-label="Next forecast run"
        >
          <ChevronRight size={16} />
        </button>
      </div>
      <div className="timeline-foot">
        <span>{fmtShortDate(items[0].issued_at)}</span>
        {isLatest ? (
          <span>{fmtShortDate(items[items.length - 1].issued_at)}</span>
        ) : (
          <button type="button" className="link-btn" onClick={() => onSelect(null)}>
            Back to latest
          </button>
        )}
      </div>
      <ul className="timeline-legend" aria-label="Timeline key">
        <li>
          <span className="tick-key st-scored_mature" /> scored, mature
        </li>
        <li>
          <span className="tick-key st-scored_provisional" /> scored, provisional
        </li>
        <li>
          <span className="tick-key st-awaiting_data" /> awaiting data
        </li>
        <li>
          <span className="tick-key st-frozen" /> window open
        </li>
        {hasReplay ? (
          <li>
            <span className="tick-key st-scored_mature is-replay" /> square = historical replay
          </li>
        ) : null}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Score of a past forecast at the selected horizon
// ---------------------------------------------------------------------------

function pick(s: HorizonScore, a: string, b: string): number | null {
  const v = (s[a] ?? s[b]) as number | null | undefined;
  return typeof v === 'number' ? v : null;
}

export function ScorePanel({
  forecast,
  horizon,
  showObserved,
  onShowObserved,
}: {
  forecast: ForecastDoc;
  horizon: string;
  showObserved: boolean;
  onShowObserved: (v: boolean) => void;
}) {
  const score = forecast.score!;
  const s = score.per_horizon?.[horizon];
  const hz = forecast.horizons[horizon];
  return (
    <div className="score">
      <div className="section-head">
        <h2>Forecast vs. reported</h2>
        <MaturityChip maturity={s?.maturity ?? score.maturity} />
      </div>
      {s ? (
        <>
          <div className="stat-grid">
            <div className="stat">
              <span className="stat-label">Reported</span>
              <span className="stat-value">{s.observed_total ?? '—'}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Expected</span>
              <span className="stat-value">
                {fmtCount((s.expected_total as number | undefined) ?? (s.predicted_total as number | undefined) ?? hz?.total_expected)}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Capture, top 10%</span>
              <span className="stat-value">{fmtPct(pick(s, 'capture_at_10pct', 'capture_10'))}</span>
            </div>
          </div>
          <p className="small muted">
            Share of reports that fell in the 10% of cells this forecast ranked highest (random ranking ≈ 10%).
            {pick(s, 'capture_at_5pct', 'capture_5') !== null || pick(s, 'capture_at_20pct', 'capture_20') !== null ? (
              <>
                {' '}
                Top 5%: {fmtPct(pick(s, 'capture_at_5pct', 'capture_5'))} · top 20%: {fmtPct(pick(s, 'capture_at_20pct', 'capture_20'))}.
              </>
            ) : null}{' '}
            Scored {fmtRelative(score.scored_at)}.
          </p>
          {(s.maturity ?? score.maturity) !== 'mature' ? (
            <p className="small note-provisional">Late reports may still arrive for this window, so these numbers can change.</p>
          ) : null}
        </>
      ) : (
        <p className="small muted">The {horizonLabel(horizon)} window of this run has not been scored.</p>
      )}
      <Switch id="show-observed" checked={showObserved} onChange={onShowObserved} label="Outline cells with reports" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Provenance: versions, data snapshots, feature cutoffs
// ---------------------------------------------------------------------------

export function ProvenanceDetails({ forecast, model }: { forecast: ForecastDoc; model?: ModelMeta }) {
  const cutoffs = Object.entries(forecast.feature_cutoffs ?? {});
  const versions = Object.entries(forecast.data_versions ?? {});
  return (
    <div className="prov">
      <dl className="kv">
        <dt>Issued (frozen)</dt>
        <dd>
          {fmtDateTime(forecast.issued_at)}
          <span className="muted"> · {fmtUtc(forecast.issued_at)}</span>
        </dd>
        <dt>Forecast ID</dt>
        <dd className="mono">{forecast.forecast_id}</dd>
        {forecast.sha256 ? (
          <>
            <dt>SHA-256</dt>
            <dd className="mono small" title="Hash of the frozen forecast payload">
              {forecast.sha256.slice(0, 16)}…
            </dd>
          </>
        ) : null}
        <dt>Model</dt>
        <dd>
          {model?.label ?? forecast.model_id}
          <div className="mono small">{forecast.model_version}</div>
          {model?.trained_at ? <div className="small muted">trained {fmtDateTime(model.trained_at)}</div> : null}
        </dd>
        <dt>Document</dt>
        <dd>
          <ProvenanceBadge provenance={forecast.provenance} compact />{' '}
          <span className="small muted">generated {fmtDateTime(forecast.provenance?.generated_at)}</span>
          {forecast.provenance?.note ? <div className="small muted">{forecast.provenance.note}</div> : null}
        </dd>
      </dl>
      {cutoffs.length > 0 ? (
        <>
          <h3 className="prov-h">Feature cutoffs</h3>
          <p className="small muted">The newest data each source actually contributed when this forecast was frozen.</p>
          <table className="mini-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Data up to (local)</th>
                <th scope="col" className="num">Before issue</th>
              </tr>
            </thead>
            <tbody>
              {cutoffs.map(([k, v]) => (
                <tr key={k}>
                  <td>{sourceLabel(k)}</td>
                  <td>{fmtDateTimeCompact(v)}</td>
                  <td className="num">{fmtLagHours(v, forecast.issued_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {versions.length > 0 ? (
        <>
          <h3 className="prov-h">Data versions</h3>
          <table className="mini-table">
            <tbody>
              {versions.map(([k, v]) => (
                <tr key={k}>
                  <td>{sourceLabel(k)}</td>
                  <td className="mono small">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Weekly pattern (citywide reports by hour of week, local time)
// ---------------------------------------------------------------------------

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const hourLabel = (h: number) => (h === 0 ? '12 AM' : h < 12 ? `${h} AM` : h === 12 ? '12 PM' : `${h - 12} PM`);

export function WeeklyPattern({ values, highlight }: { values: number[]; highlight: Set<number> }) {
  const [hover, setHover] = useState<number | null>(null);
  const { classes, peak, low } = useMemo(() => {
    const sorted = [...values].sort((a, b) => a - b);
    const q = (f: number) => sorted[Math.min(sorted.length - 1, Math.floor(f * sorted.length))];
    const br = [q(0.2), q(0.4), q(0.6), q(0.8)];
    const classes = values.map((v) => br.filter((b) => v >= b).length);
    let peak = 0;
    let low = 0;
    values.forEach((v, i) => {
      if (v > values[peak]) peak = i;
      if (v < values[low]) low = i;
    });
    return { classes, peak, low };
  }, [values]);
  if (values.length !== 168) return null;
  const name = (i: number) => `${DAYS[Math.floor(i / 24)]} ${hourLabel(i % 24)}`;
  return (
    <figure className="weekly">
      <figcaption className="small">Typical week · citywide reports per hour (Seattle time)</figcaption>
      <div className="weekly-grid" onMouseLeave={() => setHover(null)}>
        {DAYS.map((d, di) => (
          <div className="weekly-row" key={d}>
            <span className="weekly-day">{d}</span>
            <div className="weekly-cells">
              {Array.from({ length: 24 }, (_, h) => {
                const i = di * 24 + h;
                return (
                  <span
                    key={h}
                    className={`wk c${classes[i]}${highlight.has(i) ? ' is-window' : ''}`}
                    onMouseEnter={() => setHover(i)}
                  />
                );
              })}
            </div>
          </div>
        ))}
        <div className="weekly-row weekly-axis" aria-hidden="true">
          <span className="weekly-day" />
          <div className="weekly-hours">
            <span>12a</span>
            <span>6a</span>
            <span>12p</span>
            <span>6p</span>
          </div>
        </div>
      </div>
      <p className="small muted weekly-readout">
        {hover !== null ? (
          <>
            <strong>{values[hover].toFixed(1)}</strong>/h · {name(hover)}
          </>
        ) : (
          <>
            Peak {name(peak)} ({values[peak].toFixed(1)}/h) · lowest {name(low)} ({values[low].toFixed(1)}/h).{' '}
            {highlight.size > 0 ? 'Outlined: the forecast window.' : ''}
          </>
        )}
      </p>
    </figure>
  );
}

// ---------------------------------------------------------------------------
// Table view: highest-ranked cells (keyboard-accessible alternative to the map)
// ---------------------------------------------------------------------------

export function TopCells({
  rows,
  metric,
  onFocus,
}: {
  rows: { id: string; neighborhood: string | null; value: number }[];
  metric: MetricId;
  onFocus: (id: string) => void;
}) {
  return (
    <table className="mini-table top-cells">
      <thead>
        <tr>
          <th scope="col">#</th>
          <th scope="col">Area</th>
          <th scope="col" className="num">
            Value
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={r.id}>
            <td className="num muted">{i + 1}</td>
            <td>
              <button type="button" className="link-btn" onClick={() => onFocus(r.id)}>
                {r.neighborhood ? titleCase(r.neighborhood) : 'Unnamed area'}
              </button>{' '}
              <span className="mono small muted">{r.id}</span>
            </td>
            <td className="num">{formatMetric(metric, r.value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
