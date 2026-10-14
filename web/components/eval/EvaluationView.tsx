'use client';

import { useMemo, useState, type ReactNode } from 'react';
import { useData, useDoc } from '../Providers';
import { MaturityChip, ProvenanceBadge, Segmented, SelectField } from '../ui';
import { SiteFooter } from '../SiteFooter';
import { CaptureBars } from '../charts/CaptureBars';
import { DotRows } from '../charts/DotRows';
import { Reliability } from '../charts/Reliability';
import { SeriesChart, type Series } from '../charts/SeriesChart';
import { ModelLegend, seriesColor, TableView } from '../charts/common';
import { fmtDate, fmtNum, fmtPct, fmtPp, fmtRatio, fmtShortDate } from '@/lib/format';
import { ablationLabel, horizonLabel, modelFallbackLabel } from '@/lib/labels';
import type { EvaluationSummary, MetricRow, StabilityRow } from '@/lib/types';

const LIVE_EMPTY =
  'Live forecasts are frozen hourly; scores appear after SPD publishes the corresponding reports (typically 1–3 days) and become final after 30 days.';

const sourceOf = (r: { source?: string }) => (r.source === 'replay' ? 'replay' : 'live');

function Card({
  title,
  sub,
  children,
  span = 12,
  aside,
}: {
  title: string;
  sub?: ReactNode;
  children: ReactNode;
  span?: 4 | 6 | 8 | 12;
  aside?: ReactNode;
}) {
  return (
    <section className={`card${span !== 12 ? ` span-${span}` : ''}`}>
      <div className="card-head">
        <h3 className="card-title">{title}</h3>
        {aside}
      </div>
      {sub ? <p className="card-sub">{sub}</p> : null}
      {children}
    </section>
  );
}

