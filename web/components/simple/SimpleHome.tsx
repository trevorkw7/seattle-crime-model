'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAbout, useData, useDoc, useTheme } from '../Providers';
import { AlertIcon, ArrowRight, ClockIcon } from '../Icons';
import { PlaceSearch } from './PlaceSearch';
import { CLASS_OPACITY } from '@/lib/colors';
import { cellAt, cellCentroids } from '@/lib/geo';
import {
  agoPhrase,
  chancePhrase,
  CITY_RANK_TEXT,
  cityRank,
  compareToUsual,
  COMPARISON_TEXT,
  comparisonDetail,
  countPhrase,
  hoodName,
  PLAIN_CATEGORIES,
  plainCategory,
  reportsShort,
  usualFor,
  windowPhrase,
  type CityRank,
  type Comparison,
} from '@/lib/plain';
import { buildScheme } from '@/lib/scales';
import type { EvaluationSummary, ForecastDoc, ForecastIndex, GridCollection } from '@/lib/types';

const MapView = dynamic(() => import('../map/MapView'), {
  ssr: false,
  loading: () => <div className="map-canvas map-placeholder" />,
});

const HOURS = [
  { value: '6', label: 'Next 6 hours' },
  { value: '24', label: 'Next 24 hours' },
] as const;
type Hours = (typeof HOURS)[number]['value'];

interface CellStat {
  exp: number;
  p: number | null;
  rr: number | null;
  hood: string | null;
}

interface HoodStat {
  name: string;
  ids: string[];
  exp: number;
  /** forecast / usual level over cells that have a seasonal baseline */
  ratio: number | null;
  pAny: number;
  /** expected reports per cell, used to compare areas of different size */
  density: number;
}

type Place =
  | { kind: 'hood'; hood: string }
  | { kind: 'cell'; cell: string; label: string | null; point: [number, number] | null }
  | { kind: 'outside'; label: string; point: [number, number] | null };

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

function useNow(everyMs: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(t);
  }, [everyMs]);
  return now;
}

function readUrl() {
  if (typeof window === 'undefined') return {} as { cat?: string; h?: string; area?: string };
  const q = new URLSearchParams(window.location.search);
  return { cat: q.get('cat') ?? undefined, h: q.get('h') ?? undefined, area: q.get('area') ?? undefined };
}

const pct = (v: number) => `${Math.round(v * 100)}%`;
const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function CompareChip({ c }: { c: Comparison }) {
  return <span className={`tone-chip tone-${c}`}>{COMPARISON_TEXT[c]}</span>;
}
function RankChip({ r }: { r: CityRank }) {
  return <span className={`tone-chip rank-${r}`}>{CITY_RANK_TEXT[r]}</span>;
}

