'use client';

import { useState } from 'react';
import { fmtPct } from '@/lib/format';
import { horizonLabel } from '@/lib/labels';
import { linear, niceMax, niceTicks } from '@/lib/scales';
import type { MetricRow } from '@/lib/types';
import { barPath, ChartTip, seriesColor, useWidth, type TipState } from './common';

const KS = [
  { key: 'capture_5', k: 0.05, label: 'Top 5%' },
  { key: 'capture_10', k: 0.1, label: 'Top 10%' },
  { key: 'capture_20', k: 0.2, label: 'Top 20%' },
] as const;

/**
 * Small multiples (one panel per horizon) of grouped bars: capture@5/10/20% by model,
 * with the random-ranking reference (= k) and 95% intervals when present.
 */
export function CaptureBars({
  rows,
  models,
  horizons,
  selected,
  yMax,
}: {
  rows: MetricRow[];
  models: { id: string; label: string }[];
  horizons: number[];
  selected: number;
  yMax?: number;
}) {
  let max = 0;
  for (const r of rows)
    for (const k of KS) {
      const v = r[k.key] ?? 0;
      const hi = r.ci?.[k.key]?.[1] ?? 0;
      max = Math.max(max, v, hi);
    }
  const top = yMax ?? Math.max(0.3, niceMax(max * 1.05, 0.1));
  return (
    <div className="small-multiples">
      {horizons.map((h) => (
        <Panel
          key={h}
          horizon={h}
          rows={rows.filter((r) => r.horizon_h === h)}
          models={models}
          top={top}
          selected={h === selected}
        />
      ))}
    </div>
  );
}

function Panel({
  horizon,
  rows,
  models,
  top,
  selected,
}: {
  horizon: number;
  rows: MetricRow[];
  models: { id: string; label: string }[];
  top: number;
  selected: boolean;
}) {
  const [ref, width] = useWidth<HTMLDivElement>(260);
  const [tip, setTip] = useState<TipState | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const H = 210;
  const m = { l: 34, r: 6, t: 26, b: 26 };
  const iw = Math.max(80, width - m.l - m.r);
  const ih = H - m.t - m.b;
  const y = linear(0, top, ih, 0);
  const band = iw / KS.length;
  const n = Math.max(1, models.length);
  const barW = Math.min(24, ((band - 14) - 2 * (n - 1)) / n);
  const groupW = n * barW + (n - 1) * 2;
  const ticks = niceTicks(0, top, 4);
  const byModel = new Map(rows.map((r) => [r.model_id, r]));

  return (
    <div className={`sm-panel chart${selected ? ' is-selected' : ''}`} ref={ref}>
      <svg width={width} height={H} role="img" aria-label={`Capture by area share, ${horizonLabel(horizon)} horizon`}>
        <text className="panel-label" x={m.l} y={14}>
          {horizonLabel(horizon)} horizon
        </text>
        <g transform={`translate(${m.l},${m.t})`}>
          {ticks.map((t) => (
            <g key={t}>
              <line className="grid-line" x1={0} x2={iw} y1={y(t)} y2={y(t)} />
              <text className="axis-label" x={-6} y={y(t)} dy="0.32em" textAnchor="end">
                {fmtPct(t)}
              </text>
            </g>
          ))}
          {KS.map((k, gi) => {
            const gx = gi * band + (band - groupW) / 2;
            return (
              <g key={k.key}>
                {models.map((mod, mi) => {
                  const r = byModel.get(mod.id);
                  const v = r?.[k.key];
                  if (v === null || v === undefined) return null;
                  const x = gx + mi * (barW + 2);
                  const ci = r?.ci?.[k.key];
                  const id = `${k.key}-${mod.id}`;
                  return (
                    <g key={mod.id}>
                      <path
                        d={barPath(x, y(v), barW, ih - y(v))}
                        style={{ fill: seriesColor(mi) }}
                        className={hover === id ? 'mark-hover' : undefined}
                      />
                      {ci ? (
                        <g className="ci">
                          <line className="ci-line" x1={x + barW / 2} x2={x + barW / 2} y1={y(ci[0])} y2={y(ci[1])} />
                          <line className="ci-line" x1={x + barW / 2 - 3} x2={x + barW / 2 + 3} y1={y(ci[1])} y2={y(ci[1])} />
                          <line className="ci-line" x1={x + barW / 2 - 3} x2={x + barW / 2 + 3} y1={y(ci[0])} y2={y(ci[0])} />
                        </g>
                      ) : null}
                      <rect
                        className="hover-target"
                        x={x - 1}
                        y={0}
                        width={barW + 2}
                        height={ih}
                        tabIndex={0}
                        aria-label={`${mod.label}, ${k.label}: ${fmtPct(v, 1)}`}
                        onPointerEnter={() => setHover(id)}
                        onPointerLeave={() => {
                          setHover(null);
                          setTip(null);
                        }}
                        onFocus={() => setHover(id)}
                        onBlur={() => {
                          setHover(null);
                          setTip(null);
                        }}
                        onPointerMove={() => show()}
                        onFocusCapture={() => show()}
                      />
                    </g>
                  );
                  function show() {
                    setTip({
                      x: m.l + x + barW / 2,
                      y: m.t + y(v as number),
                      title: `${mod.label} · ${horizonLabel(horizon)} · ${k.label}`,
                      rows: [
                        { color: seriesColor(mi), name: 'Capture', value: fmtPct(v, 1), shape: 'square' },
                        ...(ci ? [{ name: '95% interval', value: `${fmtPct(ci[0], 1)} – ${fmtPct(ci[1], 1)}` }] : []),
                        { name: 'Random ranking', value: fmtPct(k.k) },
                      ],
                    });
                  }
                })}
                <line className="ref-line" x1={gx - 5} x2={gx + groupW + 5} y1={y(k.k)} y2={y(k.k)} />
                <text className="axis-label" x={gi * band + band / 2} y={ih + 17} textAnchor="middle">
                  {band < 62 ? k.label.replace('Top ', '') : k.label}
                </text>
              </g>
            );
          })}
          <line className="base-line" x1={0} x2={iw} y1={ih} y2={ih} />
        </g>
      </svg>
      <ChartTip tip={tip} />
    </div>
  );
}
