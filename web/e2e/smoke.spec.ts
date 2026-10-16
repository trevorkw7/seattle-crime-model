import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type ConsoleMessage, type Page } from '@playwright/test';

/**
 * Smoke test: every page renders against the bundled demo data (or real data in
 * public/data if present) at desktop and phone sizes without console errors, and
 * screenshots land in web/screenshots/.
 *
 * Tolerated console noise (reported, not failed):
 *  - the 404 for `<data base>/meta.json` that triggers the demo-data fallback;
 *  - basemap tile/style requests to tiles.openfreemap.org when the host is unreachable
 *    from the test browser (the forecast grid does not depend on them).
 */

const SHOTS = path.resolve(__dirname, '..', 'screenshots');
mkdirSync(SHOTS, { recursive: true });

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 390, height: 844 },
} as const;

const TOLERATED = [/\/data\/meta\.json/, /tiles\.openfreemap\.org/, /openfreemap/, /github\.io/];

// Tests run against the data bundled in the build: the hourly live feed (GitHub Pages) is blocked
// so results do not depend on what has been published.
test.beforeEach(async ({ page }) => {
  await page.route(/github\.io/, (route) => route.abort());
});

function watchConsole(page: Page) {
  const errors: string[] = [];
  const tolerated: string[] = [];
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() !== 'error') return;
    const where = msg.location()?.url ?? '';
    const text = `${msg.text()} ${where}`.trim();
    if (TOLERATED.some((re) => re.test(text))) tolerated.push(text);
    else errors.push(text);
  });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  return { errors, tolerated };
}

async function waitForMap(page: Page) {
  await expect(page.locator('[data-grid-ready="true"]')).toBeVisible({ timeout: 45_000 });
  // let tiles (if reachable) and the first frame of the grid settle
  await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
  await page.waitForTimeout(1200);
}

