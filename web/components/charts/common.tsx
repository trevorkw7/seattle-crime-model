'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';

/** Width of an element, tracked with ResizeObserver. */
export function useWidth<T extends HTMLElement>(fallback = 600) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && Math.abs(w - width) > 0.5) setWidth(Math.floor(w));
    });
    ro.observe(el);
    setWidth(Math.floor(el.getBoundingClientRect().width) || fallback);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return [ref, width] as const;
}

export interface TipRow {
  color?: string;
  name: string;
  value: string;
  shape?: 'line' | 'dot' | 'square' | 'hollow';
}

export interface TipState {
  x: number;
  y: number;
  title: string;
  rows: TipRow[];
}

/** Tooltip: values lead (strong), series names follow; keyed with a short line, not a box. */
export function ChartTip({ tip }: { tip: TipState | null }) {
  if (!tip) return null;
  return (
    <div className="chart-tip" style={{ left: tip.x, top: tip.y }} role="presentation">
      <div className="tip-title">{tip.title}</div>
      {tip.rows.map((r, i) => (
        <div className="tip-row" key={i}>
          {r.color ? (
            <span
              className="tip-key"
              style={
                r.shape === 'hollow'
                  ? { boxShadow: `inset 0 0 0 1.5px ${r.color}`, height: 8, width: 8, borderRadius: 4 }
                  : r.shape === 'square'
                    ? { background: r.color, height: 8, width: 8, borderRadius: 1 }
                    : r.shape === 'dot'
                      ? { background: r.color, height: 8, width: 8, borderRadius: 4 }
                      : { background: r.color }
              }
            />
          ) : null}
          <span className="tip-name">{r.name}</span>
          <span className="tip-val">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

export function seriesColor(index: number) {
  return `var(--series-${(index % 8) + 1})`;
}

export function ModelLegend({ models, shape = 'square' }: { models: { id: string; label: string }[]; shape?: 'square' | 'line' | 'dot' }) {
  return (
    <ul className="chart-legend" aria-label="Models">
      {models.map((m, i) => (
        <li key={m.id}>
          <span
            className={shape === 'line' ? 'sw-line' : shape === 'dot' ? 'sw-dot' : 'sw'}
            style={{ background: seriesColor(i) }}
            aria-hidden="true"
          />
          {m.label}
        </li>
      ))}
    </ul>
  );
}

export function TableView({ children, label = 'Table view' }: { children: ReactNode; label?: string }) {
  return (
    <details className="table-view">
      <summary>{label}</summary>
      <div className="table-wrap">{children}</div>
    </details>
  );
}

/** Rect with 4px rounded data-end, square at the baseline. */
export function barPath(x: number, y: number, w: number, h: number, r = 4) {
  if (h <= 0) return '';
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}
