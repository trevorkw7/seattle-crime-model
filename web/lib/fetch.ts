/** Tiny JSON fetch layer with an in-memory cache keyed by URL. */

export class HttpError extends Error {
  constructor(
    public status: number,
    public url: string,
  ) {
    super(`HTTP ${status} for ${url}`);
  }
}

const cache = new Map<string, Promise<unknown>>();

export function fetchJson<T>(url: string, { noCache = false }: { noCache?: boolean } = {}): Promise<T> {
  if (!noCache && cache.has(url)) return cache.get(url) as Promise<T>;
  const p = fetch(url, { cache: 'no-cache' }).then(async (res) => {
    if (!res.ok) throw new HttpError(res.status, url);
    const text = await res.text();
    try {
      return JSON.parse(text) as T;
    } catch {
      // Dev servers answer unknown paths with an HTML 404 page and status 200 on some hosts.
      throw new HttpError(415, url);
    }
  });
  if (!noCache) {
    cache.set(url, p);
    p.catch(() => cache.delete(url));
  }
  return p;
}
