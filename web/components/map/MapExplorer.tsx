'use client';

import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useData, useDoc, useTheme } from '../Providers';
import { ChevronUp, LayersIcon } from '../Icons';
import { ProvenanceBadge, Segmented, SelectField, Switch } from '../ui';
import { DisclaimerLine } from '../SiteFooter';
import { Legend, type OverlayKeys } from './Legend';
import { ForecastTimeline, ProvenanceDetails, ScorePanel, TopCells, WeeklyPattern } from './PanelParts';
import type { PopupModel } from './MapView';
import { CLASS_OPACITY } from '@/lib/colors';
import { TIMEZONE } from '@/lib/config';
import { fmtCount, fmtPct, fmtRatio, fmtWindow, titleCase } from '@/lib/format';
import { horizonLabel, horizonLong } from '@/lib/labels';
import { buildScheme, METRICS, type MetricId } from '@/lib/scales';
import type { ForecastDoc, ForecastIndex, GridCollection, GridProperties, HistoryDoc } from '@/lib/types';

const MapView = dynamic(() => import('./MapView'), {
  ssr: false,
  loading: () => <div className="map-canvas map-placeholder" />,
});

type Period = 'last_7d' | 'last_30d' | 'last_365d';
const PERIODS: { value: Period; label: string; long: string }[] = [
  { value: 'last_7d', label: '7 days', long: 'past 7 days' },
  { value: 'last_30d', label: '30 days', long: 'past 30 days' },
  { value: 'last_365d', label: '365 days', long: 'past 365 days' },
];

interface Selection {
  category: string;
  model: string;
  horizon: string;
  metric: MetricId;
  forecastId: string | null;
  showHistory: boolean;
  period: Period;
  showObserved: boolean;
}

function readUrl(): Partial<Selection> {
  if (typeof window === 'undefined') return {};
  const q = new URLSearchParams(window.location.search);
  const out: Partial<Selection> = {};
  if (q.get('cat')) out.category = q.get('cat')!;
  if (q.get('model')) out.model = q.get('model')!;
  if (q.get('h')) out.horizon = q.get('h')!;
  const m = q.get('metric');
  if (m === 'expected' || m === 'p_any' || m === 'relative_risk') out.metric = m;
  if (q.get('fc')) out.forecastId = q.get('fc');
  if (q.get('hist') === '1') out.showHistory = true;
  const p = q.get('period');
  if (p === 'last_7d' || p === 'last_30d' || p === 'last_365d') out.period = p;
  return out;
}

function writeUrl(s: Selection) {
  const q = new URLSearchParams();
  q.set('cat', s.category);
  q.set('model', s.model);
  q.set('h', s.horizon);
  q.set('metric', s.metric);
  if (s.forecastId) q.set('fc', s.forecastId);
  if (s.showHistory) {
    q.set('hist', '1');
    q.set('period', s.period);
  }
  const url = `${window.location.pathname}?${q.toString()}`;
  if (url !== `${window.location.pathname}${window.location.search}`) window.history.replaceState(window.history.state, '', url);
}

const hourOfWeekFmt = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, weekday: 'short', hour: 'numeric', hourCycle: 'h23' });
const DOW: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
function windowHoursOfWeek(start: string, end: string): Set<number> {
  const out = new Set<number>();
  const a = new Date(start).getTime();
  const b = new Date(end).getTime();
  for (let t = a; t < b && out.size < 168; t += 3600e3) {
    const parts = hourOfWeekFmt.formatToParts(new Date(t));
    const wd = parts.find((p) => p.type === 'weekday')?.value ?? 'Mon';
    const hr = Number(parts.find((p) => p.type === 'hour')?.value ?? 0) % 24;
    out.add((DOW[wd] ?? 0) * 24 + hr);
  }
  return out;
}

function useIsMobile() {
  const [mobile, setMobile] = useState<boolean | null>(null);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 760px)');
    const on = () => setMobile(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return mobile;
}

