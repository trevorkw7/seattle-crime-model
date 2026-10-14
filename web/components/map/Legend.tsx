'use client';

import { horizonLong } from '@/lib/labels';
import { fmtPct } from '@/lib/format';
import { METRICS, type ClassScheme, type MetricId } from '@/lib/scales';

export interface OverlayKeys {
  history?: { periodLabel: string; max: number; minCount: number; suppressed: number } | null;
  observed?: { maturity: string } | null;
}

export function Legend({
  scheme,
  metric,
  horizon,
  overlays,
  compact = false,
  opacities,
}: {
  scheme: ClassScheme;
  metric: MetricId;
  horizon: string;
  overlays: OverlayKeys;
  compact?: boolean;
  /** per-class fill opacity used on the map, so swatches match what is drawn */
  opacities?: number[];
}) {
  const op = (cls: number) => opacities?.[cls] ?? 1;
  const m = METRICS.find((x) => x.id === metric)!;
  const title = `${m.label} · ${horizonLong(horizon)}`;

  if (compact) {
    const rows = [...scheme.rows].reverse();
    return (
      <div className="legend legend-compact" aria-label={`Legend: ${title}`}>
        <div className="legend-title">{title}</div>
        <div className="legend-ramp" role="list">
          {rows.map((r) => (
            <span key={r.cls} role="listitem" className="ramp-step" title={`${r.label}: ${r.detail}`}>
              <span className="ramp-swatch" style={{ background: r.color, opacity: op(r.cls) }} />
            </span>
          ))}
        </div>
        <div className="legend-ramp-labels">
          <span>{scheme.kind === 'quantile' ? 'Lower 50%' : '< 0.5×'}</span>
          <span>{scheme.kind === 'quantile' ? `Top 5% ${scheme.rows[0].detail}` : '≥ 2× typical'}</span>
        </div>
        <OverlayKeyList overlays={overlays} />
      </div>
    );
  }

  return (
    <div className="legend" aria-label={`Legend: ${title}`}>
      <div className="legend-title">{title}</div>
      <div className="legend-sub">
        {scheme.kind === 'quantile' ? 'Shaded by rank among the city’s 500 m cells' : 'Ratio of forecast to the seasonal baseline'}
      </div>
      <ul className="legend-rows">
        {scheme.rows.map((r) => (
          <li key={r.cls}>
            <span className="legend-swatch" style={{ background: r.color, opacity: op(r.cls) }} aria-hidden="true" />
            <span className="legend-label">{r.label}</span>
            <span className="legend-detail">{r.detail}</span>
          </li>
        ))}
      </ul>
      {scheme.top10Share !== undefined ? (
        <p className="legend-note">
          The top 10% of cells hold <strong>{fmtPct(scheme.top10Share)}</strong> of expected reports.
        </p>
      ) : null}
      {scheme.kind === 'diverging' && scheme.nullCount > 0 ? (
        <p className="legend-note">{scheme.nullCount} cells have no seasonal baseline and are left blank.</p>
      ) : null}
      <OverlayKeyList overlays={overlays} />
    </div>
  );
}

function OverlayKeyList({ overlays }: { overlays: OverlayKeys }) {
  if (!overlays.history && !overlays.observed) return null;
  return (
    <ul className="legend-overlays">
      {overlays.history ? (
        <>
          <li>
            <span className="key-dots" aria-hidden="true">
              <span className="key-dot s" />
              <span className="key-dot m" />
              <span className="key-dot l" />
            </span>
            <span>
              Reported, {overlays.history.periodLabel} <span className="muted">(largest ≈ {overlays.history.max.toLocaleString('en-US')})</span>
            </span>
          </li>
          <li>
            <span className="key-hatch" aria-hidden="true" />
            <span>
              Fewer than {overlays.history.minCount} — count suppressed
              {overlays.history.suppressed ? <span className="muted"> ({overlays.history.suppressed} cells)</span> : null}
            </span>
          </li>
        </>
      ) : null}
      {overlays.observed ? (
        <li>
          <span className="key-outline" aria-hidden="true" />
          <span>
            Cells with reports during the window <span className="muted">({overlays.observed.maturity})</span>
          </span>
        </li>
      ) : null}
    </ul>
  );
}
