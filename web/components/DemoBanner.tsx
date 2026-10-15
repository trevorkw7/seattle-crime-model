'use client';

import { useData } from './Providers';
import { FlaskIcon } from './Icons';

/** Prominent, non-dismissable notice whenever the dashboard shows synthetic data. */
export function DemoBanner() {
  const data = useData();
  if (data.status !== 'ready' || !data.demo) return null;
  return (
    <div className="demo-banner" role="status" data-testid="demo-banner">
      <FlaskIcon size={16} />
      <p>
        <strong>DEMO DATA — synthetic.</strong> Nothing on this site is a real Seattle forecast, incident count or evaluation result.
        {data.fallback ? (
          <span className="demo-banner-why">
            {' '}
            No published data was found at <code>{data.configuredBase}/</code>, so bundled fixtures are shown.
          </span>
        ) : null}
      </p>
    </div>
  );
}
