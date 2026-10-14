#!/usr/bin/env node
/**
 * Capture screenshots of the built site (out/) with whatever data is in
 * public/data at build time:  node scripts/screenshots.mjs [outdir]
 */
import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { chromium } from '@playwright/test';

const outDir = process.argv[2] || 'screenshots/real';
mkdirSync(outDir, { recursive: true });
const port = 4199;
const server = spawn('node', ['scripts/serve-out.mjs', '--port', String(port)], { stdio: 'ignore' });
await new Promise((r) => setTimeout(r, 1200));
const browser = await chromium.launch();
const errors = [];
try {
  for (const [name, vp] of [['desktop', { width: 1440, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport: vp });
    page.on('console', (m) => m.type() === 'error' && errors.push(`${name}: ${m.text()}`));
    for (const route of ['/', '/evaluation/', '/status/']) {
      await page.goto(`http://localhost:${port}${route}`, { waitUntil: 'networkidle' });
      await page.waitForTimeout(2500);
      const file = path.join(outDir, `${route === '/' ? 'map' : route.replaceAll('/', '')}-${name}.png`);
      await page.screenshot({ path: file, fullPage: route !== '/' });
      console.log('saved', file);
    }
    await page.close();
  }
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) console.log('console errors:\n' + errors.join('\n'));