export function MapExplorer() {
  const data = useData();
  const { theme } = useTheme();
  const isMobile = useIsMobile();
  const meta = data.status === 'ready' ? data.meta : null;
  const [sel, setSel] = useState<Selection | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [focus, setFocus] = useState<{ cellId: string; nonce: number } | null>(null);
  const [basemap, setBasemap] = useState<'ok' | 'fallback' | 'degraded'>('ok');

  // Initialise the selection from the URL + meta defaults once meta is known.
  useEffect(() => {
    if (!meta || sel) return;
    const u = readUrl();
    const cats = meta.categories.map((c) => c.id);
    const models = meta.models.map((m) => m.id);
    const hs = meta.horizons_h.map(String);
    const defModel = meta.models.find((m) => m.is_default && !m.backtest_only)?.id ?? meta.models.find((m) => !m.backtest_only)?.id ?? models[0];
    setSel({
      category: u.category && cats.includes(u.category) ? u.category : cats.includes('property') ? 'property' : cats[0],
      model: u.model && models.includes(u.model) ? u.model : defModel,
      horizon: u.horizon && hs.includes(u.horizon) ? u.horizon : hs.includes('6') ? '6' : hs[0],
      metric: u.metric ?? 'expected',
      forecastId: u.forecastId ?? null,
      showHistory: u.showHistory ?? false,
      period: u.period ?? 'last_30d',
      showObserved: true,
    });
  }, [meta, sel]);

  useEffect(() => {
    if (sel) writeUrl(sel);
  }, [sel]);

  const update = useCallback((patch: Partial<Selection>) => setSel((s) => (s ? { ...s, ...patch } : s)), []);

  const grid = useDoc<GridCollection>(meta ? 'grid.geojson' : null);
  const index = useDoc<ForecastIndex>(meta ? 'forecasts/index.json' : null);

  const runs = useMemo(() => {
    if (!index.data || !sel) return [];
    return index.data.items
      .filter((i) => i.model_id === sel.model && i.category === sel.category)
      .sort((a, b) => (a.issued_at < b.issued_at ? -1 : a.issued_at > b.issued_at ? 1 : 0));
  }, [index.data, sel]);
  const latestId = useMemo(() => {
    if (!index.data || !sel) return null;
    return index.data.latest?.[sel.model]?.[sel.category] ?? runs[runs.length - 1]?.forecast_id ?? null;
  }, [index.data, sel, runs]);
  const knownRun = sel?.forecastId ? runs.some((r) => r.forecast_id === sel.forecastId) : true;
  const forecastId = sel?.forecastId && knownRun ? sel.forecastId : latestId;

  const forecast = useDoc<ForecastDoc>(forecastId ? `forecasts/${forecastId}.json` : null);
  const history = useDoc<HistoryDoc>(sel ? `history/${sel.category}.json` : null);

  // When model/category change while replaying, jump to the closest run of the new pair.
  const changePair = (patch: Partial<Selection>) => {
    if (!sel || !index.data) return update(patch);
    if (!sel.forecastId) return update(patch);
    const cur = runs.find((r) => r.forecast_id === sel.forecastId);
    const model = patch.model ?? sel.model;
    const category = patch.category ?? sel.category;
    const cand = index.data.items.filter((i) => i.model_id === model && i.category === category);
    const latest = index.data.latest?.[model]?.[category];
    if (!cur || cand.length === 0) return update({ ...patch, forecastId: null });
    const t = new Date(cur.issued_at).getTime();
    const best = cand.reduce((a, b) =>
      Math.abs(new Date(b.issued_at).getTime() - t) < Math.abs(new Date(a.issued_at).getTime() - t) ? b : a,
    );
    update({ ...patch, forecastId: best.forecast_id === latest ? null : best.forecast_id });
  };

  // ---- derived values ------------------------------------------------------
  const gridIndex = useMemo(() => {
    const m = new Map<string, GridProperties>();
    grid.data?.features.forEach((f) => m.set(f.properties.cell_id, f.properties));
    return m;
  }, [grid.data]);

  const fc = forecast.data;
  const horizon = sel?.horizon ?? '6';
  const hz = fc?.horizons?.[horizon] ?? null;
  const col = useMemo(() => {
    const cols = fc?.columns ?? [];
    return {
      expected: cols.indexOf('expected'),
      p_any: cols.indexOf('p_any'),
      relative_risk: cols.indexOf('relative_risk'),
      lo90: cols.indexOf('lo90'),
      hi90: cols.indexOf('hi90'),
    };
  }, [fc]);
  const metric: MetricId = sel?.metric ?? 'expected';
  const metricAvailable = (m: MetricId) => !fc || col[m] >= 0;

  const values = useMemo(() => {
    const out = new Map<string, number | null>();
    if (!hz) return out;
    const ci = col[metric];
    if (ci < 0) return out;
    for (const [id, row] of Object.entries(hz.cells)) {
      const v = row?.[ci];
      out.set(id, typeof v === 'number' ? v : null);
    }
    return out;
  }, [hz, col, metric]);

  const scheme = useMemo(() => buildScheme(metric, values, theme), [metric, values, theme]);
  const fills = useMemo(() => {
    const m = new Map<string, { color: string; opacity: number }>();
    const op = CLASS_OPACITY[scheme.kind][theme];
    for (const [id, c] of scheme.cellClass) m.set(id, { color: scheme.colors[c], opacity: op[c] });
    return m;
  }, [scheme, theme]);
  const sortedValues = useMemo(() => [...values.values()].filter((v): v is number => v !== null).sort((a, b) => b - a), [values]);

  const period = sel?.period ?? 'last_30d';
  const histPeriod = history.data?.periods?.[period] ?? null;
  const historyOverlay = useMemo(() => {
    if (!sel?.showHistory || !histPeriod) return null;
    let max = 0;
    for (const v of Object.values(histPeriod.cells)) if (typeof v === 'number' && v > max) max = v;
    return { counts: histPeriod.cells, max };
  }, [sel?.showHistory, histPeriod]);
  const suppressedCount = useMemo(
    () => (historyOverlay ? Object.values(historyOverlay.counts).filter((v) => v === null).length : 0),
    [historyOverlay],
  );

  const observed = useMemo(() => {
    if (!fc?.score || !sel?.showObserved) return null;
    return fc.score.observed?.[horizon] ?? null;
  }, [fc, sel?.showObserved, horizon]);

  const model = meta?.models.find((m) => m.id === (fc?.model_id ?? sel?.model));
  // Only offer models that actually publish forecasts (backtest-only baselines are evaluated on /evaluation).
  const mapModels = useMemo(() => {
    const all = meta?.models ?? [];
    const withRuns = new Set(index.data?.items.map((i) => i.model_id) ?? []);
    const list = all.filter((m) => (withRuns.size ? withRuns.has(m.id) : !m.backtest_only));
    return list.length ? list : all;
  }, [meta, index.data]);
  const category = meta?.categories.find((c) => c.id === (fc?.category ?? sel?.category));
  const isDemo = data.status === 'ready' && (data.demo || fc?.provenance?.kind === 'demo');

  const describe = useCallback(
    (cellId: string): PopupModel | null => {
      if (!hz) return null;
      const g = gridIndex.get(cellId);
      const row = hz.cells[cellId];
      if (!row) return null;
      const get = (i: number) => (i >= 0 && typeof row[i] === 'number' ? (row[i] as number) : null);
      const exp = get(col.expected);
      const p = get(col.p_any);
      const rr = get(col.relative_risk);
      const lo = get(col.lo90);
      const hi = get(col.hi90);
      const v = values.get(cellId);
      const rows: PopupModel['rows'] = [];
      rows.push({
        label: 'Expected reports',
        value: fmtCount(exp),
        sub: lo !== null && hi !== null ? `90% interval ${lo}–${hi}` : col.lo90 >= 0 ? 'no interval from this model' : undefined,
      });
      if (col.p_any >= 0) rows.push({ label: 'Chance of ≥ 1 report', value: fmtPct(p, p !== null && p < 0.1 ? 1 : 0) });
      if (col.relative_risk >= 0) {
        rows.push({
          label: 'Relative to typical',
          value: rr === null ? 'no baseline' : fmtRatio(rr),
          sub: rr === null ? undefined : rr >= 1.25 ? 'above seasonal baseline' : rr < 0.8 ? 'below seasonal baseline' : 'near seasonal baseline',
        });
      }
      if (typeof v === 'number' && sortedValues.length > 0) {
        let above = 0;
        while (above < sortedValues.length && sortedValues[above] > v) above++;
        const pct = Math.max(1, Math.ceil(((above + 1) / sortedValues.length) * 100));
        rows.push({
          label: 'City rank',
          value: pct <= 50 ? `top ${pct}%` : `bottom ${101 - pct}%`,
          sub: `of ${sortedValues.length.toLocaleString('en-US')} cells`,
        });
      }
      if (observed) {
        rows.push({
          label: 'Reported in window',
          value: String(observed[cellId] ?? 0),
          sub: fc?.score?.maturity === 'mature' ? 'mature' : 'provisional — may still rise',
        });
      }
      if (historyOverlay) {
        const hv = historyOverlay.counts[cellId];
        const lab = PERIODS.find((x) => x.value === period)!.long;
        rows.push({
          label: `Reported, ${lab}`,
          value: hv === null ? `fewer than ${history.data?.min_count ?? 3}` : hv === undefined ? 'none listed' : hv.toLocaleString('en-US'),
          sub: hv === null ? 'suppressed for privacy' : undefined,
        });
      }
      const notes: string[] = [];
      if (g && !g.in_city) notes.push('This cell lies partly outside Seattle city limits.');
      if (fc?.provenance?.kind === 'replay') notes.push('Historical replay — reconstructed as of the issue time, not a live forecast.');
      if (isDemo) notes.push('DEMO — synthetic value, not a real forecast.');
      return {
        title: g?.neighborhood ? titleCase(g.neighborhood) : 'Unnamed area',
        subtitle: `${category?.label ?? ''} · 500 m cell ${cellId}${g?.precinct ? ` · ${titleCase(g.precinct)} precinct` : ''}`,
        window: fmtWindow(hz.window_start, hz.window_end),
        rows,
        notes,
      };
    },
    [hz, gridIndex, col, values, sortedValues, metric, observed, fc, historyOverlay, period, history.data, isDemo, category],
  );

  const topCells = useMemo(() => {
    const arr: { id: string; neighborhood: string | null; value: number }[] = [];
    for (const [id, v] of values) if (v !== null) arr.push({ id, neighborhood: gridIndex.get(id)?.neighborhood ?? null, value: v });
    return arr.sort((a, b) => b.value - a.value).slice(0, 10);
  }, [values, gridIndex]);

  const windowHours = useMemo(() => (hz ? windowHoursOfWeek(hz.window_start, hz.window_end) : new Set<number>()), [hz]);

  const overlays: OverlayKeys = {
    history:
      historyOverlay && history.data
        ? {
            periodLabel: PERIODS.find((x) => x.value === period)!.long,
            max: historyOverlay.max,
            minCount: history.data.min_count ?? 3,
            suppressed: suppressedCount,
          }
        : null,
    observed: observed ? { maturity: fc?.score?.maturity === 'mature' ? 'mature' : 'provisional' } : null,
  };

  // ---- render ----------------------------------------------------------------
  if (data.status === 'error') {
    return (
      <div className="map-page">
        <div className="center-msg">
          <h1>Data unavailable</h1>
          <p>Neither the published data nor the bundled demo fixtures could be loaded.</p>
          <p className="mono small">{data.message}</p>
        </div>
      </div>
    );
  }

  const ready = !!(meta && sel && grid.data);
  const padding =
    isMobile === null
      ? { top: 20, right: 20, bottom: 20, left: 20 }
      : isMobile
        ? { top: 12, right: 12, bottom: 268, left: 12 }
        : { top: 32, right: 300, bottom: 32, left: 400 };
  const scored = !!fc?.score;
  const isLatest = !!fc && fc.forecast_id === latestId;
  const metricHelp = METRICS.find((m) => m.id === metric)!.help;

  return (
    <div className="map-page">
      {ready && isMobile !== null ? (
        <MapView
          grid={grid.data!}
          bbox={meta!.grid.bbox}
          theme={theme}
          fills={fills}
          history={historyOverlay}
          observed={observed}
          describe={describe}
          focus={focus}
          padding={padding}
          compactControls={isMobile}
          dim={forecast.loading}
          onBasemapStatus={setBasemap}
        />
      ) : (
        <div className="map-canvas map-placeholder" />
      )}

      <div className="map-status" aria-live="polite">
        {!ready || (forecast.loading && !forecast.stale) ? <span className="pill">Loading forecast…</span> : null}
        {forecast.stale ? <span className="pill">Loading forecast run…</span> : null}
        {forecast.error ? <span className="pill pill-error">Forecast could not be loaded ({forecast.error.message})</span> : null}
        {index.error ? <span className="pill pill-error">No forecast index published yet</span> : null}
        {basemap !== 'ok' ? (
          <span className="pill" title="The OpenFreeMap tile service could not be reached. The forecast grid is unaffected.">
            Basemap unavailable — grid only
          </span>
        ) : null}
      </div>

      {sel && meta ? (
        <aside className="panel" data-expanded={expanded} aria-label="Map controls">
          <div className="sheet-head">
            <button type="button" className="sheet-toggle" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
              <span className="sheet-grip" aria-hidden="true" />
              <span className="sheet-summary">
                <strong>{category?.label ?? sel.category}</strong> · {model?.label ?? sel.model}
              </span>
              <span className="sheet-more">
                {expanded ? 'Less' : 'More'}
                <ChevronUp size={15} className={expanded ? '' : 'rot'} />
              </span>
            </button>
          </div>
          <div className="panel-scroll">
            <section className="panel-section">
              <div className="panel-title-row sheet-extra">
                <h1 className="panel-title">Forecast map</h1>
                <ProvenanceBadge provenance={fc?.provenance ?? meta.provenance} compact />
              </div>
              <p className="panel-intro sheet-extra">
                Forecasts of <em>publicly reported</em> incidents for each 500 m cell — an area-level research view, not a prediction about
                any person or address.
              </p>
              <div className="field-grid sheet-extra">
                <SelectField
                  id="category"
                  label="Incident category"
                  value={sel.category}
                  onChange={(v) => changePair({ category: v })}
                  options={meta.categories.map((c) => ({ value: c.id, label: c.label }))}
                />
                <SelectField
                  id="model"
                  label="Model"
                  value={sel.model}
                  onChange={(v) => changePair({ model: v })}
                  options={mapModels.map((m) => ({ value: m.id, label: m.label }))}
                />
              </div>
              <Segmented
                name="horizon"
                label="Forecast horizon"
                value={sel.horizon}
                onChange={(v) => update({ horizon: v })}
                options={meta.horizons_h.map((h) => ({
                  value: String(h),
                  label: horizonLabel(h),
                  title: horizonLong(h),
                  disabled: !!fc && !fc.horizons?.[String(h)],
                }))}
              />
              <Segmented
                name="metric"
                label="Colour cells by"
                value={sel.metric}
                onChange={(v) => update({ metric: v })}
                options={METRICS.map((m) => ({
                  value: m.id,
                  label: m.short,
                  title: m.label,
                  disabled: !metricAvailable(m.id),
                }))}
              />
              <p className="field-help sheet-extra">{metricHelp}</p>
            </section>

            {hz ? (
              <section className="window-card" aria-label="Forecast window">
                <div className="window-kicker">
                  {isLatest ? 'Latest forecast' : fc?.provenance?.kind === 'replay' ? 'Historical replay' : 'Past forecast'} ·{' '}
                  {horizonLong(horizon)}
                </div>
                <div className="window-time">{fmtWindow(hz.window_start, hz.window_end)}</div>
                <div className="window-total">
                  <span className="window-total-n">{fmtCount(hz.total_expected)}</span> {category?.label.toLowerCase() ?? ''} reports expected
                  citywide
                </div>
              </section>
            ) : null}

            <div className="legend-inline">
              <Legend scheme={scheme} metric={metric} horizon={horizon} overlays={overlays} opacities={CLASS_OPACITY[scheme.kind][theme]} compact />
            </div>

            <section className="panel-section sheet-extra">
              <div className="section-head">
                <h2>Forecast runs</h2>
                <span className="small muted">replay frozen forecasts</span>
              </div>
              <ForecastTimeline items={runs} currentId={forecastId} latestId={latestId} onSelect={(id) => update({ forecastId: id })} />
            </section>

            {fc && scored ? (
              <section className="panel-section sheet-extra">
                <ScorePanel
                  forecast={fc}
                  horizon={horizon}
                  showObserved={sel.showObserved}
                  onShowObserved={(v) => update({ showObserved: v })}
                />
              </section>
            ) : null}

            <section className="panel-section sheet-extra">
              <div className="section-head">
                <h2>
                  <LayersIcon size={15} /> Overlay
                </h2>
                {history.data ? <ProvenanceBadge provenance={history.data.provenance} compact /> : null}
              </div>
              <Switch
                id="show-history"
                checked={sel.showHistory}
                onChange={(v) => update({ showHistory: v })}
                label="Historical reported incidents"
                description="Aggregated counts per cell — never individual incidents."
              />
              {sel.showHistory ? (
                <>
                  <Segmented
                    name="period"
                    label="Period"
                    size="sm"
                    value={sel.period}
                    onChange={(v) => update({ period: v })}
                    options={PERIODS.map((p) => ({ value: p.value, label: p.label }))}
                  />
                  {history.error ? (
                    <p className="small note-provisional">No historical counts published for this category.</p>
                  ) : (
                    <p className="small muted">
                      Dots scale with the count. Cells with fewer than {history.data?.min_count ?? 3} reports are hatched and their counts
                      withheld.
                    </p>
                  )}
                </>
              ) : null}
              {history.data?.hour_of_week_citywide ? (
                <WeeklyPattern values={history.data.hour_of_week_citywide} highlight={windowHours} />
              ) : null}
            </section>

            <details className="disclosure sheet-extra">
              <summary>Table view — top 10 cells</summary>
              <p className="small muted">Ranked by {METRICS.find((m) => m.id === metric)!.label.toLowerCase()}. Select an area to show it on the map.</p>
              <TopCells rows={topCells} metric={metric} onFocus={(id) => setFocus({ cellId: id, nonce: Date.now() })} />
            </details>

            {fc ? (
              <details className="disclosure sheet-extra">
                <summary>Provenance</summary>
                <ProvenanceDetails forecast={fc} model={model} />
              </details>
            ) : null}
          </div>
          <div className="panel-footer sheet-extra">
            <DisclaimerLine />
          </div>
          <div className="sheet-attrib">
            Basemap © <a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> ©{' '}
            <a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a> ©{' '}
            <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>
          </div>
        </aside>
      ) : null}

      {sel && hz ? (
        <div className="legend-card">
          <Legend scheme={scheme} metric={metric} horizon={horizon} overlays={overlays} opacities={CLASS_OPACITY[scheme.kind][theme]} />
        </div>
      ) : null}
      <span className="sr-only" aria-live="polite">
        {hz && sel
          ? `Map shows ${METRICS.find((m) => m.id === metric)!.label.toLowerCase()} for the ${horizonLong(horizon)}, ${fmtWindow(hz.window_start, hz.window_end)}.`
          : ''}
      </span>
    </div>
  );
}
