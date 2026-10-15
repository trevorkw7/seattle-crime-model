'use client';

import { useEffect, useMemo, useRef } from 'react';
import * as maplibregl from 'maplibre-gl';
import type { GeoJSONSource, Map as MLMap, MapLayerMouseEvent, StyleSpecification } from 'maplibre-gl';
import { BASEMAP_STYLES, MAPLIBRE_WORKER_URL } from '@/lib/config';
import { FALLBACK_BACKGROUND, MAP_INK, type Theme } from '@/lib/colors';
import type { GridCollection } from '@/lib/types';

export interface PopupRow {
  label: string;
  value: string;
  sub?: string;
}

export interface PopupModel {
  title: string;
  subtitle: string;
  window?: string;
  rows: PopupRow[];
  notes: string[];
}

export interface MapViewProps {
  grid: GridCollection;
  bbox: [number, number, number, number];
  theme: Theme;
  /** fill colour + opacity per cell id (cells without an entry are transparent) */
  fills: Map<string, { color: string; opacity: number }>;
  /** historical counts overlay; null value = suppressed */
  history: { counts: Record<string, number | null>; max: number } | null;
  /** observed counts during the forecast window (scored forecasts) */
  observed: Record<string, number> | null;
  describe: (cellId: string) => PopupModel | null;
  focus: { cellId: string; nonce: number } | null;
  padding: { top: number; right: number; bottom: number; left: number };
  compactControls: boolean;
  dim: boolean;
  onBasemapStatus?: (status: 'ok' | 'fallback' | 'degraded') => void;
}

type Centroids = Map<string, [number, number]>;

function centroidsOf(grid: GridCollection): Centroids {
  const out: Centroids = new Map();
  for (const f of grid.features) {
    const ring = f.geometry.type === 'Polygon' ? f.geometry.coordinates[0] : f.geometry.coordinates[0][0];
    const pts = ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1] ? ring.slice(0, -1) : ring;
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

function fallbackStyle(theme: Theme): StyleSpecification {
  return {
    version: 8,
    sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': FALLBACK_BACKGROUND[theme] } }],
  };
}

async function loadStyle(theme: Theme): Promise<{ spec: StyleSpecification; fallback: boolean }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(BASEMAP_STYLES[theme], { signal: ctrl.signal });
    if (!res.ok) throw new Error(`style HTTP ${res.status}`);
    const spec = (await res.json()) as StyleSpecification;
    return { spec, fallback: false };
  } catch {
    return { spec: fallbackStyle(theme), fallback: true };
  } finally {
    clearTimeout(timer);
  }
}

/** 45° hatch tile used for suppressed historical cells. */
function hatchImage(rgb: [number, number, number]) {
  const size = 16;
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = (x + y) % 8;
      const a = d === 0 ? 150 : d === 1 || d === 7 ? 45 : 0;
      const i = (y * size + x) * 4;
      data[i] = rgb[0];
      data[i + 1] = rgb[1];
      data[i + 2] = rgb[2];
      data[i + 3] = a;
    }
  }
  return { width: size, height: size, data };
}

function popupNode(m: PopupModel): HTMLElement {
  const el = (tag: string, cls?: string, text?: string) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const root = el('div', 'pp');
  root.append(el('div', 'pp-title', m.title));
  root.append(el('div', 'pp-sub', m.subtitle));
  if (m.window) root.append(el('div', 'pp-window', m.window));
  const dl = el('dl', 'pp-rows');
  for (const r of m.rows) {
    dl.append(el('dt', undefined, r.label));
    const dd = el('dd');
    dd.append(el('span', 'pp-val', r.value));
    if (r.sub) dd.append(el('span', 'pp-rowsub', r.sub));
    dl.append(dd);
  }
  root.append(dl);
  for (const n of m.notes) root.append(el('p', 'pp-note', n));
  return root;
}

