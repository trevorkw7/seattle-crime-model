'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { CONFIGURED_DATA_BASE, DEMO_DATA_BASE, LIVE_DATA_BASE, REFRESH_MS } from '@/lib/config';
import { fetchJson } from '@/lib/fetch';
import type { Theme } from '@/lib/colors';
import type { Meta } from '@/lib/types';

// ---------------------------------------------------------------------------
// Data source resolution: real export first, synthetic demo fixtures as fallback
// ---------------------------------------------------------------------------

export type DataState =
  | { status: 'loading' }
  | {
      status: 'ready';
      base: string;
      meta: Meta;
      /** true when no published data was found and we fell back to /demo-data */
      fallback: boolean;
      /** true whenever the data must be presented as synthetic */
      demo: boolean;
      configuredBase: string;
      /** where the data came from: the hourly live feed, the snapshot bundled with the site, or demo fixtures */
      source: 'live' | 'bundled' | 'demo';
      /** changes whenever newer data is published; appended to document URLs to bypass caches */
      version: string;
    }
  | { status: 'error'; message: string };

const DataContext = createContext<DataState>({ status: 'loading' });

function looksLikeMeta(m: unknown): m is Meta {
  const x = m as Meta;
  return !!x && Array.isArray(x.categories) && Array.isArray(x.models) && Array.isArray(x.horizons_h);
}

async function tryMeta(base: string, bust: string): Promise<Meta | null> {
  try {
    const m = await fetchJson<Meta>(`${base}/meta.json${bust}`, { noCache: true });
    return looksLikeMeta(m) ? m : null;
  } catch {
    return null;
  }
}

const generatedAt = (m: Meta) => Date.parse(m.provenance?.generated_at ?? '') || 0;

/** Prefer the live feed when it is at least as new as the bundled snapshot; demo fixtures last. */
async function resolveData(): Promise<DataState> {
  // bucket the cache-buster so CDNs still absorb most requests
  const bust = `?t=${Math.floor(Date.now() / 120000)}`;
  const [live, bundled] = await Promise.all([
    LIVE_DATA_BASE && LIVE_DATA_BASE !== CONFIGURED_DATA_BASE ? tryMeta(LIVE_DATA_BASE, bust) : Promise.resolve(null),
    tryMeta(CONFIGURED_DATA_BASE, bust),
  ]);
  const ready = (base: string, meta: Meta, source: 'live' | 'bundled'): DataState => ({
    status: 'ready',
    base,
    meta,
    fallback: false,
    demo: meta.provenance?.kind === 'demo' || base === DEMO_DATA_BASE,
    configuredBase: CONFIGURED_DATA_BASE,
    source: meta.provenance?.kind === 'demo' ? 'demo' : source,
    version: meta.provenance?.generated_at ?? '',
  });
  if (live && live.provenance?.kind !== 'demo' && (!bundled || generatedAt(live) >= generatedAt(bundled))) return ready(LIVE_DATA_BASE, live, 'live');
  if (bundled) return ready(CONFIGURED_DATA_BASE, bundled, 'bundled');
  const meta = await fetchJson<Meta>(`${DEMO_DATA_BASE}/meta.json`);
  if (!looksLikeMeta(meta)) throw new Error('demo meta.json is malformed');
  return {
    status: 'ready',
    base: DEMO_DATA_BASE,
    meta,
    fallback: true,
    demo: true,
    configuredBase: CONFIGURED_DATA_BASE,
    source: 'demo',
    version: meta.provenance?.generated_at ?? '',
  };
}

function DataProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DataState>({ status: 'loading' });

  useEffect(() => {
    let alive = true;
    const load = async (initial: boolean) => {
      try {
        const next = await resolveData();
        if (!alive) return;
        setState((cur) =>
          // keep the current object (and every view's documents) unless something new was published
          !initial && cur.status === 'ready' && next.status === 'ready' && cur.base === next.base && cur.version === next.version ? cur : next,
        );
      } catch (e) {
        if (alive && initial) setState({ status: 'error', message: e instanceof Error ? e.message : String(e) });
      }
    };
    void load(true);
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void load(false);
    }, REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load(false);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive = false;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);

  return <DataContext.Provider value={state}>{children}</DataContext.Provider>;
}

export const useData = () => useContext(DataContext);

/**
 * Fetch a document relative to the resolved data base. While a new path loads, the
 * previous document stays available (`stale`) so views can keep their frame.
 */
export function useDoc<T>(relPath: string | null) {
  const data = useData();
  const url =
    data.status === 'ready' && relPath ? `${data.base}/${relPath}${data.version ? `?v=${encodeURIComponent(data.version)}` : ''}` : null;
  const [state, setState] = useState<{ key: string | null; data: T | null; error: Error | null }>({
    key: null,
    data: null,
    error: null,
  });

  useEffect(() => {
    if (!url) return;
    let alive = true;
    fetchJson<T>(url).then(
      (d) => alive && setState({ key: url, data: d, error: null }),
      // never keep showing a different document after a failed load
      (e: Error) => alive && setState({ key: url, data: null, error: e }),
    );
    return () => {
      alive = false;
    };
  }, [url]);

  const loading = !!url && state.key !== url;
  return {
    data: url ? state.data : null,
    error: url && state.key === url ? state.error : null,
    loading,
    stale: loading && state.data !== null,
    ready: !!url && state.key === url && !state.error,
  };
}

// ---------------------------------------------------------------------------
// Theme (light default, dark optional; follows the OS until the user picks)
// ---------------------------------------------------------------------------

const ThemeContext = createContext<{ theme: Theme; toggle: () => void }>({ theme: 'light', toggle: () => {} });

function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>('light');

  useEffect(() => {
    const el = document.documentElement;
    const current = el.dataset.theme === 'dark' ? 'dark' : 'light';
    setTheme(current);
    let stored: string | null = null;
    try {
      stored = localStorage.getItem('scf-theme');
    } catch {
      /* storage unavailable */
    }
    if (stored === 'light' || stored === 'dark') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => {
      const t: Theme = mq.matches ? 'dark' : 'light';
      el.dataset.theme = t;
      setTheme(t);
    };
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const toggle = useCallback(() => {
    setTheme((t) => {
      const next: Theme = t === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem('scf-theme', next);
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  const value = useMemo(() => ({ theme, toggle }), [theme, toggle]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export const useTheme = () => useContext(ThemeContext);

// ---------------------------------------------------------------------------
// "About this map" drawer state
// ---------------------------------------------------------------------------

const AboutContext = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} });

function AboutProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const value = useMemo(() => ({ open, setOpen }), [open]);
  return <AboutContext.Provider value={value}>{children}</AboutContext.Provider>;
}

export const useAbout = () => useContext(AboutContext);

export function Providers({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <DataProvider>
        <AboutProvider>{children}</AboutProvider>
      </DataProvider>
    </ThemeProvider>
  );
}