export function SimpleHome() {
  const data = useData();
  const { theme } = useTheme();
  const { setOpen: openAbout } = useAbout();
  const isMobile = useIsMobile();
  const now = useNow(60_000);
  const meta = data.status === 'ready' ? data.meta : null;

  const [category, setCategory] = useState<string>('property');
  const [hours, setHours] = useState<Hours>('6');
  const [place, setPlace] = useState<Place | null>(null);
  const [fit, setFit] = useState<{ ids: string[]; nonce: number } | null>(null);
  const [pendingArea, setPendingArea] = useState<string | null>(null);

  // initial selection from the URL (shareable links)
  useEffect(() => {
    const u = readUrl();
    if (u.cat && PLAIN_CATEGORIES.some((c) => c.id === u.cat)) setCategory(u.cat);
    if (u.h === '6' || u.h === '24') setHours(u.h);
    if (u.area) setPendingArea(u.area);
  }, []);

  const grid = useDoc<GridCollection>(meta ? 'grid.geojson' : null);
  const index = useDoc<ForecastIndex>(meta ? 'forecasts/index.json' : null);
  const evaluation = useDoc<EvaluationSummary>(meta ? 'evaluation/summary.json' : null);

  const categories = useMemo(
    () => PLAIN_CATEGORIES.filter((c) => !meta || meta.categories.some((m) => m.id === c.id)),
    [meta],
  );

  // the default production model, else any model with a forecast for this category
  const modelId = useMemo(() => {
    const latest = index.data?.latest ?? {};
    const order = [meta?.models.find((m) => m.is_default)?.id, 'lgbm', 'seasonal', ...Object.keys(latest)];
    return order.find((m): m is string => !!m && !!latest[m]?.[category]) ?? null;
  }, [index.data, meta, category]);
  const forecastId = modelId ? (index.data?.latest?.[modelId]?.[category] ?? null) : null;
  const forecast = useDoc<ForecastDoc>(forecastId ? `forecasts/${forecastId}.json` : null);
  const fc = forecast.data;
  const hz = fc?.horizons?.[hours] ?? null;

  // ---- per-cell and per-neighborhood numbers --------------------------------
  const hoodOf = useMemo(() => {
    const m = new Map<string, string | null>();
    grid.data?.features.forEach((f) => m.set(f.properties.cell_id, hoodName(f.properties.neighborhood)));
    return m;
  }, [grid.data]);
  const centroids = useMemo(() => (grid.data ? cellCentroids(grid.data) : new Map<string, [number, number]>()), [grid.data]);

  const cells = useMemo(() => {
    const out = new Map<string, CellStat>();
    if (!hz || !fc) return out;
    const ci = (k: string) => fc.columns.indexOf(k);
    const [ie, ip, ir] = [ci('expected'), ci('p_any'), ci('relative_risk')];
    for (const [id, row] of Object.entries(hz.cells)) {
      const e = row?.[ie];
      if (typeof e !== 'number') continue;
      const p = ip >= 0 && typeof row[ip] === 'number' ? (row[ip] as number) : null;
      const rr = ir >= 0 && typeof row[ir] === 'number' ? (row[ir] as number) : null;
      out.set(id, { exp: e, p, rr, hood: hoodOf.get(id) ?? null });
    }
    return out;
  }, [hz, fc, hoodOf]);

  const ratioOf = (list: CellStat[]) => {
    let e = 0;
    let u = 0;
    for (const c of list) {
      if (c.rr && c.rr > 0) {
        e += c.exp;
        u += c.exp / c.rr;
      }
    }
    return u > 0 ? e / u : null;
  };

  const hoods = useMemo(() => {
    const groups = new Map<string, { ids: string[]; list: CellStat[] }>();
    for (const [id, c] of cells) {
      if (!c.hood) continue;
      const g = groups.get(c.hood) ?? { ids: [], list: [] };
      g.ids.push(id);
      g.list.push(c);
      groups.set(c.hood, g);
    }
    const out = new Map<string, HoodStat>();
    for (const [name, g] of groups) {
      const exp = g.list.reduce((s, c) => s + c.exp, 0);
      const pNone = g.list.reduce((s, c) => s * (1 - (c.p ?? 0)), 1);
      out.set(name, { name, ids: g.ids, exp, ratio: ratioOf(g.list), pAny: 1 - pNone, density: exp / g.ids.length });
    }
    return out;
  }, [cells]);

  const hoodNames = useMemo(() => {
    const names = new Set<string>();
    hoodOf.forEach((n) => n && names.add(n));
    return [...names].sort((a, b) => a.localeCompare(b));
  }, [hoodOf]);

  const hoodRank = useMemo(() => {
    const sorted = [...hoods.values()].sort((a, b) => b.density - a.density);
    const m = new Map<string, number>();
    sorted.forEach((h, i) => m.set(h.name, i / Math.max(1, sorted.length)));
    // the list is headed "where the most reports are expected", so it ranks by total, not per square
    const top = [...hoods.values()].filter((h) => h.ids.length >= 2).sort((a, b) => b.exp - a.exp).slice(0, 5);
    return { map: m, top, n: sorted.length };
  }, [hoods]);

  const cellShare = useCallback(
    (id: string) => {
      const v = cells.get(id)?.exp;
      if (v === undefined) return null;
      let above = 0;
      for (const c of cells.values()) if (c.exp > v) above++;
      return above / Math.max(1, cells.size);
    },
    [cells],
  );

  const city = useMemo(() => {
    const list = [...cells.values()];
    return { exp: hz?.total_expected ?? list.reduce((s, c) => s + c.exp, 0), ratio: ratioOf(list) };
  }, [cells, hz]);

  // ---- map colouring ----------------------------------------------------------
  const values = useMemo(() => {
    const m = new Map<string, number | null>();
    for (const [id, c] of cells) m.set(id, c.exp);
    return m;
  }, [cells]);
  const scheme = useMemo(() => buildScheme('expected', values, theme), [values, theme]);
  const fills = useMemo(() => {
    const m = new Map<string, { color: string; opacity: number }>();
    const op = CLASS_OPACITY[scheme.kind][theme];
    for (const [id, c] of scheme.cellClass) m.set(id, { color: scheme.colors[c], opacity: op[c] });
    return m;
  }, [scheme, theme]);

  // ---- choosing a place ---------------------------------------------------------
  const chooseHood = useCallback(
    (name: string) => {
      const ids: string[] = [];
      hoodOf.forEach((n, id) => n === name && ids.push(id));
      if (!ids.length) return;
      setPlace({ kind: 'hood', hood: name });
      setFit({ ids, nonce: Date.now() });
    },
    [hoodOf],
  );
  const choosePoint = useCallback(
    (p: [number, number], label: string) => {
      const id = meta ? cellAt(centroids, p, meta.grid.cell_size_m) : null;
      if (!id) {
        setPlace({ kind: 'outside', label, point: p });
        return;
      }
      setPlace({ kind: 'cell', cell: id, label, point: p });
      setFit({ ids: [id], nonce: Date.now() });
    },
    [centroids, meta],
  );
  const onCellClick = useCallback((id: string | null, p: [number, number]) => {
    if (id) setPlace({ kind: 'cell', cell: id, label: null, point: null });
    else setPlace({ kind: 'outside', label: 'That spot', point: p });
  }, []);

  useEffect(() => {
    if (!pendingArea || hoodNames.length === 0) return;
    const hit = hoodNames.find((n) => n.toLowerCase() === pendingArea.toLowerCase());
    if (hit) chooseHood(hit);
    setPendingArea(null);
  }, [pendingArea, hoodNames, chooseHood]);

  // keep the URL shareable
  useEffect(() => {
    const q = new URLSearchParams();
    if (category !== 'property') q.set('cat', category);
    if (hours !== '6') q.set('h', hours);
    if (place?.kind === 'hood') q.set('area', place.hood);
    const s = q.toString();
    const url = `${window.location.pathname}${s ? `?${s}` : ''}`;
    if (url !== `${window.location.pathname}${window.location.search}`) window.history.replaceState(window.history.state, '', url);
  }, [category, hours, place]);

  // ---- derived text ---------------------------------------------------------------
  const cat = plainCategory(category);
  const isDemo = data.status === 'ready' && (data.demo || fc?.provenance?.kind === 'demo');
  const isReplay = fc?.provenance?.kind === 'replay';
  const win = hz ? windowPhrase(hz.window_start, hz.window_end, new Date(now)) : '';
  const usual = hz ? usualFor(hz.window_start, Number(hours)) : '';
  const stale = !!hz && new Date(hz.window_end).getTime() < now;

  const accuracy = useMemo(() => {
    const rows = evaluation.data?.backtest?.rows ?? [];
    const pick = (m: string | null) =>
      rows.find((r) => r.model_id === m && r.category === category && r.horizon_h === Number(hours) && (r.event_time ?? 'report') === 'report');
    const a = pick(modelId);
    const b = pick('seasonal');
    if (typeof a?.capture_10 !== 'number') return null;
    return { model: a.capture_10, usual: modelId !== 'seasonal' && typeof b?.capture_10 === 'number' ? b.capture_10 : null, window: evaluation.data?.backtest?.test_window };
  }, [evaluation.data, category, hours, modelId]);

  const selectedIds = useMemo(() => {
    if (!place) return [];
    if (place.kind === 'cell') return [place.cell];
    if (place.kind === 'hood') return hoods.get(place.hood)?.ids ?? [];
    return [];
  }, [place, hoods]);

  // ---- render -------------------------------------------------------------------
  if (data.status === 'error') {
    return (
      <div className="center-msg">
        <h1>Forecasts are unavailable</h1>
        <p>The forecast data could not be loaded. Please try again later.</p>
      </div>
    );
  }

  const ready = !!(meta && grid.data);
  const padding = isMobile ? { top: 16, right: 16, bottom: 16, left: 16 } : { top: 40, right: 40, bottom: 40, left: 40 };
  const marker = place && place.kind !== 'hood' && 'point' in place ? place.point : null;

  return (
    <div className="simple-page">
      <div className="simple-head">
        <h1>Where crime reports are expected in Seattle</h1>
        {hz ? (
          stale ? (
            <p className="fresh-note is-stale" role="status">
              <AlertIcon size={16} />
              <span>
                <strong>This forecast is out of date.</strong> It covered {win}, and no newer forecast has been published since{' '}
                {agoPhrase(fc?.issued_at, now)}.
              </span>
            </p>
          ) : (
            <p className="fresh-note">
              <ClockIcon size={15} />
              <span>
                Updated {agoPhrase(fc?.issued_at, now)}
                {isReplay ? ' · reconstructed from past data (no live forecast yet)' : ''}
              </span>
            </p>
          )
        ) : (
          <p className="fresh-note">{forecast.error || index.error ? 'No forecast is available right now.' : 'Loading the latest forecast…'}</p>
        )}
      </div>

      <aside className="simple-panel" aria-label="Forecast for your area">
        <div className="choice-group">
          <h2 className="choice-legend">Check an area</h2>
          <PlaceSearch hoods={hoodNames} onHood={chooseHood} onPoint={choosePoint} />
        </div>

        <fieldset className="choice-group">
          <legend>What kind of crime?</legend>
          <div className="choice-pills three">
            {categories.map((c) => (
              <label key={c.id} className={`choice-pill${c.id === category ? ' is-on' : ''}`}>
                <input type="radio" name="category" value={c.id} checked={c.id === category} onChange={() => setCategory(c.id)} />
                {c.name}
              </label>
            ))}
          </div>
          <p className="choice-hint">{cat.examples}.</p>
        </fieldset>

        <fieldset className="choice-group">
          <legend>When?</legend>
          <div className="choice-pills">
            {HOURS.map((h) => (
              <label key={h.value} className={`choice-pill${h.value === hours ? ' is-on' : ''}`}>
                <input type="radio" name="hours" value={h.value} checked={h.value === hours} onChange={() => setHours(h.value)} />
                {h.label}
              </label>
            ))}
          </div>
        </fieldset>

        <section className="answer" aria-live="polite" aria-label="Forecast summary">
          {!hz ? (
            <p className="answer-loading">{forecast.loading || index.loading ? 'Loading…' : 'No forecast to show.'}</p>
          ) : place?.kind === 'outside' ? (
            <>
              <p className="answer-kicker">{place.label}</p>
              <h2 className="answer-headline">Outside the forecast area</h2>
              <p className="answer-text">Forecasts cover the City of Seattle only. Try a spot or neighborhood inside the city.</p>
              <button type="button" className="btn btn-ghost answer-back" onClick={() => setPlace(null)}>
                Back to all of Seattle
              </button>
            </>
          ) : place?.kind === 'cell' && cells.get(place.cell) ? (
            (() => {
              const c = cells.get(place.cell)!;
              const share = cellShare(place.cell);
              const cmp = compareToUsual(c.rr);
              const h = c.hood ? hoods.get(c.hood) : undefined;
              const hcmp = h ? compareToUsual(h.ratio) : null;
              const where = [place.label, c.hood].filter(Boolean).join(' · ');
              return (
                <>
                  <p className="answer-kicker">
                    {where || 'This spot'} · {win}
                  </p>
                  {c.exp >= 1.5 ? (
                    <>
                      <h2 className="answer-headline">{countPhrase(c.exp, cat.noun)} expected</h2>
                      <p className="answer-text">in this square (about 0.3 miles across).</p>
                    </>
                  ) : (
                    <>
                      <h2 className="answer-headline">{capitalize(chancePhrase(c.p))} chance</h2>
                      <p className="answer-text">
                        that a {cat.noun} is reported in this square (about 0.3 miles across) between {win.replace(' – ', ' and ')}.
                      </p>
                    </>
                  )}
                  <ul className="answer-facts">
                    {cmp ? (
                      <li>
                        <CompareChip c={cmp} /> <span>{usual}</span>
                      </li>
                    ) : null}
                    {share !== null ? (
                      <li>
                        <RankChip r={cityRank(share)} /> <span>compared with other squares</span>
                      </li>
                    ) : null}
                  </ul>
                  {h ? (
                    <div className="answer-hood">
                      <p className="answer-hood-title">All of {h.name}</p>
                      <p className="answer-hood-text">
                        {countPhrase(h.exp, cat.noun)} expected{hcmp ? ` · ${COMPARISON_TEXT[hcmp].toLowerCase()}` : ''}.
                      </p>
                      <button type="button" className="link-btn" onClick={() => chooseHood(h.name)}>
                        Show {h.name} on the map
                      </button>
                    </div>
                  ) : null}
                  <button type="button" className="btn btn-ghost answer-back" onClick={() => setPlace(null)}>
                    Back to all of Seattle
                  </button>
                </>
              );
            })()
          ) : place?.kind === 'hood' && hoods.get(place.hood) ? (
            (() => {
              const h = hoods.get(place.hood)!;
              const cmp = compareToUsual(h.ratio);
              const share = hoodRank.map.get(h.name);
              return (
                <>
                  <p className="answer-kicker">
                    {h.name} · {win}
                  </p>
                  <h2 className="answer-headline">{countPhrase(h.exp, cat.noun)} expected</h2>
                  <p className="answer-text">across the {h.ids.length} squares outlined on the map.</p>
                  <ul className="answer-facts">
                    {cmp && h.ratio !== null ? (
                      <li>
                        <CompareChip c={cmp} /> <span>{usual}</span>
                        <span className="answer-fine"> ({comparisonDetail(h.ratio)})</span>
                      </li>
                    ) : null}
                    {share !== undefined ? (
                      <li>
                        <RankChip r={cityRank(share)} /> <span>for its size, compared with {hoodRank.n - 1} other neighborhoods</span>
                      </li>
                    ) : null}
                    <li>
                      <span className="answer-fine">
                        Chance of at least one report in {h.name}: <strong>{chancePhrase(h.pAny)}</strong>
                      </span>
                    </li>
                  </ul>
                  <button type="button" className="btn btn-ghost answer-back" onClick={() => setPlace(null)}>
                    Back to all of Seattle
                  </button>
                </>
              );
            })()
          ) : (
            (() => {
              const cmp = compareToUsual(city.ratio);
              return (
                <>
                  <p className="answer-kicker">All of Seattle · {win}</p>
                  <h2 className="answer-headline">{countPhrase(city.exp, cat.noun)} expected</h2>
                  {cmp ? (
                    <ul className="answer-facts">
                      <li>
                        <CompareChip c={cmp} /> <span>{usual}</span>
                      </li>
                    </ul>
                  ) : null}
                  {hoodRank.top.length ? (
                    <div className="answer-top">
                      <p className="answer-top-title">Where the most reports are expected</p>
                      <ol className="answer-top-list">
                        {hoodRank.top.map((h) => (
                          <li key={h.name}>
                            <button type="button" onClick={() => chooseHood(h.name)}>
                              <span>{h.name}</span>
                              <span className="answer-top-n">{h.exp >= 0.5 ? reportsShort(h.exp) : `${chancePhrase(h.pAny)} chance`}</span>
                            </button>
                          </li>
                        ))}
                      </ol>
                      <p className="answer-fine">Busy places — downtown, shopping streets — see more reports mainly because more people and cars are there.</p>
                    </div>
                  ) : null}
                </>
              );
            })()
          )}
        </section>

        <section className="good-to-know" aria-labelledby="gtk-title">
          <h2 id="gtk-title">Good to know</h2>
          <ul>
            <li>
              This shows crimes <strong>reported to Seattle Police</strong>, not all crime. Many crimes are never reported.
            </li>
            <li>More reports usually means more people, shops and parked cars. It is not a measure of your personal safety.</li>
            <li>
              The forecast mostly follows each area’s usual pattern for the time of day and day of the week.
              {accuracy ? (
                <>
                  {' '}
                  In a test on past reports{accuracy.window ? ` (${fmtMonth(accuracy.window.start)} – ${fmtMonth(accuracy.window.end)})` : ''}, the 10% of
                  squares it ranked highest received {pct(accuracy.model)} of {cat.name.toLowerCase()} reports
                  {accuracy.usual !== null
                    ? Math.round(accuracy.usual * 100) === Math.round(accuracy.model * 100)
                      ? ' — about the same as using the usual pattern alone.'
                      : `, compared with ${pct(accuracy.usual)} using the usual pattern alone.`
                    : '.'}{' '}
                </>
              ) : null}
              <Link href="/evaluation/">How accurate is it?</Link>
            </li>
            <li>Police publish reports once a day, a few days late, so the last few days aren’t reflected yet.</li>
            <li>Nothing here is about individual people, and it shouldn’t be used to make decisions about anyone.</li>
          </ul>
          <p className="gtk-links">
            <Link href="/explore/" className="gtk-explore">
              Open the detailed research map <ArrowRight size={15} />
            </Link>
            <button type="button" className="link-btn" onClick={() => openAbout(true)}>
              About this project
            </button>
          </p>
        </section>

        <p className="simple-attrib">
          Data: Seattle Police Department (data.seattle.gov). Map ©{' '}
          <a href="https://openfreemap.org" target="_blank" rel="noreferrer">
            OpenFreeMap
          </a>{' '}
          ©{' '}
          <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">
            OpenStreetMap
          </a>
          .
        </p>
      </aside>

      <div className="simple-map">
        {ready && isMobile !== null ? (
          <MapView
            grid={grid.data!}
            bbox={meta!.grid.bbox}
            theme={theme}
            fills={fills}
            history={null}
            observed={null}
            describe={() => null}
            focus={null}
            padding={padding}
            compactControls={isMobile}
            dim={forecast.loading}
            onCellClick={onCellClick}
            selected={selectedIds}
            fitTo={fit}
            marker={marker}
            cooperative={isMobile}
          />
        ) : (
          <div className="map-canvas map-placeholder" />
        )}
        <div className="simple-legend" aria-hidden={!hz}>
          <span className="simple-legend-label">Fewer reports</span>
          <span className="simple-legend-ramp">
            {scheme.colors.map((c, i) => (
              <span key={c} style={{ background: c, opacity: CLASS_OPACITY.quantile[theme][i] }} />
            ))}
          </span>
          <span className="simple-legend-label">More</span>
        </div>
        {isDemo ? <div className="simple-demo">Sample data — not a real forecast</div> : null}
      </div>
    </div>
  );
}

const monthFmt = new Intl.DateTimeFormat('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });
function fmtMonth(iso: string): string {
  return monthFmt.format(new Date(iso));
}