for (const [name, viewport] of Object.entries(VIEWPORTS)) {
  test.describe(`${name} ${viewport.width}x${viewport.height}`, () => {
    test.use({
      viewport,
      isMobile: name === 'mobile',
      hasTouch: name === 'mobile',
      colorScheme: 'light',
    });

    test('home page (plain-language view)', async ({ page }) => {
      const log = watchConsole(page);
      await page.goto('/');
      await waitForMap(page);
      await expect(page.getByRole('heading', { name: 'Where crime reports are expected in Seattle' })).toBeVisible();
      const answer = page.getByRole('region', { name: 'Forecast summary' });
      await expect(answer).toContainText('All of Seattle');
      await expect(answer).toContainText(/expected/);
      await page.screenshot({ path: path.join(SHOTS, `home-${name}.png`) });

      // neighborhood search
      await page.getByRole('combobox', { name: 'Neighborhood or address' }).fill('capitol');
      await expect(page.getByRole('option', { name: 'Capitol Hill' })).toBeVisible();
      await page.keyboard.press('Enter');
      await expect(answer).toContainText('Capitol Hill');
      await expect(page).toHaveURL(/area=Capitol(%20|\+)Hill/);

      // crime type and time window
      await page.getByText('Violent crime', { exact: true }).click();
      await page.getByText('Next 24 hours', { exact: true }).click();
      await expect(page).toHaveURL(/cat=violent/);
      await expect(page).toHaveURL(/h=24/);
      await expect(answer).toContainText('violent crime');
      await page.screenshot({ path: path.join(SHOTS, `home-${name}-area.png`), fullPage: name === 'mobile' });

      // tapping a square shows that square
      await page.getByTestId('map').scrollIntoViewIfNeeded();
      const box = (await page.getByTestId('map').boundingBox())!;
      if (name === 'mobile') await page.touchscreen.tap(box.x + box.width * 0.5, box.y + box.height * 0.5);
      else await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.5);
      await expect(answer).toContainText(/this square|Outside the forecast area/);
      await answer.getByRole('button', { name: 'Back to all of Seattle' }).click();
      await expect(answer).toContainText('All of Seattle');

      expect(log.errors, `console errors:\n${log.errors.join('\n')}`).toEqual([]);
    });

    test('map page', async ({ page }) => {
      const log = watchConsole(page);
      await page.goto('/explore/');
      await waitForMap(page);
      await expect(page.getByRole('complementary', { name: 'Map controls' })).toBeVisible();
      if (await page.getByTestId('demo-banner').count()) {
        await expect(page.getByTestId('demo-banner')).toContainText('DEMO DATA');
      }
      await page.screenshot({ path: path.join(SHOTS, `map-${name}.png`) });

      if (name === 'desktop') {
        // hover a cell near the city centre to open the popup
        const box = (await page.getByTestId('map').boundingBox())!;
        await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5);
        await page.waitForTimeout(400);
        await page.mouse.move(box.x + box.width * 0.6 + 4, box.y + box.height * 0.5 + 2);
        await expect(page.locator('.cell-popup')).toBeVisible({ timeout: 5_000 });
        await page.screenshot({ path: path.join(SHOTS, `map-${name}-popup.png`) });
      } else {
        await page.getByRole('button', { name: /More/ }).click();
        await page.waitForTimeout(300);
        await page.screenshot({ path: path.join(SHOTS, `map-${name}-controls.png`) });
        await page.getByRole('button', { name: /Less/ }).click();
        const box = (await page.getByTestId('map').boundingBox())!;
        await page.touchscreen.tap(box.x + box.width * 0.55, box.y + box.height * 0.3);
        await expect(page.locator('.cell-popup')).toBeVisible({ timeout: 5_000 });
        await page.screenshot({ path: path.join(SHOTS, `map-${name}-popup.png`) });
      }

      // About drawer
      await page.getByRole('button', { name: 'About this project' }).click();
      await expect(page.getByRole('dialog', { name: 'About this map' })).toBeVisible();
      await page.waitForTimeout(400); // let the slide-in animation finish
      await page.screenshot({ path: path.join(SHOTS, `about-${name}.png`) });
      await page.keyboard.press('Escape');

      expect(log.errors, `console errors:\n${log.errors.join('\n')}`).toEqual([]);
      if (log.tolerated.length) console.log(`[${name}] tolerated console messages:\n  ${log.tolerated.join('\n  ')}`);
    });

    test('map page — scored past forecast with overlays', async ({ page }) => {
      const log = watchConsole(page);
      await page.goto('/explore/');
      await waitForMap(page);
      // pick the oldest scored run for the default model/category via the index
      const base = await page.evaluate(() => (document.querySelector('[data-testid="demo-banner"]') ? '/demo-data' : '/data'));
      const index = await (await page.request.get(`${base}/forecasts/index.json`)).json();
      const run = index.items.find((i: { status: string; model_id: string; category: string }) =>
        i.status.startsWith('scored') && i.category === 'property',
      );
      test.skip(!run, 'no scored forecast published');
      await page.goto(`/explore/?cat=property&model=${run.model_id}&h=24&metric=expected&fc=${run.forecast_id}&hist=1&period=last_30d`);
      await waitForMap(page);
      if (name === 'mobile') await page.getByRole('button', { name: /More/ }).click();
      await expect(page.getByText('Forecast vs. reported')).toBeVisible();
      await page.screenshot({ path: path.join(SHOTS, `map-${name}-replay.png`) });
      expect(log.errors, `console errors:\n${log.errors.join('\n')}`).toEqual([]);
    });

    test('evaluation page', async ({ page }) => {
      const log = watchConsole(page);
      await page.goto('/evaluation/');
      await expect(page.getByRole('heading', { name: 'Historical backtest' })).toBeVisible({ timeout: 30_000 });
      await expect(page.getByRole('heading', { name: 'Prospective' })).toBeVisible();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(SHOTS, `evaluation-${name}.png`), fullPage: true });
      expect(log.errors, `console errors:\n${log.errors.join('\n')}`).toEqual([]);
    });

    test('status page', async ({ page }) => {
      const log = watchConsole(page);
      await page.goto('/status/');
      await expect(page.getByRole('heading', { name: 'Data freshness' })).toBeVisible({ timeout: 30_000 });
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, `status-${name}.png`), fullPage: true });
      expect(log.errors, `console errors:\n${log.errors.join('\n')}`).toEqual([]);
    });
  });
}

