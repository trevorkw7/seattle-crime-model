'use client';

import type { ReactNode } from 'react';
import { fmtDateTime } from '@/lib/format';
import type { Provenance } from '@/lib/types';
import { AlertIcon, CheckIcon, ClockIcon, HistoryIcon, XCircleIcon } from './Icons';

/** Badge/chip for a document's provenance.kind — demo data is never shown unmarked. */
export function ProvenanceBadge({ provenance, compact = false }: { provenance?: Provenance | null; compact?: boolean }) {
  if (!provenance) return null;
  const title = [provenance.note, provenance.generated_at ? `Generated ${fmtDateTime(provenance.generated_at)}` : null]
    .filter(Boolean)
    .join(' — ');
  if (provenance.kind === 'demo') {
    return (
      <span className="badge-demo" title={title}>
        DEMO{compact ? '' : ' · synthetic'}
      </span>
    );
  }
  if (provenance.kind === 'replay') {
    return (
      <span className="chip chip-replay" title={title || 'Reconstructed for a past issue time using only data available then. Not a live forecast.'}>
        <HistoryIcon size={13} />
        {compact ? 'Replay' : 'Historical replay (simulated as-of)'}
      </span>
    );
  }
  if (provenance.kind === 'provisional') {
    return (
      <span className="chip chip-provisional" title={title}>
        <ClockIcon size={13} />
        {compact ? 'Provisional' : 'Provisional — reports still arriving'}
      </span>
    );
  }
  return (
    <span className="chip chip-actual" title={title}>
      Actual
    </span>
  );
}

export function MaturityChip({ maturity }: { maturity: 'provisional' | 'mature' | string | undefined }) {
  if (maturity === 'mature') {
    return (
      <span className="chip chip-mature" title="All late reports for this window have had time to arrive">
        <CheckIcon size={13} />
        Mature
      </span>
    );
  }
  return (
    <span className="chip chip-provisional" title="Late reports may still arrive; scores can change">
      <ClockIcon size={13} />
      Provisional
    </span>
  );
}

type Tone = 'good' | 'warning' | 'critical' | 'neutral';

const STATUS_TONE: Record<string, Tone> = {
  ok: 'good',
  stale: 'warning',
  degraded: 'warning',
  missing: 'critical',
  error: 'critical',
};

const STATUS_LABEL: Record<string, string> = {
  ok: 'OK',
  stale: 'Stale',
  degraded: 'Degraded',
  missing: 'Missing',
  error: 'Error',
};

export function StatusPill({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? 'neutral';
  const Icon = tone === 'good' ? CheckIcon : tone === 'warning' ? AlertIcon : tone === 'critical' ? XCircleIcon : ClockIcon;
  return (
    <span className={`status-pill tone-${tone}`}>
      <span className="status-dot" aria-hidden="true">
        <Icon size={11} strokeWidth={3} />
      </span>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export interface SegOption<V extends string> {
  value: V;
  label: ReactNode;
  disabled?: boolean;
  title?: string;
}

export function Segmented<V extends string>({
  name,
  label,
  options,
  value,
  onChange,
  hideLabel = false,
  size = 'md',
}: {
  name: string;
  label: string;
  options: SegOption<V>[];
  value: V;
  onChange: (v: V) => void;
  hideLabel?: boolean;
  size?: 'sm' | 'md';
}) {
  return (
    <fieldset className={`field seg-field ${size === 'sm' ? 'seg-sm' : ''}`}>
      <legend className={hideLabel ? 'sr-only' : 'field-label'}>{label}</legend>
      <div className="seg" role="presentation">
        {options.map((o) => (
          <label key={o.value} className="seg-opt" title={o.title}>
            <input
              type="radio"
              name={name}
              value={o.value}
              checked={value === o.value}
              disabled={o.disabled}
              onChange={() => onChange(o.value)}
            />
            <span>{o.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function SelectField({
  id,
  label,
  value,
  onChange,
  options,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <select id={id} className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  description,
  id,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  description?: ReactNode;
  id: string;
}) {
  return (
    <label className="switch-row" htmlFor={id}>
      <span className="switch-text">
        <span className="switch-label">{label}</span>
        {description ? <span className="switch-desc">{description}</span> : null}
      </span>
      <input id={id} type="checkbox" role="switch" className="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}
