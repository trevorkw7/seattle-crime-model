import type { NextConfig } from 'next';

/**
 * Static export: `npm run build` writes a self-contained site to `out/` that can be
 * served by any static host. Set NEXT_PUBLIC_BASE_PATH when hosting under a sub-path
 * (e.g. GitHub Pages project sites: NEXT_PUBLIC_BASE_PATH=/seattle-crime-model).
 */
const basePath = (process.env.NEXT_PUBLIC_BASE_PATH ?? '').replace(/\/$/, '');

const nextConfig: NextConfig = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  ...(basePath ? { basePath } : {}),
};

export default nextConfig;
