'use client';

import { useMemo, useRef, useState } from 'react';
import { linear, niceTicks } from '@/lib/scales';
import { ChartTip, useWidth, type TipRow, type TipState } from './common';

export interface SeriesPoint {
  x: number;
  y: number | null;
  /** hollow marker = provisional */
  hollow?: boolean;
  /** square marker = historical replay */
  square?: boolean;
  note?: string;
}

export interface Series {
  id: string;
  name: string;
  color: string;
  points: SeriesPoint[];
  /** de-emphasised line (e.g. replay) */
  faint?: boolean;
}

/**
 * Line chart with a crosshair tooltip that snaps to the nearest x and lists every
 * series at that x. Supports shaded bands (e.g. "provisional"), vertical markers
 * and one horizontal reference line.
 */
export function SeriesChart({
  series,
  xDomain,
  xTicks,
  xTitle,
  yDomain,
  yFormat,
  yTitle,
  reference,
  bands = [],
  markers = [],
  height = 260,
  xLabel,
  showMarkers = true,
}: {
  series: Series[];
  xDomain: [number, number];
  xTicks: { value: number; label: string }[];
  xTitle?: string;
  yDomain?: [number, number];
  yFormat: (v: number) => string;
  yTitle?: string;
  reference?: { y: number; label: string };
  bands?: { x0: number; x1: number; label: string }[];
  markers?: { x: number; label: string }[];
  height?: number;
  xLabel: (x: number) => string;
  showMarkers?: boolean;
}) {
  const [ref, width] = useWidth<HTMLDivElement>(700);
  const [tip, setTip] = useState<TipState | null>(null);
  const [cross, setCross] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const m = { l: yTitle ? 60 : 44, r: 16, t: 18, b: xTitle ? 44 : 28 };
  const iw = Math.max(120, width - m.l - m.r);
  const ih = height - m.t - m.b;

  const [y0, y1] = useMemo(() => {
    if (yDomain) return yDomain;
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of series)
      for (const p of s.points)
        if (p.y !== null) {
          lo = Math.min(lo, p.y);
          hi = Math.max(hi, p.y);
        }
    if (reference) {
      lo = Math.min(lo, reference.y);
      hi = Math.max(hi, reference.y);
    }
    if (!Number.isFinite(lo)) return [0, 1] as [number, number];
    const pad = (hi - lo) * 0.1 || 0.05;
    return [Math.max(0, lo - pad), hi + pad] as [number, number];
  }, [series, yDomain, reference]);

  const x = linear(xDomain[0], xDomain[1], 0, iw);
  const y = linear(y0, y1, ih, 0);
  const yTicks = niceTicks(y0, y1, 5);
  const xs = useMemo(() => [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort((a, b) => a - b), [series]);
  const dense = xs.length > 40;
  const r = dense ? 3 : 4;

  const pathFor = (pts: SeriesPoint[]) => {
    let d = '';
    let pen = false;
    for (const p of pts) {
      if (p.y === null) {
        pen = false;
        continue;
      }
      d += `${pen ? 'L' : 'M'}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`;
      pen = true;
    }
    return d;
  };

  const onMove = (clientX: number) => {
    const svg = svgRef.current;
    if (!svg || xs.length === 0) return;
    const rect = svg.getBoundingClientRect();
    const px = clientX - rect.left - m.l;
    const xv = xDomain[0] + (px / iw) * (xDomain[1] - xDomain[0]);
    let best = xs[0];
    for (const v of xs) if (Math.abs(v - xv) < Math.abs(best - xv)) best = v;
    setCross(best);
    const rows: TipRow[] = [];
    let yTop = ih;
    for (const s of series) {
      const p = s.points.find((q) => q.x === best && q.y !== null);
      if (!p || p.y === null) continue;
      yTop = Math.min(yTop, y(p.y));
      rows.push({
        color: s.color,
        name: `${s.name}${p.note ? ` · ${p.note}` : ''}`,
        value: yFormat(p.y),
        shape: p.hollow ? 'hollow' : p.square ? 'square' : 'dot',
      });
    }
    if (rows.length === 0) {
      setTip(null);
      return;
    }
    setTip({ x: m.l + x(best), y: m.t + yTop - 4, title: xLabel(best), rows });
  };

  return (
    <div className="chart" ref={ref}>
      <svg
        ref={svgRef}
        width={width}
        height={height}
        role="img"
        aria-label={yTitle ?? 'Line chart'}
        onPointerMove={(e) => onMove(e.clientX)}
        onPointerLeave={() => {
          setTip(null);
          setCross(null);
        }}
      >
        <g transform={`translate(${m.l},${m.t})`}>
          {bands.map((b, i) => {
            const bx0 = Math.max(0, x(b.x0));
            const bx1 = Math.min(iw, x(b.x1));
            if (bx1 <= bx0) return null;
            return (
              <g key={i}>
                <rect x={bx0} y={0} width={bx1 - bx0} height={ih} style={{ fill: 'var(--prov-bg)', opacity: 0.75 }} />
                <text className="ref-label" x={bx0 + 6} y={12} style={{ fill: 'var(--prov-ink)', fontWeight: 600 }}>
                  {b.label}
                </text>
              </g>
            );
          })}
          {yTicks.map((t) => (
            <g key={t}>
              <line className="grid-line" x1={0} x2={iw} y1={y(t)} y2={y(t)} />
              <text className="axis-label" x={-8} y={y(t)} dy="0.32em" textAnchor="end">
                {yFormat(t)}
              </text>
            </g>
          ))}
          {xTicks.map((t) => (
            <text key={t.value} className="axis-label" x={x(t.value)} y={ih + 17} textAnchor="middle">
              {t.label}
            </text>
          ))}
          <line className="base-line" x1={0} x2={iw} y1={ih} y2={ih} />
          {reference ? (
            <g>
              <line className="ref-line" x1={0} x2={iw} y1={y(reference.y)} y2={y(reference.y)} />
              <text className="ref-label" x={4} y={y(reference.y) - 5}>
                {reference.label}
              </text>
            </g>
          ) : null}
          {markers.map((mk, i) => (
            <g key={i}>
              <line x1={x(mk.x)} x2={x(mk.x)} y1={0} y2={ih} style={{ stroke: 'var(--ink-2)', strokeWidth: 1 }} />
              <text className="ref-label" x={x(mk.x) + 5} y={12} style={{ fontWeight: 600 }}>
                {mk.label}
              </text>
            </g>
          ))}
          {cross !== null ? <line x1={x(cross)} x2={x(cross)} y1={0} y2={ih} style={{ stroke: 'var(--ink-2)', strokeWidth: 1 }} /> : null}
          {series.map((s) => (
            <path
              key={s.id}
              d={pathFor(s.points)}
              style={{ fill: 'none', stroke: s.color, strokeWidth: s.faint ? 1.5 : 2, strokeOpacity: s.faint ? 0.55 : 1, strokeLinejoin: 'round', strokeLinecap: 'round' }}
            />
          ))}
          {showMarkers
            ? series.map((s) =>
                s.points.map((p, i) => {
                  if (p.y === null) return null;
                  const cx = x(p.x);
                  const cy = y(p.y);
                  const hot = cross === p.x;
                  const rr = hot ? r + 1.5 : r;
                  const style = p.hollow
                    ? { fill: 'var(--surface)', stroke: s.color, strokeWidth: 1.75 }
                    : { fill: s.color, stroke: 'var(--surface)', strokeWidth: 1.5 };
                  return p.square ? (
                    <rect key={`${s.id}-${i}`} x={cx - rr} y={cy - rr} width={rr * 2} height={rr * 2} rx={1} style={style} />
                  ) : (
                    <circle key={`${s.id}-${i}`} cx={cx} cy={cy} r={rr} style={style} />
                  );
                }),
              )
            : null}
          {yTitle ? (
            <text className="axis-title" transform={`translate(${-(m.l - 12)},${ih / 2}) rotate(-90)`} textAnchor="middle">
              {yTitle}
            </text>
          ) : null}
          {xTitle ? (
            <text className="axis-title" x={iw / 2} y={ih + 36} textAnchor="middle">
              {xTitle}
            </text>
          ) : null}
        </g>
      </svg>
      <ChartTip tip={tip} />
    </div>
  );
}
