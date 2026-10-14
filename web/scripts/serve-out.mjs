#!/usr/bin/env node
/**
 * Minimal static file server for the exported site in out/ (used by `npm start`
 * and the Playwright smoke test). Mirrors what a static host does with
 * trailingSlash: true — /evaluation → /evaluation/ → evaluation/index.html.
 *
 *   node scripts/serve-out.mjs [--port 4173] [--dir out]
 */
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : def;
};
const port = Number(arg('port', process.env.PORT || 4173));
const root = path.resolve(here, '..', arg('dir', 'out'));
const basePath = (process.env.NEXT_PUBLIC_BASE_PATH || '').replace(/\/$/, '');

if (!existsSync(root)) {
  console.error(`[serve-out] ${root} does not exist — run \`npm run build\` first.`);
  process.exit(1);
}

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.geojson': 'application/geo+json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
};

function send(res, status, file) {
  res.writeHead(status, {
    'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream',
    'Cache-Control': 'no-cache',
  });
  createReadStream(file).pipe(res);
}

createServer((req, res) => {
  let urlPath = decodeURIComponent(new URL(req.url || '/', 'http://x').pathname);
  if (basePath && urlPath.startsWith(basePath)) urlPath = urlPath.slice(basePath.length) || '/';
  const target = path.normalize(path.join(root, urlPath));
  if (!target.startsWith(root)) {
    res.writeHead(403).end();
    return;
  }
  if (existsSync(target) && statSync(target).isDirectory()) {
    if (!urlPath.endsWith('/')) {
      res.writeHead(308, { Location: `${basePath}${urlPath}/` }).end();
      return;
    }
    const index = path.join(target, 'index.html');
    if (existsSync(index)) return send(res, 200, index);
  } else if (existsSync(target)) {
    return send(res, 200, target);
  } else if (existsSync(`${target}.html`)) {
    return send(res, 200, `${target}.html`);
  }
  const notFound = path.join(root, '404.html');
  if (existsSync(notFound)) return send(res, 404, notFound);
  res.writeHead(404, { 'Content-Type': 'text/plain' }).end('Not found');
}).listen(port, () => {
  console.log(`[serve-out] serving ${path.relative(process.cwd(), root) || root} at http://localhost:${port}${basePath}/`);
});
