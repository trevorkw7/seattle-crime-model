'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAbout, useTheme } from './Providers';
import { BrandMark, InfoIcon, MoonIcon, SunIcon } from './Icons';

const NAV = [
  { href: '/', label: 'Map' },
  { href: '/evaluation/', label: 'Evaluation' },
  { href: '/status/', label: 'Status' },
];

export function AppHeader() {
  const pathname = usePathname() || '/';
  const { theme, toggle } = useTheme();
  const { setOpen } = useAbout();
  const norm = (p: string) => (p.endsWith('/') ? p : `${p}/`);

  return (
    <header className="app-header">
      <Link href="/" className="brand" aria-label="Seattle reported-crime forecast — map">
        <BrandMark />
        <span className="brand-text">
          <span className="brand-title">Seattle Crime Forecast</span>
          <span className="brand-sub">Research preview · reported incidents</span>
        </span>
      </Link>
      <nav className="nav" aria-label="Main">
        {NAV.map((n) => {
          const active = n.href === '/' ? norm(pathname) === '/' : norm(pathname).startsWith(n.href);
          return (
            <Link key={n.href} href={n.href} aria-current={active ? 'page' : undefined}>
              {n.label}
            </Link>
          );
        })}
      </nav>
      <div className="header-actions">
        <button
          type="button"
          className="btn btn-ghost about-btn"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          aria-label="About this map"
        >
          <InfoIcon size={16} />
          <span className="about-btn-text">About this map</span>
        </button>
        <button
          type="button"
          className="icon-btn"
          onClick={toggle}
          aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          title={theme === 'dark' ? 'Light theme' : 'Dark theme'}
        >
          {theme === 'dark' ? <SunIcon size={17} /> : <MoonIcon size={17} />}
        </button>
      </div>
    </header>
  );
}
