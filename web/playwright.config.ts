import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { defineConfig } from '@playwright/test';

/**
 * Smoke test against the static export in out/ (run `npm run build` first).
 * @playwright/test is pinned to the version whose Chromium build is pre-installed
 * (PLAYWRIGHT_BROWSERS_PATH). If that ever drifts, set PW_CHROMIUM_EXECUTABLE or we
 * fall back to the first chrome binary found under PLAYWRIGHT_BROWSERS_PATH.
 */
function chromiumExecutable(): string | undefined {
  if (process.env.PW_CHROMIUM_EXECUTABLE) return process.env.PW_CHROMIUM_EXECUTABLE;
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH;
  if (!root || !existsSync(root)) return undefined;
  const expected = 'chromium-1194'; // Chromium build bundled with Playwright 1.56.x
  if (existsSync(path.join(root, expected))) return undefined; // version matches; let Playwright resolve it
  const dir = readdirSync(root).find((d) => /^chromium-\d+$/.test(d));
  const bin = dir ? path.join(root, dir, 'chrome-linux', 'chrome') : undefined;
  return bin && existsSync(bin) ? bin : undefined;
}

const PORT = Number(process.env.E2E_PORT || 4173);
const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: `http://localhost:${PORT}`,
    browserName: 'chromium',
    launchOptions: {
      executablePath: chromiumExecutable(),
      args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
    },
    // In sandboxed CI the only route to the basemap tile host may be an HTTPS proxy.
    ...(proxy ? { proxy: { server: proxy, bypass: 'localhost,127.0.0.1' } } : {}),
  },
  webServer: {
    command: `node scripts/serve-out.mjs --port ${PORT}`,
    url: `http://localhost:${PORT}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
