'use client';

import { useState } from 'react';
import { fmtPct } from '@/lib/format';
import { linear, logScale, logTicks, niceTicks } from '@/lib/scales';
import type { CalibrationEntry } from '@/lib/types';
import { ChartTip, seriesColor, useWidth, type TipState } from './common';

/** Reliability diagram: mean predicted P(≥1) vs observed rate per bin, one line per model. */
export function Reliability({ entries, models }: { entries: CalibrationEntry[]; models: { id: string; label: string }[] }) {
  const [ref, width] = useWidth<HTMLDivElement>(420);
  const [tip, setTip] = useState<TipState | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const m = { l: 48, r: 14, t: 10, b: 42 };
  const size = Math.min(Math.max(220, width - m.l - m.r), 360);
  const W = size + m.l + m.r;
  const H = size + m.t + m.b;

  const all = entries.flatMap((e) => e.bins.flatMap((b) => [b.p_mean, b.obs_rate])).filter((v) => v > 0);
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const useLog = all.length > 0 && hi / lo > 25;
  const d0 = useLog ? Math.pow(10, Math.floor(Math.log10(lo) * 2) / 2) : 0;
  const d1 = useLog ? Math.min(1, Math.pow(10, Math.ceil(Math.log10(hi) * 2) / 2)) : Math.min(1, hi * 1.1);
  const sx = useLog ? logScale(d0, d1, 0, size) : linear(d0, d1, 0, size);
  const sy = useLog ? logScale(d0, d1, size, 0) : linear(d0, d1, size, 0);
  const lt = useLog ? logTicks(d0, d1) : [];
  const ticks = useLog
    ? lt.length > 7
      ? lt.filter((t) => Math.abs(Math.log10(t) - Math.round(Math.log10(t))) < 1e-9)
      : lt
    : niceTicks(d0, d1, 5);

  return (
    <div className="chart" ref={ref}>
      <svg width={W} height={H} role="img" aria-label="Reliability diagram">
        <g transform={`translate(${m.l},${m.t})`}>
          {ticks.map((t) => (
            <g key={t}>
              <line className="grid-line" x1={sx(t)} x2={sx(t)} y1={0} y2={size} />
              <line className="grid-line" x1={0} x2={size} y1={sy(t)} y2={sy(t)} />
              <text className="axis-label" x={sx(t)} y={size + 15} textAnchor="middle">
                {fmtPct(t, t < 0.01 ? 1 : 0)}
              </text>
              <text className="axis-label" x={-6} y={sy(t)} dy="0.32em" textAnchor="end">
                {fmtPct(t, t < 0.01 ? 1 : 0)}
              </text>
            </g>
          ))}
          <line className="ref-line" x1={sx(d0 || 1e-9)} y1={sy(d0 || 1e-9)} x2={sx(d1)} y2={sy(d1)} />
          <line className="base-line" x1={0} x2={size} y1={size} y2={size} />
          {entries.map((e) => {
            const mi = models.findIndex((mm) => mm.id === e.model_id);
            const mod = models[mi];
            if (!mod) return null;
            const pts = e.bins.filter((b) => b.p_mean > 0 && b.obs_rate >= 0).map((b) => ({ ...b, x: sx(Math.max(b.p_mean, d0)), y: sy(Math.max(b.obs_rate, d0)) }));
            return (
              <g key={e.model_id}>
                <polyline
                  points={pts.map((p) => `${p.x},${p.y}`).join(' ')}
                  style={{ fill: 'none', stroke: seriesColor(mi), strokeWidth: 2, strokeLinejoin: 'round', strokeLinecap: 'round' }}
                />
                {pts.map((p, i) => {
                  const id = `${e.model_id}-${i}`;
                  const show = () =>
                    setTip({
                      x: m.l + p.x,
                      y: m.t + p.y - 4,
                      title: `${mod.label} · bin ${i + 1}`,
                      rows: [
                        { color: seriesColor(mi), name: 'Observed rate', value: fmtPct(p.obs_rate, 2), shape: 'dot' },
                        { name: 'Mean forecast', value: fmtPct(p.p_mean, 2) },
                        { name: 'Cell-windows', value: p.n.toLocaleString('en-US') },
                      ],
                    });
                  return (
                    <g key={i}>
                      <circle cx={p.x} cy={p.y} r={hover === id ? 5.5 : 4} style={{ fill: seriesColor(mi), stroke: 'var(--surface)', strokeWidth: 2 }} />
                      <circle
                        className="hover-target"
                        cx={p.x}
                        cy={p.y}
                        r={11}
                        tabIndex={0}
                        aria-label={`${mod.label}: forecast ${fmtPct(p.p_mean, 2)}, observed ${fmtPct(p.obs_rate, 2)}`}
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
              </g>
            );
          })}
          <text className="axis-title" x={size / 2} y={size + 34} textAnchor="middle">
            Forecast chance of ≥ 1 report{useLog ? ' (log scale)' : ''}
          </text>
          <text className="axis-title" transform={`translate(${-38},${size / 2}) rotate(-90)`} textAnchor="middle">
            Observed share with ≥ 1 report
          </text>
        </g>
      </svg>
      <ChartTip tip={tip} />
    </div>
  );
}
