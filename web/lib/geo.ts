import type { GridCollection } from './types';

export type Centroids = Map<string, [number, number]>;

/** Mean vertex of each grid cell's outer ring (cells are small squares). */
export function cellCentroids(grid: GridCollection): Centroids {
  const out: Centroids = new Map();
  for (const f of grid.features) {
    const ring = f.geometry.type === 'Polygon' ? f.geometry.coordinates[0] : f.geometry.coordinates[0][0];
    const closed = ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1];
    const pts = closed ? ring.slice(0, -1) : ring;
    let x = 0;
    let y = 0;
    for (const p of pts) {
      x += p[0];
      y += p[1];
    }
    out.set(f.properties.cell_id, [x / pts.length, y / pts.length]);
  }
  return out;
}

/** Approximate ground distance in metres (fine at city scale). */
export function metres(a: [number, number], b: [number, number]): number {
  const lat = ((a[1] + b[1]) / 2) * (Math.PI / 180);
  const dx = (a[0] - b[0]) * 111320 * Math.cos(lat);
  const dy = (a[1] - b[1]) * 110540;
  return Math.hypot(dx, dy);
}

/**
 * Grid cell containing a point: the nearest centre, accepted only within half a cell
 * diagonal (so points outside the forecast area return null).
 */
export function cellAt(centroids: Centroids, p: [number, number], cellSizeM: number): string | null {
  let best: string | null = null;
  let bestD = Infinity;
  for (const [id, c] of centroids) {
    const d = metres(p, c);
    if (d < bestD) {
      bestD = d;
      best = id;
    }
  }
  return bestD <= (cellSizeM * Math.SQRT2) / 2 + 10 ? best : null;
}
