import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import '@fontsource-variable/inter';
import 'maplibre-gl/dist/maplibre-gl.css';
import './globals.css';
import { Providers } from '@/components/Providers';
import { AppHeader } from '@/components/AppHeader';
import { DemoBanner } from '@/components/DemoBanner';
import { AboutDrawer } from '@/components/AboutDrawer';

export const metadata: Metadata = {
  title: { default: 'Seattle Crime Forecast — research preview', template: '%s · Seattle Crime Forecast' },
  description:
    'Area-level (500 m) forecasts of publicly reported incidents in Seattle, with model evaluation and data provenance. A research and public-understanding tool — not for enforcement.',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#fcfcfb' },
    { media: '(prefers-color-scheme: dark)', color: '#141413' },
  ],
};

// Runs before first paint so the stored/OS theme applies without a flash.
const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem('scf-theme');if(t!=='light'&&t!=='dark'){t=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'}document.documentElement.setAttribute('data-theme',t)}catch(e){}})();`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <Providers>
          <a className="skip-link" href="#main">
            Skip to content
          </a>
          <div className="app">
            <AppHeader />
            <DemoBanner />
            <main id="main" className="app-main">
              {children}
            </main>
          </div>
          <AboutDrawer />
        </Providers>
      </body>
    </html>
  );
}