test.describe('contract variants', () => {
  test.use({ viewport: VIEWPORTS.desktop });

  test('replay provenance and awaiting_data are labelled', async ({ page }) => {
    const log = watchConsole(page);
    // Re-label demo documents as a backend would for historical replays.
    await page.route('**/forecasts/index.json', async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      for (const it of body.items) if (it.status === 'scored_mature' || it.status === 'scored_provisional') it.provenance_kind = 'replay';
      await route.fulfill({ response: res, json: body });
    });
    await page.route('**/forecasts/fc_*.json', async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      if (body.score) body.provenance = { ...body.provenance, kind: 'replay' };
      await route.fulfill({ response: res, json: body });
    });
    await page.goto('/explore/');
    await waitForMap(page);
    const base = await page.evaluate(() => (document.querySelector('[data-testid="demo-banner"]') ? '/demo-data' : '/data'));
    const index = await (await page.request.get(`${base}/forecasts/index.json`)).json();
    const run = index.items.find((i: { status: string }) => i.status === 'scored_provisional');
    test.skip(!run, 'no scored forecast published');
    await page.goto(`/explore/?cat=${run.category}&model=${run.model_id}&h=24&fc=${run.forecast_id}`);
    await waitForMap(page);
    await expect(page.locator('.chip-replay').first()).toBeVisible();
    await expect(page.locator('.tick.is-replay').first()).toBeAttached();
    await page.screenshot({ path: path.join(SHOTS, 'variant-replay-desktop.png') });
    expect(log.errors).toEqual([]);
  });

  test('evaluation tolerates backend variants (event_time, nested stability, baselines)', async ({ page }) => {
    const log = watchConsole(page);
    await page.route('**/evaluation/summary.json', async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      type Row = Record<string, unknown>;
      const rows: Row[] = body.backtest.rows;
      // the same rows for two event-time definitions, plus a backtest-only baseline model
      body.backtest.rows = [
        ...rows.map((r) => ({ ...r, event_time: 'report' })),
        ...rows.map((r) => ({ ...r, event_time: 'offense', capture_10: (r.capture_10 as number) * 0.9 })),
        ...rows
          .filter((r) => r.model_id === 'hotspot_kde')
          .map((r) => ({ ...r, model_id: 'seasonal', event_time: 'report', capture_10: (r.capture_10 as number) * 0.95 })),
      ];
      // nested stability rows: {model_id, category, horizon_h, by_precinct: [...]}
      const nest = (list: Row[], key: string) => {
        const groups = new Map<string, Row>();
        for (const r of list) {
          const k = `${r.model_id}|${r.category}|${r.horizon_h}`;
          if (!groups.has(k)) groups.set(k, { model_id: r.model_id, category: r.category, horizon_h: r.horizon_h, event_time: 'report', [key]: [] });
          (groups.get(k)![key] as Row[]).push(r);
        }
        return [...groups.values()];
      };
      body.stability = { by_precinct: nest(body.stability.by_precinct, 'by_precinct'), by_month: nest(body.stability.by_month, 'by_month') };
      await route.fulfill({ response: res, json: body });
    });
    await page.goto('/evaluation/');
    await expect(page.getByRole('heading', { name: 'Historical backtest' })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('radio', { name: 'Offense time' })).toBeAttached();
    await expect(page.getByText('Seasonal baseline').first()).toBeVisible();
    await expect(page.getByText('North', { exact: true }).first()).toBeVisible();
    await page.screenshot({ path: path.join(SHOTS, 'variant-backend-evaluation-desktop.png'), fullPage: true });
    expect(log.errors).toEqual([]);
  });

  test('evaluation handles zero live scores', async ({ page }) => {
    const log = watchConsole(page);
    await page.route('**/evaluation/summary.json', async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      body.prospective.rows = body.prospective.rows.filter((r: { source?: string }) => r.source === 'replay');
      body.prospective.timeseries = body.prospective.timeseries.filter((r: { source?: string }) => r.source === 'replay');
      await route.fulfill({ response: res, json: body });
    });
    await page.goto('/evaluation/');
    await expect(page.getByText('No live scores yet.')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/scores appear after SPD publishes/).first()).toBeVisible();
    expect(log.errors).toEqual([]);
  });
});
