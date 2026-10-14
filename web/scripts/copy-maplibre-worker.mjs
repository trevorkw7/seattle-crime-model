#!/usr/bin/env node
/**
 * MapLibre GL JS v6 loads its web worker from a separate module file. Bundlers rename
 * chunks, so we ship the worker as a plain static asset and point MapLibre at it with
 * setWorkerUrl() (see components/map/MapView.tsx). The `.js` extension keeps static
 * hosts serving it with a JavaScript MIME type.
 */
import { copyFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const pkgDir = path.dirname(require.resolve('maplibre-gl/package.json'));
const src = path.join(pkgDir, 'dist', 'maplibre-gl-worker.mjs');
const destDir = path.resolve(here, '../public/vendor');
mkdirSync(destDir, { recursive: true });
copyFileSync(src, path.join(destDir, 'maplibre-gl-worker.js'));
console.log('copied maplibre-gl worker -> public/vendor/maplibre-gl-worker.js');
