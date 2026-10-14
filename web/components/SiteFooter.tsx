'use client';

import { useAbout } from './Providers';

/** Short ethics line used at the foot of every view. */
export function DisclaimerLine({ className = '' }: { className?: string }) {
  const { setOpen } = useAbout();
  return (
    <p className={`disclaimer-line ${className}`}>
      Area-level (500 m) forecasts of <em>reported</em> incidents for research and public understanding — not predictions about
      people and not for enforcement. Reports reflect reporting and policing patterns.{' '}
      <button type="button" className="link-btn" onClick={() => setOpen(true)}>
        About this map
      </button>
    </p>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <DisclaimerLine />
      <p className="footer-meta">
        Basemap © <a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a>,{' '}
        <a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a>, data ©{' '}
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>. Incident data
        source: Seattle Police Department via data.seattle.gov.
      </p>
    </footer>
  );
}
