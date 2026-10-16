'use client';

import { useId, useMemo, useState, type FormEvent, type KeyboardEvent } from 'react';
import { LocateIcon, SearchIcon } from '../Icons';

/** Seattle search box (west, north, east, south) for address lookups. */
const VIEWBOX = '-122.46,47.75,-122.22,47.48';

export interface PlaceSearchProps {
  /** neighborhood display names */
  hoods: string[];
  onHood: (name: string) => void;
  onPoint: (lngLat: [number, number], label: string) => void;
}

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

export function PlaceSearch({ hoods, onHood, onPoint }: PlaceSearchProps) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [status, setStatus] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const listId = useId();

  const matches = useMemo(() => {
    const n = norm(q);
    if (!n) return [];
    const starts = hoods.filter((h) => norm(h).startsWith(n));
    const contains = hoods.filter((h) => !starts.includes(h) && norm(h).includes(n));
    return [...starts, ...contains].slice(0, 6);
  }, [q, hoods]);

  const pick = (name: string) => {
    setQ(name);
    setOpen(false);
    setStatus(null);
    onHood(name);
  };

  const lookupAddress = async (text: string) => {
    setBusy(true);
    setStatus({ kind: 'info', text: 'Looking up that address…' });
    try {
      const url =
        'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&bounded=1' +
        `&viewbox=${VIEWBOX}&q=${encodeURIComponent(text)}`;
      const res = await fetch(url, { headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error(String(res.status));
      const hits = (await res.json()) as { lat: string; lon: string; display_name: string }[];
      if (!hits.length) {
        setStatus({ kind: 'error', text: 'We couldn’t find that in Seattle. Try a neighborhood name, a street address or an intersection.' });
        return;
      }
      setStatus(null);
      setOpen(false);
      const label = hits[0].display_name.split(',').slice(0, 2).join(',').trim();
      onPoint([Number(hits[0].lon), Number(hits[0].lat)], label);
    } catch {
      setStatus({ kind: 'error', text: 'Address search is unavailable right now. Try a neighborhood name instead.' });
    } finally {
      setBusy(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = q.trim();
    if (!text) return;
    const exact = hoods.find((h) => norm(h) === norm(text));
    if (exact) return pick(exact);
    if (open && matches.length) return pick(matches[Math.min(active, matches.length - 1)]);
    void lookupAddress(text);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!matches.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((a) => (a + 1) % matches.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => (a - 1 + matches.length) % matches.length);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  const locate = () => {
    if (!('geolocation' in navigator)) {
      setStatus({ kind: 'error', text: 'Your browser can’t share your location.' });
      return;
    }
    setBusy(true);
    setStatus({ kind: 'info', text: 'Finding your location…' });
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setBusy(false);
        setStatus(null);
        setQ('');
        onPoint([pos.coords.longitude, pos.coords.latitude], 'Your location');
      },
      (err) => {
        setBusy(false);
        setStatus({
          kind: 'error',
          text: err.code === err.PERMISSION_DENIED ? 'Location access was turned off. You can type a neighborhood instead.' : 'We couldn’t get your location.',
        });
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
    );
  };

  const showList = open && matches.length > 0;
  return (
    <div className="place-search">
      <form role="search" onSubmit={submit} className="place-form">
        <label htmlFor={`${listId}-input`} className="sr-only">
          Neighborhood or address
        </label>
        <div className="place-input-wrap">
          <SearchIcon size={18} className="place-input-icon" />
          <input
            id={`${listId}-input`}
            className="place-input"
            type="text"
            inputMode="search"
            autoComplete="off"
            placeholder="Neighborhood or address"
            value={q}
            role="combobox"
            aria-expanded={showList}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={showList ? `${listId}-${active}` : undefined}
            onChange={(e) => {
              setQ(e.target.value);
              setOpen(true);
              setActive(0);
              setStatus(null);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 150)}
            onKeyDown={onKey}
          />
        </div>
        <button type="submit" className="btn btn-primary place-go" disabled={busy || !q.trim()}>
          Go
        </button>
      </form>
      {showList ? (
        <ul id={listId} role="listbox" className="place-list">
          {matches.map((m, i) => (
            <li
              key={m}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? 'is-active' : ''}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(m);
              }}
            >
              {m}
            </li>
          ))}
        </ul>
      ) : null}
      <p className="place-alt">
        <button type="button" className="btn btn-ghost place-locate" onClick={locate} disabled={busy}>
          <LocateIcon size={17} /> Use my location
        </button>
        <span>or tap the map</span>
      </p>
      {status ? (
        <p className={`place-status${status.kind === 'error' ? ' is-error' : ''}`} role="status">
          {status.text}
        </p>
      ) : null}
      <p className="place-privacy">
        Your location stays on your device. Address search uses{' '}
        <a href="https://nominatim.org/" target="_blank" rel="noreferrer">
          OpenStreetMap
        </a>
        .
      </p>
    </div>
  );
}
