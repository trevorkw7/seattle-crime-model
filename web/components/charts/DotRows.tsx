'use client';

import { useState } from 'react';
import { linear, niceTicks } from '@/lib/scales';
import { ChartTip, seriesColor, useWidth, type TipState } from './common';

export interface DotRow {
  key: string;
  label: string;
  /** value per model id */
  values: Record<string, number | null | undefined>;
  highlight?: boolean;
}

/**
 * Horizontal dot plot / dumbbell: one row per group, one dot per model, a thin
 * connector showing the spread, and an optional reference line (e.g. 1.0 = baseline).
 */
export function DotRows({
  rows,
  models,
  format,
  reference,
  domain,
  axisTitle,
  labelWidth = 70,
}: {
  rows: DotRow[];
  models: { id: string; label: string }[];
  format: (v: number) => string;
  reference?: { value: number; label: string };
  domain?: [number, number];
  axisTitle?: string;
  labelWidth?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>(480);
  const [tip, setTip] = useState<TipState | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const rowH = 34;
  const m = { l: labelWidth, r: 56, t: reference ? 22 : 8, b: axisTitle ? 40 : 26 };
  const H = m.t + m.b + rows.length * rowH;
  const iw = Math.max(80, width - m.l - m.r);

  let lo = Infinity;
  let hi = -Infinity;
  for (const r of rows)
    for (const v of Object.values(r.values))
      if (typeof v === 'number') {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
  if (reference) {
    lo = Math.min(lo, reference.value);
    hi = Math.max(hi, reference.value);
  }
  if (!Number.isFinite(lo)) {
    lo = 0;
    hi = 1;
  }
  const pad = (hi - lo) * 0.12 || 0.05;
  const [d0, d1] = domain ?? [lo - pad, hi + pad];
  const x = linear(d0, d1, 0, iw);
  const ticks = niceTicks(d0, d1, Math.max(3, Math.floor(iw / 90)));

  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={H} role="img" aria-label={axisTitle ?? 'Dot plot'}>
        <g transform={`translate(${m.l},${m.t})`}>
          {ticks.map((t) => (
            <g key={t}>
              <line className="grid-line" x1={x(t)} x2={x(t)} y1={0} y2={rows.length * rowH} />
              <text className="axis-label" x={x(t)} y={rows.length * rowH + 16} textAnchor="middle">
                {format(t)}
              </text>
            </g>
          ))}
          {reference ? (
            <g>
              <line className="base-line" x1={x(reference.value)} x2={x(reference.value)} y1={-4} y2={rows.length * rowH} strokeWidth={1.5} />
            </g>
          ) : null}
          {axisTitle ? (
            <text className="axis-title" x={iw / 2} y={rows.length * rowH + 34} textAnchor="middle">
              {axisTitle}
            </text>
          ) : null}
          {rows.map((r, ri) => {
            const cy = ri * rowH + rowH / 2;
            const vals = models
              .map((mod, mi) => ({ mod, mi, v: r.values[mod.id] }))
              .filter((d): d is { mod: { id: string; label: string }; mi: number; v: number } => typeof d.v === 'number');
            if (vals.length === 0) return null;
            const vmin = Math.min(...vals.map((d) => d.v));
            const vmax = Math.max(...vals.map((d) => d.v));
            const best = vals.reduce((a, b) => (b.v > a.v ? b : a));
            return (
              <g key={r.key}>
                {r.highlight ? <rect x={-m.l + 2} y={ri * rowH + 2} width={iw + m.l + m.r - 4} height={rowH - 4} rx={6} style={{ fill: 'var(--surface-2)' }} /> : null}
                <text className="axis-label" x={-10} y={cy} dy="0.32em" textAnchor="end" style={{ fill: 'var(--ink-2)', fontSize: 12 }}>
                  {r.label}
                </text>
                <line x1={x(vmin)} x2={x(vmax)} y1={cy} y2={cy} style={{ stroke: 'var(--axis)', strokeWidth: 2 }} />
                {vals.map(({ mod, mi, v }) => {
                  const id = `${r.key}-${mod.id}`;
                  const show = () =>
                    setTip({
                      x: m.l + x(v),
                      y: m.t + cy - 6,
                      title: r.label,
                      rows: vals.map((d) => ({ color: seriesColor(d.mi), name: d.mod.label, value: format(d.v), shape: 'dot' as const })),
                    });
                  return (
                    <g key={mod.id}>
                      <circle
                        cx={x(v)}
                        cy={cy}
                        r={hover === id ? 6.5 : 5.5}
                        style={{ fill: seriesColor(mi), stroke: 'var(--surface)', strokeWidth: 2 }}
                      />
                      <circle
                        className="hover-target"
                        cx={x(v)}
                        cy={cy}
                        r={12}
                        tabIndex={0}
                        aria-label={`${r.label}, ${mod.label}: ${format(v)}`}
                        onPointerEnter={() => {
                          setHover(id);
                          show();
                        }}
                        onPointerLeave={() => {
                          setHover(null);
                          setTip(null);
                        }}
                        onFocus={() => {
                          setHover(id);
                          show();
                        }}
                        onBlur={() => {
                          setHover(null);
                          setTip(null);
                        }}
                      />
                    </g>
                  );
                })}
                <text className="axis-label" x={x(best.v) + 11} y={cy} dy="0.32em" style={{ fill: 'var(--ink-2)' }}>
                  {format(best.v)}
                </text>
              </g>
            );
          })}
        </g>
        {reference ? (
          <text className="ref-label" x={m.l + x(reference.value)} y={12} textAnchor="middle">
            {reference.label}
          </text>
        ) : null}
      </svg>
      <ChartTip tip={tip} />
    </div>
  );
}