export function EvaluationView() {
  const data = useData();
  const meta = data.status === 'ready' ? data.meta : null;
  const summary = useDoc<EvaluationSummary>(meta ? 'evaluation/summary.json' : null);
  const [category, setCategory] = useState<string | null>(null);
  const [horizon, setHorizon] = useState<string | null>(null);
  const [eventTimeSel, setEventTime] = useState<string | null>(null);

  const cat = category ?? (meta?.categories.some((c) => c.id === 'property') ? 'property' : meta?.categories[0]?.id) ?? 'property';
  const hs = meta?.horizons_h ?? [1, 3, 6, 24];
  const h = Number(horizon ?? (hs.includes(6) ? 6 : hs[0]));
  const raw = summary.data;

  // Rows may be published for several event-time definitions (report vs offense time);
  // show one at a time, defaulting to the platform's primary definition.
  const eventTimes = useMemo(
    () => [...new Set((raw?.backtest?.rows ?? []).map((r) => r.event_time).filter((x): x is string => !!x))],
    [raw],
  );
  const et = eventTimeSel ?? (eventTimes.includes(meta?.event_time ?? 'report') ? (meta?.event_time ?? 'report') : eventTimes[0]);
  const s = useMemo(() => (raw ? filterEventTime(raw, et) : null), [raw, et]);

  // Colour order: meta.models first, then any extra model ids found in the summary (e.g. baselines).
  const models = useMemo(() => {
    const out = (meta?.models ?? []).map((m) => ({ id: m.id, label: m.label }));
    const seen = new Set(out.map((m) => m.id));
    const ids = [
      ...(s?.backtest?.rows ?? []).map((r) => r.model_id),
      ...(s?.prospective?.rows ?? []).map((r) => r.model_id),
      ...(s?.prospective?.timeseries ?? []).map((r) => r.model_id),
    ];
    for (const id of ids)
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push({ id, label: modelFallbackLabel(id) });
      }
    return out;
  }, [meta, s]);
  const modelIdx = (id: string) => models.findIndex((m) => m.id === id);
  const modelLabel = (id: string) => models.find((m) => m.id === id)?.label ?? id;
  const catLabel = meta?.categories.find((c) => c.id === cat)?.label ?? cat;

  const backtest = useMemo(() => (s?.backtest?.rows ?? []).filter((r) => r.category === cat), [s, cat]);
  const backtestH = backtest.filter((r) => r.horizon_h === h);

  if (data.status === 'error') {
    return (
      <div className="page">
        <div className="center-msg">
          <h1>Data unavailable</h1>
          <p className="mono small">{data.message}</p>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="page">
        <div className="page-head">
          <div>
            <h1>Model evaluation</h1>
            <p>
              How well do the forecasts rank areas where incidents are later <em>reported</em>? The headline metric is{' '}
              <strong>capture@k%</strong>: the share of reported incidents that fell in the k% of cells the model ranked highest. A
              random ranking captures about k%.
            </p>
          </div>
          {s ? <ProvenanceBadge provenance={s.provenance} /> : null}
        </div>

        {meta ? (
          <div className="filter-bar" role="group" aria-label="Filters">
            <SelectField
              id="eval-category"
              label="Incident category"
              value={cat}
              onChange={setCategory}
              options={meta.categories.map((c) => ({ value: c.id, label: c.label }))}
            />
            <Segmented
              name="eval-horizon"
              label="Horizon"
              value={String(h)}
              onChange={setHorizon}
              options={hs.map((x) => ({ value: String(x), label: horizonLabel(x) }))}
            />
            {eventTimes.length > 1 ? (
              <Segmented
                name="eval-event-time"
                label="Event time"
                value={et ?? ''}
                onChange={setEventTime}
                options={eventTimes.map((x) => ({ value: x, label: x === 'report' ? 'Report time' : x === 'offense' ? 'Offense time' : x }))}
              />
            ) : null}
            <p className="filter-note">Filters apply to every chart below; the capture comparison shows all horizons and highlights the selected one.</p>
          </div>
        ) : null}

        {summary.loading && !s ? <p className="empty">Loading evaluation summary…</p> : null}
        {summary.error ? (
          <p className="empty">No evaluation summary has been published yet ({summary.error.message}).</p>
        ) : null}

        {s ? (
          <>
            {/* ------------------------------------------------------------------ */}
            <section className="eval-section" aria-labelledby="bt-h">
              <div className="eval-section-head">
                <h2 id="bt-h">Historical backtest</h2>
                <span className="section-tag">Out-of-sample · fixed test window</span>
                {s.backtest?.test_window ? (
                  <span className="small muted">
                    Test window {fmtDate(s.backtest.test_window.start)} – {fmtDate(s.backtest.test_window.end)}
                  </span>
                ) : null}
              </div>
              <p className="section-lede">
                Models were trained on earlier data and scored on a held-out period, using only features that would have been available at
                each issue time. Backtest numbers are mature: all late reports for the period had arrived.
              </p>

              {backtestH.length > 0 ? (
                <div className="kpis">
                  {backtestH.map((r) => (
                    <div className="kpi" key={r.model_id}>
                      <div className="kpi-label">
                        <span className="sw" style={{ background: seriesColor(modelIdx(r.model_id)) }} aria-hidden="true" />
                        {modelLabel(r.model_id)} · capture@10%, {horizonLabel(h)}
                      </div>
                      <div className="kpi-value">{fmtPct(r.capture_10, 1)}</div>
                      <div className="kpi-sub">
                        {r.ci?.capture_10 ? `CI ${fmtPct(r.ci.capture_10[0], 1)}–${fmtPct(r.ci.capture_10[1], 1)} · ` : ''}
                        {r.pai_10 != null ? `${fmtNum(r.pai_10, 1)}× random` : ''}
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="cards">
                <Card
                  title={`Capture by area share — ${catLabel}`}
                  sub="Share of reported incidents inside the top 5%, 10% and 20% of cells ranked by each model. Dashed ticks show what a random ranking would capture; whiskers are the published confidence intervals."
                >
                  {backtest.length ? (
                    <>
                      <ul className="chart-legend">
                        {models.map((m, i) => (
                          <li key={m.id}>
                            <span className="sw" style={{ background: seriesColor(i) }} aria-hidden="true" />
                            {m.label}
                          </li>
                        ))}
                        <li>
                          <span className="sw-ref" aria-hidden="true" />
                          Random ranking
                        </li>
                      </ul>
                      <CaptureBars rows={backtest} models={models} horizons={hs} selected={h} />
                      <TableView>
                        <MetricTable rows={backtest} modelLabel={modelLabel} cols={['capture_5', 'capture_10', 'capture_20']} />
                      </TableView>
                    </>
                  ) : (
                    <p className="empty">No backtest rows for this category.</p>
                  )}
                </Card>

                <Card
                  span={6}
                  title="Lift over the seasonal baseline"
                  sub="Capture@10% relative to a seasonal-average forecast (same cell, hour of week and season). Above 1.0× means the model adds information beyond the usual pattern."
                >
                  {backtest.length ? (
                    <>
                      <ModelLegend models={models} shape="dot" />
                      <DotRows
                        rows={hs.map((x) => ({
                          key: String(x),
                          label: horizonLabel(x),
                          highlight: x === h,
                          values: Object.fromEntries(
                            backtest.filter((r) => r.horizon_h === x).map((r) => [r.model_id, r.lift_vs_seasonal_10 ?? null]),
                          ),
                        }))}
                        models={models}
                        format={(v) => `${v.toFixed(2)}×`}
                        reference={{ value: 1, label: 'seasonal baseline = 1.0×' }}
                        labelWidth={48}
                      />
                      <TableView>
                        <MetricTable rows={backtest} modelLabel={modelLabel} cols={['lift_vs_seasonal_10']} />
                      </TableView>
                    </>
                  ) : (
                    <p className="empty">No backtest rows.</p>
                  )}
                </Card>

                <Card
                  span={6}
                  title={`Calibration — ${horizonLabel(h)}`}
                  sub="Do forecast probabilities match how often incidents were actually reported? Points on the diagonal are well calibrated; below it, the model is over-confident."
                >
                  <CalibrationBlock s={s} cat={cat} h={h} models={models} modelLabel={modelLabel} />
                </Card>

                <Card
                  title={`Accuracy metrics — ${catLabel}`}
                  sub="Lower is better for the error and scoring metrics; higher is better for capture, PAI and lift. Best value per horizon in bold."
                >
                  {backtest.length ? (
                    <div className="table-wrap">
                      <MetricTable
                        rows={backtest}
                        modelLabel={modelLabel}
                        cols={['capture_10', 'pai_10', 'lift_vs_seasonal_10', 'brier', 'nll', 'poisson_deviance', 'mae', 'rmse', 'ece']}
                        highlightBest
                        selectedH={h}
                      />
                    </div>
                  ) : (
                    <p className="empty">No backtest rows.</p>
                  )}
                </Card>

                <Card
                  span={6}
                  title={`Ablations — ${horizonLabel(h)}`}
                  sub="Capture@10% when one feature group is removed and the model retrained. Negative = the group helped."
                >
                  <AblationBlock s={s} cat={cat} h={h} modelLabel={modelLabel} />
                </Card>

                <Card
                  span={6}
                  title={`Stability by precinct — ${horizonLabel(h)}`}
                  sub="Capture@10% computed separately within each SPD precinct. Large gaps can signal uneven performance across the city."
                >
                  <StabilityGroups rows={s.stability?.by_precinct ?? []} groupKey="precinct" cat={cat} h={h} models={models} />
                </Card>

                <Card
                  title={`Stability over time — ${horizonLabel(h)}`}
                  sub="Capture@10% by calendar month of the test window."
                >
                  <StabilityMonths rows={s.stability?.by_month ?? []} cat={cat} h={h} models={models} />
                </Card>
              </div>
            </section>

            {/* ------------------------------------------------------------------ */}
            <ProspectiveSection s={s} cat={cat} catLabel={catLabel} h={h} models={models} modelLabel={modelLabel} />
          </>
        ) : null}
      </div>
      <SiteFooter />
    </>
  );
}

// ---------------------------------------------------------------------------

const COL_LABEL: Record<string, string> = {
  capture_5: 'Capture@5%',
  capture_10: 'Capture@10%',
  capture_20: 'Capture@20%',
  pai_10: 'PAI@10%',
  lift_vs_seasonal_10: 'Lift vs seasonal',
  brier: 'Brier',
  nll: 'NLL',
  poisson_deviance: 'Poisson dev.',
  mae: 'MAE',
  rmse: 'RMSE',
  ece: 'ECE',
};
const HIGHER_BETTER = new Set(['capture_5', 'capture_10', 'capture_20', 'pai_10', 'lift_vs_seasonal_10']);

function fmtCol(col: string, v: number | null | undefined) {
  if (v === null || v === undefined) return '—';
  if (col.startsWith('capture')) return fmtPct(v, 1);
  if (col === 'pai_10') return fmtNum(v, 2);
  if (col === 'lift_vs_seasonal_10') return fmtRatio(v);
  if (col === 'ece' || col === 'brier') return v < 0.001 ? v.toExponential(1) : fmtNum(v, 4);
  return fmtNum(v, 4);
}

function MetricTable({
  rows,
  modelLabel,
  cols,
  highlightBest = false,
  selectedH,
  extraCols = [],
}: {
  rows: MetricRow[];
  modelLabel: (id: string) => string;
  cols: string[];
  highlightBest?: boolean;
  selectedH?: number;
  extraCols?: { key: string; label: string; render: (r: MetricRow) => ReactNode }[];
}) {
  const sorted = [...rows].sort((a, b) => a.horizon_h - b.horizon_h || a.model_id.localeCompare(b.model_id));
  const best = new Map<string, number>();
  if (highlightBest) {
    for (const r of sorted)
      for (const c of cols) {
        const v = r[c as keyof MetricRow] as number | null | undefined;
        if (typeof v !== 'number') continue;
        const k = `${r.horizon_h}|${c}`;
        const cur = best.get(k);
        if (cur === undefined || (HIGHER_BETTER.has(c) ? v > cur : v < cur)) best.set(k, v);
      }
  }
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Horizon</th>
          <th scope="col">Model</th>
          {extraCols.map((c) => (
            <th scope="col" key={c.key}>
              {c.label}
            </th>
          ))}
          {cols.map((c) => (
            <th scope="col" key={c} className="num">
              {COL_LABEL[c] ?? c}
              {highlightBest && c === 'capture_10' && rows.some((r) => r.ci?.capture_10) ? <span className="muted"> ± CI</span> : null}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((r, i) => (
          <tr
            key={`${r.horizon_h}-${r.model_id}-${r.source ?? ''}-${r.maturity ?? ''}`}
            className={i > 0 && sorted[i - 1].horizon_h !== r.horizon_h ? 'group-start' : undefined}
            style={selectedH === r.horizon_h ? { background: 'var(--surface-2)' } : undefined}
          >
            <td>{horizonLabel(r.horizon_h)}</td>
            <td>{modelLabel(r.model_id)}</td>
            {extraCols.map((c) => (
              <td key={c.key}>{c.render(r)}</td>
            ))}
            {cols.map((c) => {
              const v = r[c as keyof MetricRow] as number | null | undefined;
              const isBest = highlightBest && typeof v === 'number' && best.get(`${r.horizon_h}|${c}`) === v;
              const ci = c === 'capture_10' ? r.ci?.capture_10 : undefined;
              return (
                <td key={c} className={`num${isBest ? ' best' : ''}`}>
                  {fmtCol(c, v)}
                  {ci && highlightBest ? <span className="muted small"> ±{((ci[1] - ci[0]) * 50).toFixed(1)}</span> : null}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CalibrationBlock({
  s,
  cat,
  h,
  models,
  modelLabel,
}: {
  s: EvaluationSummary;
  cat: string;
  h: number;
  models: { id: string; label: string }[];
  modelLabel: (id: string) => string;
}) {
  const entries = (s.calibration ?? []).filter((c) => c.category === cat && c.horizon_h === h);
  if (entries.length === 0) return <p className="empty">No calibration bins for this selection.</p>;
  return (
    <>
      <ul className="chart-legend">
        {models.map((m, i) =>
          entries.some((e) => e.model_id === m.id) ? (
            <li key={m.id}>
              <span className="sw-line" style={{ background: seriesColor(i) }} aria-hidden="true" />
              {m.label}
            </li>
          ) : null,
        )}
        <li>
          <span className="sw-ref" aria-hidden="true" />
          Perfect calibration
        </li>
      </ul>
      <Reliability entries={entries} models={models} />
      <TableView>
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" className="num">
                Mean forecast
              </th>
              <th scope="col" className="num">
                Observed rate
              </th>
              <th scope="col" className="num">
                Cell-windows
              </th>
            </tr>
          </thead>
          <tbody>
            {entries.flatMap((e) =>
              e.bins.map((b, i) => (
                <tr key={`${e.model_id}-${i}`}>
                  <td>{modelLabel(e.model_id)}</td>
                  <td className="num">{fmtPct(b.p_mean, 2)}</td>
                  <td className="num">{fmtPct(b.obs_rate, 2)}</td>
                  <td className="num">{b.n.toLocaleString('en-US')}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </TableView>
    </>
  );
}

function AblationBlock({ s, cat, h, modelLabel }: { s: EvaluationSummary; cat: string; h: number; modelLabel: (id: string) => string }) {
  const base = (s.ablations ?? []).filter((a) => a.category === cat && a.horizon_h === h);
  if (base.length === 0) return <p className="empty">No ablation runs for this selection.</p>;
  const full = s.backtest?.rows.find((r) => r.category === cat && r.horizon_h === h && r.model_id === base[0].model_id)?.capture_10;
  // "Change vs full" is always ablated − full (negative = the removed group helped). We derive it from the
  // two capture values when both exist, so it does not depend on the sign convention of delta_vs_full.
  const rows = base
    .map((r) => ({ ...r, change: typeof r.capture_10 === 'number' && typeof full === 'number' ? r.capture_10 - full : (r.delta_vs_full ?? 0) }))
    .sort((a, b) => a.change - b.change);
  const maxAbs = Math.max(0.005, ...rows.map((r) => Math.abs(r.change)));
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">Removed ({modelLabel(rows[0].model_id)})</th>
            <th scope="col" className="num">
              Capture@10%
            </th>
            <th scope="col" className="num">
              Change vs full model
            </th>
          </tr>
        </thead>
        <tbody>
          {full != null ? (
            <tr>
              <td>
                <strong>Full model</strong>
              </td>
              <td className="num">{fmtPct(full, 1)}</td>
              <td className="num muted">—</td>
            </tr>
          ) : null}
          {rows.map((r) => {
            const d = r.change;
            const w = (Math.abs(d) / maxAbs) * 60;
            return (
              <tr key={`${r.name}-${r.model_id}`}>
                <td>{ablationLabel(r.name)}</td>
                <td className="num">{fmtPct(r.capture_10, 1)}</td>
                <td className="num">
                  <span className="delta-cell">
                    {fmtPp(d)}
                    <span className="delta-bar" aria-hidden="true">
                      <span
                        className="delta-fill"
                        style={{
                          left: d < 0 ? `${60 - w}px` : '60px',
                          width: `${Math.max(1, w)}px`,
                          background: d < 0 ? 'var(--series-2)' : 'var(--series-1)',
                        }}
                      />
                    </span>
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Accept flat rows or parent rows carrying a nested array (e.g. {model_id, horizon_h, by_precinct: [...]}) */
function flattenStability(rows: StabilityRow[]): StabilityRow[] {
  const out: StabilityRow[] = [];
  for (const r of rows) {
    const nested = Object.entries(r).find(([, v]) => Array.isArray(v) && v.length > 0 && typeof v[0] === 'object');
    if (nested) {
      const { [nested[0]]: _drop, ...parent } = r;
      for (const child of nested[1] as StabilityRow[]) out.push({ ...parent, ...child });
    } else if (!Object.values(r).some((v) => Array.isArray(v))) {
      out.push(r);
    }
  }
  return out;
}

function rowsFor(rows: StabilityRow[], cat: string, h: number) {
  return flattenStability(rows).filter(
    (r) => (r.category === undefined || r.category === cat) && (r.horizon_h === undefined || r.horizon_h === h) && typeof r.capture_10 === 'number',
  );
}

type WithET = { event_time?: string };
const etOk = (r: WithET, et: string | undefined) => !et || !r.event_time || r.event_time === et;

function filterEventTime(s: EvaluationSummary, et: string | undefined): EvaluationSummary {
  if (!et) return s;
  return {
    ...s,
    backtest: s.backtest ? { ...s.backtest, rows: s.backtest.rows.filter((r) => etOk(r, et)) } : s.backtest,
    prospective: s.prospective
      ? { rows: s.prospective.rows.filter((r) => etOk(r, et)), timeseries: s.prospective.timeseries.filter((r) => etOk(r, et)) }
      : s.prospective,
    ablations: s.ablations?.filter((r) => etOk(r, et)),
    calibration: s.calibration?.filter((r) => etOk(r, et)),
    stability: s.stability
      ? {
          by_precinct: s.stability.by_precinct?.filter((r) => etOk(r as WithET, et)),
          by_month: s.stability.by_month?.filter((r) => etOk(r as WithET, et)),
        }
      : s.stability,
  };
}

function StabilityGroups({
  rows,
  groupKey,
  cat,
  h,
  models,
}: {
  rows: StabilityRow[];
  groupKey: string;
  cat: string;
  h: number;
  models: { id: string; label: string }[];
}) {
  const sel = rowsFor(rows, cat, h);
  if (sel.length === 0) return <p className="empty">Not available for this selection.</p>;
  const groups = [...new Set(sel.map((r) => String(r[groupKey] ?? r.group ?? '—')))];
  const dotRows = groups.map((g) => ({
    key: g,
    label: g,
    values: Object.fromEntries(
      sel.filter((r) => String(r[groupKey] ?? r.group) === g).map((r) => [r.model_id ?? models[0]?.id ?? 'model', (r.capture_10 as number) ?? null]),
    ),
  }));
  return (
    <>
      <ModelLegend models={models} shape="dot" />
      <DotRows rows={dotRows} models={models} format={(v) => fmtPct(v, 1)} labelWidth={84} />
      <TableView>
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Group</th>
              <th scope="col">Model</th>
              <th scope="col" className="num">
                Capture@10%
              </th>
              <th scope="col" className="num">
                Reports
              </th>
            </tr>
          </thead>
          <tbody>
            {sel.map((r, i) => (
              <tr key={i}>
                <td>{String(r[groupKey] ?? r.group ?? '—')}</td>
                <td>{models.find((m) => m.id === r.model_id)?.label ?? r.model_id}</td>
                <td className="num">{fmtPct(r.capture_10 as number, 1)}</td>
                <td className="num">{typeof r.n_events === 'number' ? r.n_events.toLocaleString('en-US') : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableView>
    </>
  );
}

function StabilityMonths({ rows, cat, h, models }: { rows: StabilityRow[]; cat: string; h: number; models: { id: string; label: string }[] }) {
  const sel = rowsFor(rows, cat, h).filter((r) => typeof r.month === 'string' || typeof r.month === 'number');
  if (sel.length === 0) return <p className="empty">Not available for this selection.</p>;
  const months = [...new Set(sel.map((r) => r.month as string))].sort();
  const series: Series[] = models
    .map((m, i) => ({
      id: m.id,
      name: m.label,
      color: seriesColor(i),
      points: months.map((mo, xi) => {
        const r = sel.find((q) => q.month === mo && q.model_id === m.id);
        return { x: xi, y: typeof r?.capture_10 === 'number' ? r.capture_10 : null };
      }),
    }))
    .filter((s) => s.points.some((p) => p.y !== null));
  const label = (mo: string) => {
    const [y, mm] = mo.split('-').map(Number);
    return new Date(Date.UTC(y, (mm || 1) - 1, 15)).toLocaleString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' });
  };
  const step = Math.ceil(months.length / 8);
  return (
    <>
      <ModelLegend models={models} shape="line" />
      <SeriesChart
        series={series}
        xDomain={[-0.3, months.length - 0.7]}
        xTicks={months.map((mo, i) => ({ value: i, label: label(mo) })).filter((_, i) => i % step === 0)}
        yFormat={(v) => fmtPct(v, 0)}
        xLabel={(x) => label(months[x])}
        height={230}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Prospective
// ---------------------------------------------------------------------------

function ProspectiveSection({
  s,
  cat,
  catLabel,
  h,
  models,
  modelLabel,
}: {
  s: EvaluationSummary;
  cat: string;
  catLabel: string;
  h: number;
  models: { id: string; label: string }[];
  modelLabel: (id: string) => string;
}) {
  const rows = (s.prospective?.rows ?? []).filter((r) => r.category === cat && r.horizon_h === h);
  const ts = (s.prospective?.timeseries ?? []).filter((r) => r.category === cat && r.horizon_h === h);
  const liveRows = rows.filter((r) => sourceOf(r) === 'live');
  const replayRows = rows.filter((r) => sourceOf(r) === 'replay');
  const liveTs = ts.filter((r) => sourceOf(r) === 'live');
  const hasReplay = replayRows.length > 0 || ts.some((r) => sourceOf(r) === 'replay');
  const anyLive = (s.prospective?.rows ?? []).some((r) => sourceOf(r) === 'live') || (s.prospective?.timeseries ?? []).some((r) => sourceOf(r) === 'live');

  const times = ts.map((p) => new Date(p.issued_at).getTime());
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const provTimes = ts.filter((p) => p.maturity === 'provisional').map((p) => new Date(p.issued_at).getTime());
  const liveStart = liveTs.length ? Math.min(...liveTs.map((p) => new Date(p.issued_at).getTime())) : null;
  const span = t1 - t0 || 86400e3;

  const series: Series[] = [];
  let aggregated = false;
  models.forEach((m, i) => {
    for (const src of ['replay', 'live'] as const) {
      const raw = ts
        .filter((p) => p.model_id === m.id && sourceOf(p) === src && typeof p.capture_10 === 'number')
        .sort((a, b) => (a.issued_at < b.issued_at ? -1 : 1));
      // Hourly issuance can mean thousands of points: average per day (weighted by reports) when dense.
      const daily = raw.length > 150;
      aggregated ||= daily;
      const groups = new Map<string, typeof raw>();
      for (const p of raw) {
        const k = daily ? p.issued_at.slice(0, 10) : p.issued_at;
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k)!.push(p);
      }
      const pts = [...groups.entries()].map(([k, g]) => {
        const wsum = g.reduce((a, p) => a + (p.observed_total ?? 1), 0);
        const y = g.reduce((a, p) => a + (p.capture_10 as number) * (p.observed_total ?? 1), 0) / Math.max(wsum, 1e-9);
        const prov = g.some((p) => p.maturity === 'provisional');
        return {
          x: daily ? new Date(`${k}T12:00:00Z`).getTime() : new Date(k).getTime(),
          y,
          hollow: prov,
          square: src === 'replay',
          note: `${src === 'replay' ? 'replay' : 'live'}, ${prov ? 'provisional' : 'mature'}${daily ? `, ${g.length} forecasts` : ''}`,
        };
      });
      if (pts.length) series.push({ id: `${m.id}-${src}`, name: m.label, color: seriesColor(i), points: pts, faint: src === 'replay' });
    }
  });

  const tickCount = 6;
  const xTicks = Array.from({ length: tickCount }, (_, i) => t0 + (span * i) / (tickCount - 1)).map((v) => ({ value: v, label: fmtShortDate(v) }));

  return (
    <section className="eval-section" aria-labelledby="pr-h">
      <div className="eval-section-head">
        <h2 id="pr-h">Prospective</h2>
        <span className="section-tag live">Frozen before the window · scored as reports arrive</span>
      </div>
      <p className="section-lede">
        <strong>Live</strong> forecasts are frozen in real time and scored later against what SPD publishes. Recent scores are{' '}
        <MaturityChip maturity="provisional" /> until the window is 30 days old, because late reports keep arriving; then they become{' '}
        <MaturityChip maturity="mature" />.
        {hasReplay ? (
          <>
            {' '}
            <strong>Historical replay</strong> forecasts are reconstructed for past issue times using only data that was available then — a
            simulation from real data and real models, shown separately from live results.
          </>
        ) : null}
      </p>

      {!anyLive ? (
        <div className="callout callout-info" role="note">
          <p>
            <strong>No live scores yet.</strong> {LIVE_EMPTY}
          </p>
        </div>
      ) : null}

      <div className="cards">
        <Card
          title={`Capture@10% by issue time — ${catLabel}, ${horizonLabel(h)}`}
          sub={`${aggregated ? 'Points are daily averages of scored forecasts (weighted by reports).' : 'Each point is one scored forecast.'} Hollow points are provisional; squares on faint lines are historical replays, circles are live forecasts.`}
        >
          {ts.length ? (
            <>
              <ul className="chart-legend">
                {models.map((m, i) => (
                  <li key={m.id}>
                    <span className="sw-line" style={{ background: seriesColor(i) }} aria-hidden="true" />
                    {m.label}
                  </li>
                ))}
                <li>
                  <span className="sw-dot" style={{ background: 'var(--ink-2)' }} aria-hidden="true" />
                  mature
                </li>
                <li>
                  <span className="sw-hollow" aria-hidden="true" />
                  provisional
                </li>
                {hasReplay ? (
                  <li>
                    <span className="sw" style={{ background: 'var(--ink-2)', width: 9, height: 9, borderRadius: 1 }} aria-hidden="true" />
                    replay
                  </li>
                ) : null}
                <li>
                  <span className="sw-ref" aria-hidden="true" />
                  random (10%)
                </li>
              </ul>
              <SeriesChart
                series={series}
                xDomain={[t0 - span * 0.02, t1 + span * 0.02]}
                xTicks={xTicks}
                yFormat={(v) => fmtPct(v, 0)}
                yTitle="Capture@10%"
                reference={{ y: 0.1, label: 'random ranking' }}
                bands={provTimes.length ? [{ x0: Math.min(...provTimes) - span * 0.01, x1: t1 + span * 0.02, label: 'Provisional' }] : []}
                markers={liveStart !== null && hasReplay ? [{ x: liveStart - span * 0.005, label: 'Live →' }] : []}
                xLabel={(x) => fmtDate(new Date(x).toISOString())}
                height={280}
              />
              <TableView>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Issued</th>
                      <th scope="col">Model</th>
                      <th scope="col">Source</th>
                      <th scope="col">Maturity</th>
                      <th scope="col" className="num">
                        Capture@10%
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...ts]
                      .sort((a, b) => (a.issued_at < b.issued_at ? 1 : -1))
                      .map((p, i) => (
                        <tr key={i}>
                          <td>{fmtDate(p.issued_at)}</td>
                          <td>{modelLabel(p.model_id)}</td>
                          <td>{sourceOf(p) === 'replay' ? 'Replay' : 'Live'}</td>
                          <td>{p.maturity}</td>
                          <td className="num">{fmtPct(p.capture_10, 1)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </TableView>
            </>
          ) : (
            <p className="empty">{LIVE_EMPTY}</p>
          )}
        </Card>

        <Card span={6} title="Live forecasts" sub="Frozen hourly in real time; scored against reports SPD has published so far.">
          {liveRows.length ? (
            <ProspectiveTable rows={liveRows} modelLabel={modelLabel} />
          ) : (
            <p className="empty">{LIVE_EMPTY}</p>
          )}
        </Card>
        <Card
          span={6}
          title="Historical replay"
          aside={hasReplay ? <ProvenanceBadge provenance={{ kind: 'replay', generated_at: '' }} /> : null}
          sub="Reconstructed as-of past issue times with only then-available data. Useful for volume, but not a substitute for live results."
        >
          {replayRows.length ? (
            <ProspectiveTable rows={replayRows} modelLabel={modelLabel} />
          ) : (
            <p className="empty">No replay results published.</p>
          )}
        </Card>
      </div>
    </section>
  );
}

function ProspectiveTable({ rows, modelLabel }: { rows: MetricRow[]; modelLabel: (id: string) => string }) {
  const sorted = [...rows].sort((a, b) => a.model_id.localeCompare(b.model_id) || (a.maturity ?? '').localeCompare(b.maturity ?? ''));
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Maturity</th>
            <th scope="col" className="num">
              Forecasts
            </th>
            <th scope="col" className="num">
              Capture@10%
            </th>
            <th scope="col" className="num">
              Lift
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr key={i}>
              <td>{modelLabel(r.model_id)}</td>
              <td>
                <MaturityChip maturity={r.maturity} />
              </td>
              <td className="num">{r.n_forecasts?.toLocaleString('en-US') ?? '—'}</td>
              <td className="num">{fmtPct(r.capture_10, 1)}</td>
              <td className="num">{fmtRatio(r.lift_vs_seasonal_10)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
