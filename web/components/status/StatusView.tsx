'use client';

import { useData, useDoc } from '../Providers';
import { ProvenanceBadge, StatusPill } from '../ui';
import { SiteFooter } from '../SiteFooter';
import { AlertIcon, CheckIcon } from '../Icons';
import { fmtDateTime, fmtHours, fmtRelative } from '@/lib/format';
import { jobLabel, sourceLabel } from '@/lib/labels';
import type { HealthDoc } from '@/lib/types';

const TONE: Record<string, 'good' | 'warning' | 'critical'> = {
  ok: 'good',
  stale: 'warning',
  degraded: 'warning',
  missing: 'critical',
  error: 'critical',
};

function countBy<T>(items: T[], key: (t: T) => string) {
  const out: Record<string, number> = {};
  for (const i of items) out[key(i)] = (out[key(i)] ?? 0) + 1;
  return out;
}

export function StatusView() {
  const data = useData();
  const meta = data.status === 'ready' ? data.meta : null;
  const health = useDoc<HealthDoc>(meta ? 'health.json' : null);
  const h = health.data;
  const modelLabel = (id: string) => meta?.models.find((m) => m.id === id)?.label ?? id;

  const problems = h
    ? h.sources.filter((s) => s.status !== 'ok').length +
      h.models.filter((m) => m.status !== 'ok').length +
      h.jobs.filter((j) => j.last_status !== 'ok').length
    : 0;
  const maxLag = h ? Math.max(24, ...h.sources.map((s) => s.lag_hours ?? 0)) : 24;

  return (
    <>
      <div className="page">
        <div className="page-head">
          <div>
            <h1>Data &amp; system status</h1>
            <p>
              How fresh each data source is, whether the models are producing forecasts, and how the scheduled jobs last ran. SPD publishes
              finalised reports about once a day, so crime data always lags real time by a day or more.
            </p>
          </div>
          {h ? <ProvenanceBadge provenance={h.provenance} /> : null}
        </div>

        {data.status === 'error' ? <p className="empty">{data.message}</p> : null}
        {health.loading && !h ? <p className="empty">Loading status…</p> : null}
        {health.error ? <p className="empty">No health report has been published yet ({health.error.message}).</p> : null}

        {h ? (
          <>
            <div className={`status-headline tone-${problems ? 'warning' : 'good'}`} role="status">
              <span className="status-dot" aria-hidden="true">
                {problems ? <AlertIcon size={11} strokeWidth={3} /> : <CheckIcon size={11} strokeWidth={3} />}
              </span>
              {problems
                ? `${problems} item${problems === 1 ? ' needs' : 's need'} attention`
                : 'All sources, models and jobs are healthy'}
              <span className="small muted" style={{ fontWeight: 400, marginLeft: 'auto' }}>
                Report generated {fmtDateTime(h.provenance?.generated_at)} ({fmtRelative(h.provenance?.generated_at)})
              </span>
            </div>

            <div className="tiles">
              <SummaryTile label="Data sources" items={h.sources.map((s) => s.status)} />
              <SummaryTile label="Models" items={h.models.map((m) => m.status)} />
              <SummaryTile label="Scheduled jobs" items={h.jobs.map((j) => j.last_status)} />
            </div>

            <section className="eval-section" aria-labelledby="src-h">
              <div className="eval-section-head">
                <h2 id="src-h">Data freshness</h2>
                <span className="section-tag">Lag = time since the newest event in each source</span>
              </div>
              <div className="source-grid" style={{ marginTop: 14 }}>
                {h.sources.map((s) => {
                  const tone = TONE[s.status] ?? 'warning';
                  const pct = s.lag_hours == null ? 0 : Math.max(2, Math.min(100, (Math.log1p(s.lag_hours) / Math.log1p(maxLag)) * 100));
                  return (
                    <article className="source-card" key={s.name}>
                      <div className="source-card-head">
                        <div>
                          <div className="source-name">{sourceLabel(s.name)}</div>
                          <div className="source-id mono">{s.name}</div>
                        </div>
                        <StatusPill status={s.status} />
                      </div>
                      <div className="lag">
                        <div className="lag-text">
                          <span>
                            {s.lag_hours == null ? (
                              <strong>No data</strong>
                            ) : (
                              <>
                                <strong>{fmtHours(s.lag_hours)}</strong> behind
                              </>
                            )}
                          </span>
                          <span className="muted">log scale</span>
                        </div>
                        <div className="lag-bar" aria-hidden="true">
                          <div className={`lag-fill tone-${tone}`} style={{ width: `${pct}%` }} />
                        </div>
                      </div>
                      <dl className="kv" style={{ margin: 0 }}>
                        <dt>Newest event</dt>
                        <dd>{fmtDateTime(s.latest_event_ts)}</dd>
                        <dt>Publisher update</dt>
                        <dd>{fmtDateTime(s.publisher_updated_at)}</dd>
                        <dt>Fetched</dt>
                        <dd>
                          {fmtDateTime(s.fetched_at)}
                          {s.fetched_at ? <span className="muted"> · {fmtRelative(s.fetched_at)}</span> : null}
                        </dd>
                        <dt>Snapshot</dt>
                        <dd className="mono small">{s.snapshot_id ?? '—'}</dd>
                      </dl>
                    </article>
                  );
                })}
              </div>
            </section>

            <section className="eval-section" aria-labelledby="mod-h">
              <div className="eval-section-head">
                <h2 id="mod-h">Models</h2>
              </div>
              <div className="source-grid" style={{ marginTop: 14 }}>
                {h.models.map((m) => (
                  <article className="source-card" key={m.model_id}>
                    <div className="source-card-head">
                      <div>
                        <div className="source-name">{modelLabel(m.model_id)}</div>
                        <div className="source-id mono">{m.version ?? m.model_id}</div>
                      </div>
                      <StatusPill status={m.status} />
                    </div>
                    <dl className="kv" style={{ margin: 0 }}>
                      <dt>Last forecast</dt>
                      <dd>
                        {fmtDateTime(m.last_forecast_at)}
                        {m.last_forecast_at ? <span className="muted"> · {fmtRelative(m.last_forecast_at)}</span> : null}
                      </dd>
                      {m.notes ? (
                        <>
                          <dt>Notes</dt>
                          <dd>{m.notes}</dd>
                        </>
                      ) : null}
                    </dl>
                  </article>
                ))}
              </div>
            </section>

            <section className="eval-section" aria-labelledby="job-h">
              <div className="eval-section-head">
                <h2 id="job-h">Scheduled jobs</h2>
              </div>
              <div className="card" style={{ marginTop: 14, padding: '6px 8px' }}>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col">Job</th>
                        <th scope="col">Status</th>
                        <th scope="col">Last run</th>
                        <th scope="col">Message</th>
                      </tr>
                    </thead>
                    <tbody>
                      {h.jobs.map((j) => (
                        <tr key={j.name}>
                          <td>
                            <div style={{ fontWeight: 600 }}>{jobLabel(j.name)}</div>
                            <div className="mono small muted">{j.name}</div>
                          </td>
                          <td>
                            <StatusPill status={j.last_status} />
                          </td>
                          <td>
                            {fmtDateTime(j.last_run)}
                            <div className="small muted">{fmtRelative(j.last_run)}</div>
                          </td>
                          <td style={{ whiteSpace: 'normal', minWidth: 220 }}>{j.message ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </section>
          </>
        ) : null}
      </div>
      <SiteFooter />
    </>
  );
}

function SummaryTile({ label, items }: { label: string; items: string[] }) {
  const counts = countBy(items, (s) => s);
  const ok = counts.ok ?? 0;
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">
        {ok}
        <span className="muted" style={{ fontSize: 16, fontWeight: 500 }}>
          {' '}
          / {items.length} OK
        </span>
      </div>
      <div className="tile-breakdown">
        {Object.entries(counts)
          .filter(([k]) => k !== 'ok')
          .map(([k, n]) => (
            <span key={k} className="small">
              <StatusPill status={k} /> <span className="muted">× {n}</span>
            </span>
          ))}
      </div>
    </div>
  );
}
