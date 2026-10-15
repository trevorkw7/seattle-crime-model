'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { CONFIGURED_DATA_BASE, DEMO_DATA_BASE } from '@/lib/config';
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
      /** true when the configured base had no meta.json and we fell back to /demo-data */
      fallback: boolean;
      /** true whenever the data must be presented as synthetic */
      demo: boolean;
      configuredBase: string;
    }
  | { status: 'error'; message: string };

const DataContext = createContext<DataState>({ status: 'loading' });

function looksLikeMeta(m: unknown): m is Meta {
  const x = m as Meta;
  return !!x && Array.isArray(x.categories) && Array.isArray(x.models) && Array.isArray(x.horizons_h);
}

function DataProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DataState>({ status: 'loading' });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const meta = await fetchJson<Meta>(`${CONFIGURED_DATA_BASE}/meta.json`, { noCache: true });
        if (!looksLikeMeta(meta)) throw new Error('meta.json does not match the contract');
        if (!alive) return;
        setState({
          status: 'ready',
          base: CONFIGURED_DATA_BASE,
          meta,
          fallback: false,
          demo: meta.provenance?.kind === 'demo' || CONFIGURED_DATA_BASE === DEMO_DATA_BASE,
          configuredBase: CONFIGURED_DATA_BASE,
        });
      } catch {
        try {
          const meta = await fetchJson<Meta>(`${DEMO_DATA_BASE}/meta.json`);
          if (!looksLikeMeta(meta)) throw new Error('demo meta.json is malformed');
          if (!alive) return;
          setState({ status: 'ready', base: DEMO_DATA_BASE, meta, fallback: true, demo: true, configuredBase: CONFIGURED_DATA_BASE });
        } catch (e) {
          if (alive) setState({ status: 'error', message: e instanceof Error ? e.message : String(e) });
        }
      }
    })();
    return () => {
      alive = false;
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
  const url = data.status === 'ready' && relPath ? `${data.base}/${relPath}` : null;
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
