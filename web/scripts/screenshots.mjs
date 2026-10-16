#!/usr/bin/env node
/**
 * Capture screenshots of the built site (out/) with whatever data is in
 * public/data at build time:  node scripts/screenshots.mjs [outdir]
 *
 * The page clock is pinned to shortly after the export was generated so relative
 * times ("Updated 40 minutes ago") read as they did when the data was current.
 * The live feed is blocked so the bundled data is what gets captured.
 */
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { chromium } from '@playwright/test';

const outDir = process.argv[2] || 'screenshots/real';
mkdirSync(outDir, { recursive: true });
const meta = JSON.parse(readFileSync('public/data/meta.json', 'utf8'));
const clock = new Date(new Date(meta.provenance.generated_at).getTime() + 40 * 60 * 1000);
const port = 4199;
const server = spawn('node', ['scripts/serve-out.mjs', '--port', String(port)], { stdio: 'ignore' });
await new Promise((r) => setTimeout(r, 1200));
const browser = await chromium.launch();
const errors = [];
const routes = [
  ['/', 'home'],
  ['/explore/', 'map'],
  ['/evaluation/', 'evaluation'],
  ['/status/', 'status'],
];
try {
  for (const [name, vp] of [['desktop', { width: 1440, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport: vp });
    await page.clock.install({ time: clock });
    await page.route(/github\.io/, (r) => r.abort());
    page.on('console', (m) => m.type() === 'error' && !m.text().includes('ERR_FAILED') && errors.push(`${name}: ${m.text()}`));
    for (const [route, label] of routes) {
      await page.goto(`http://localhost:${port}${route}`, { waitUntil: 'networkidle' });
      await page.waitForTimeout(2500);
      const file = path.join(outDir, `${label}-${name}.png`);
      await page.screenshot({ path: file, fullPage: !['/', '/explore/'].includes(route) || (route === '/' && name === 'mobile') });
      console.log('saved', file);
      if (route === '/') {
        await page.getByRole('combobox', { name: 'Neighborhood or address' }).fill('capitol');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(1800);
        const area = path.join(outDir, `home-area-${name}.png`);
        await page.screenshot({ path: area, fullPage: name === 'mobile' });
        console.log('saved', area);
      }
    }
    await page.close();
  }
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) console.log('console errors:\n' + errors.join('\n'));