export default function MapView(props: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const propsRef = useRef(props);
  propsRef.current = props;
  const hoverRef = useRef<string | null>(null);
  const pinnedRef = useRef<string | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const themeRef = useRef<Theme>(props.theme);
  const centroids = useMemo(() => centroidsOf(props.grid), [props.grid]);
  const centroidsRef = useRef(centroids);
  centroidsRef.current = centroids;

  // ---- data builders -------------------------------------------------------
  const buildCells = () => {
    const { grid, fills, history, observed } = propsRef.current;
    return {
      type: 'FeatureCollection' as const,
      features: grid.features.map((f) => {
        const id = f.properties.cell_id;
        const hv = history ? history.counts[id] : undefined;
        const fill = fills.get(id);
        return {
          type: 'Feature' as const,
          geometry: f.geometry,
          properties: {
            cell_id: id,
            fill: fill?.color ?? null,
            fo: fill?.opacity ?? 0,
            hs: history && hv === null ? 1 : 0,
            obs: observed ? (observed[id] ?? 0) : 0,
          },
        };
      }),
    };
  };
  const buildDots = () => {
    const { history } = propsRef.current;
    const features = [];
    if (history) {
      for (const [id, v] of Object.entries(history.counts)) {
        const c = centroidsRef.current.get(id);
        if (!c || v === null || !(v > 0)) continue;
        features.push({
          type: 'Feature' as const,
          geometry: { type: 'Point' as const, coordinates: c },
          properties: { cell_id: id, hc: v, hr: Math.sqrt(v / Math.max(1, history.max)) },
        });
      }
    }
    return { type: 'FeatureCollection' as const, features };
  };

  const pushData = (map: MLMap) => {
    (map.getSource('cells') as GeoJSONSource | undefined)?.setData(buildCells());
    (map.getSource('dots') as GeoJSONSource | undefined)?.setData(buildDots());
  };

  const install = (map: MLMap) => {
    if (map.getSource('cells')) return;
    const theme = themeRef.current;
    const ink = MAP_INK[theme];
    if (!map.hasImage('hatch')) map.addImage('hatch', hatchImage(ink.hatch), { pixelRatio: 2 });
    const before = map.getStyle().layers?.find((l) => l.type === 'symbol')?.id;
    map.addSource('cells', { type: 'geojson', data: buildCells(), promoteId: 'cell_id' });
    map.addSource('dots', { type: 'geojson', data: buildDots(), promoteId: 'cell_id' });
    map.addLayer(
      {
        id: 'cells-fill',
        type: 'fill',
        source: 'cells',
        paint: {
          'fill-color': ['coalesce', ['get', 'fill'], 'rgba(0,0,0,0)'],
          'fill-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], ['min', 1, ['+', ['get', 'fo'], 0.15]], ['get', 'fo']],
          'fill-opacity-transition': { duration: 0 },
        },
      },
      before,
    );
    map.addLayer(
      {
        id: 'cells-gap',
        type: 'line',
        source: 'cells',
        filter: ['!=', ['get', 'fill'], null],
        paint: {
          'line-color': ink.gap,
          'line-opacity': theme === 'dark' ? 0.55 : 0.7,
          'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.3, 13, 1, 15, 1.6],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: 'hist-hatch',
        type: 'fill',
        source: 'cells',
        filter: ['==', ['get', 'hs'], 1],
        paint: { 'fill-pattern': 'hatch', 'fill-opacity': 0.9 },
      },
      before,
    );
    map.addLayer(
      {
        id: 'observed-line',
        type: 'line',
        source: 'cells',
        filter: ['>', ['get', 'obs'], 0],
        paint: {
          'line-color': ink.observed,
          'line-width': ['interpolate', ['linear'], ['get', 'obs'], 1, 1.6, 3, 2.6, 6, 3.4],
          'line-offset': ['interpolate', ['linear'], ['get', 'obs'], 1, 0.8, 3, 1.3, 6, 1.7],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: 'hist-dots',
        type: 'circle',
        source: 'dots',
        paint: {
          'circle-color': ink.ink,
          'circle-opacity': 0.7,
          'circle-stroke-color': ink.halo,
          'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 10, 0.6, 13, 1.2],
          'circle-radius': [
            'interpolate',
            ['linear'],
            ['zoom'],
            10,
            ['+', 0.6, ['*', 3.6, ['get', 'hr']]],
            13,
            ['+', 1.5, ['*', 10, ['get', 'hr']]],
            15,
            ['+', 3, ['*', 22, ['get', 'hr']]],
          ],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: 'cells-hover',
        type: 'line',
        source: 'cells',
        paint: {
          'line-color': ink.ink,
          'line-width': ['case', ['boolean', ['feature-state', 'pinned'], false], 2.6, ['boolean', ['feature-state', 'hover'], false], 2, 0],
        },
      },
      before,
    );
    if (pinnedRef.current) map.setFeatureState({ source: 'cells', id: pinnedRef.current }, { pinned: true });
    containerRef.current?.setAttribute('data-grid-ready', 'true');
  };

  // ---- popups --------------------------------------------------------------
  const showPopup = (map: MLMap, cellId: string, lngLat: maplibregl.LngLatLike, pinned: boolean) => {
    const model = propsRef.current.describe(cellId);
    if (!model) return;
    const existing = popupRef.current;
    if (existing && existing.isOpen() && (existing as unknown as { _pinned?: boolean })._pinned === pinned) {
      existing.setLngLat(lngLat).setDOMContent(popupNode(model));
      return;
    }
    existing?.remove();
    const popup = new maplibregl.Popup({
      closeButton: pinned,
      closeOnClick: false,
      closeOnMove: false,
      focusAfterOpen: false,
      maxWidth: '300px',
      offset: 12,
      className: `cell-popup${pinned ? ' is-pinned' : ''}`,
    })
      .setLngLat(lngLat)
      .setDOMContent(popupNode(model))
      .addTo(map);
    (popup as unknown as { _pinned?: boolean })._pinned = pinned;
    popup.on('close', () => {
      if (popupRef.current === popup && pinned) unpin(map);
    });
    popupRef.current = popup;
  };

  const setHover = (map: MLMap, id: string | null) => {
    if (hoverRef.current === id) return;
    if (hoverRef.current && map.getSource('cells')) map.setFeatureState({ source: 'cells', id: hoverRef.current }, { hover: false });
    hoverRef.current = id;
    if (id && map.getSource('cells')) map.setFeatureState({ source: 'cells', id }, { hover: true });
  };

  const unpin = (map: MLMap) => {
    if (pinnedRef.current && map.getSource('cells')) map.setFeatureState({ source: 'cells', id: pinnedRef.current }, { pinned: false });
    pinnedRef.current = null;
  };

  const pin = (map: MLMap, id: string, lngLat: maplibregl.LngLatLike) => {
    unpin(map);
    pinnedRef.current = id;
    if (map.getSource('cells')) map.setFeatureState({ source: 'cells', id }, { pinned: true });
    showPopup(map, id, lngLat, true);
  };

  // ---- create the map once -----------------------------------------------
  useEffect(() => {
    let cancelled = false;
    let map: MLMap | null = null;
    try {
      maplibregl.setWorkerUrl(MAPLIBRE_WORKER_URL);
    } catch {
      /* older builds bundle the worker */
    }
    (async () => {
      const { spec, fallback } = await loadStyle(themeRef.current);
      if (cancelled || !containerRef.current) return;
      const p = propsRef.current;
      propsRef.current.onBasemapStatus?.(fallback ? 'fallback' : 'ok');
      const mapInst = new maplibregl.Map({
        container: containerRef.current,
        style: spec,
        bounds: p.bbox,
        fitBoundsOptions: { padding: p.padding },
        attributionControl: false,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        maxZoom: 16.5,
        minZoom: 8.5,
        cooperativeGestures: false,
      });
      map = mapInst;
      const mp = mapInst;
      mp.touchZoomRotate.disableRotation();
      mp.keyboard.disableRotation();
      mp.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
      // On phones the attribution lives in the bottom sheet (see MapExplorer), which
      // would otherwise cover the map's bottom corners.
      if (!p.compactControls) {
        mp.addControl(
          new maplibregl.AttributionControl({
            compact: false,
            customAttribution: fallback ? 'Basemap unavailable — showing grid only' : undefined,
          }),
          'bottom-right',
        );
      }
      mapRef.current = mp;

      let tileErrors = 0;
      mp.on('error', (e) => {
        // Basemap tile/glyph failures must not break the grid; report once, quietly.
        tileErrors++;
        if (tileErrors === 1) {
          propsRef.current.onBasemapStatus?.('degraded');
          console.warn('[map] basemap resource failed to load:', (e.error as Error | undefined)?.message ?? e);
        }
      });
      mp.on('style.load', () => install(mp));
      if (mp.isStyleLoaded()) install(mp);
      mp.on('load', () => install(mp));

      const canHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
      mp.on('mousemove', 'cells-fill', (e: MapLayerMouseEvent) => {
        const id = e.features?.[0]?.properties?.cell_id as string | undefined;
        if (!id || !canHover) return;
        mp.getCanvas().style.cursor = 'pointer';
        setHover(mp, id);
        if (!pinnedRef.current) showPopup(mp, id, e.lngLat, false);
      });
      mp.on('mouseleave', 'cells-fill', () => {
        mp.getCanvas().style.cursor = '';
        setHover(mp, null);
        if (!pinnedRef.current) {
          popupRef.current?.remove();
          popupRef.current = null;
        }
      });
      mp.on('click', (e) => {
        const hit = mp.getLayer('cells-fill') ? mp.queryRenderedFeatures(e.point, { layers: ['cells-fill'] }) : [];
        const id = hit[0]?.properties?.cell_id as string | undefined;
        if (id && propsRef.current.fills.has(id)) {
          pin(mp, id, e.lngLat);
        } else {
          unpin(mp);
          popupRef.current?.remove();
          popupRef.current = null;
        }
      });
    })();
    return () => {
      cancelled = true;
      popupRef.current?.remove();
      map?.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- push data when inputs change ---------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getSource('cells')) return;
    pushData(map);
    if (pinnedRef.current && popupRef.current?.isOpen()) {
      const model = props.describe(pinnedRef.current);
      if (model) popupRef.current.setDOMContent(popupNode(model));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.fills, props.history, props.observed, props.describe]);

  // ---- theme switch: swap basemap, re-install layers ----------------------
  useEffect(() => {
    if (themeRef.current === props.theme) return;
    themeRef.current = props.theme;
    const map = mapRef.current;
    if (!map) return;
    let cancelled = false;
    loadStyle(props.theme).then(({ spec, fallback }) => {
      if (cancelled) return;
      propsRef.current.onBasemapStatus?.(fallback ? 'fallback' : 'ok');
      map.setStyle(spec, { diff: false });
    });
    return () => {
      cancelled = true;
    };
  }, [props.theme]);

  // ---- focus a cell (from the table view) ---------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !props.focus) return;
    const c = centroids.get(props.focus.cellId);
    if (!c) return;
    map.flyTo({ center: c, zoom: Math.max(map.getZoom(), 13), padding: props.padding, duration: 700 });
    pin(map, props.focus.cellId, c);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.focus]);

  return (
    <div
      ref={containerRef}
      className={`map-canvas${props.dim ? ' is-dim' : ''}`}
      data-testid="map"
      role="region"
      aria-label="Forecast map of Seattle 500 m grid cells. Use the table view in the panel for a keyboard-accessible list."
    />
  );
}
