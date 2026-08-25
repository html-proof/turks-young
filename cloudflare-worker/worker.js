/**
 * Soundwaves CDN Worker — ultra-low latency build
 *
 * Uses Cloudflare's native CF-cache (cf.cacheEverything) instead of the
 * programmatic Cache API. Native cache is served from in-memory at every
 * edge PoP — typical HIT latency 5–20 ms vs 50–150 ms for Cache API.
 *
 * Cache TTLs (seconds):
 *   /trending, /charts             600   (10 min)
 *   /newreleases, search           900   (15 min)
 *   info, similar                  21600 (6 h)
 *   /playlists/info                3600  (1 h)
 *   /health, /ready, /me           0     (bypass)
 */

const BACKEND = "https://turks-young.onrender.com";

const TTL_RULES = [
  [/^\/me\//,                              0],
  [/^\/(health|ready)\/?$/,               0],
  [/^\/trending\b/,                        600],
  [/^\/charts\b/,                          600],
  [/^\/newreleases\b/,                     900],
  [/^\/(songs|artists|albums)\/search\//,  900],
  [/^\/playlists\/info\//,                 3600],
  [/^\/(songs|albums|artists)\/info\//,    21600],
  [/^\/(artists|albums)\/similar\//,       21600],
];

function getTTL(pathname) {
  for (const [re, ttl] of TTL_RULES) {
    if (re.test(pathname)) return ttl;
  }
  return 300;
}

const CORS = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    const ttl      = getTTL(url.pathname);
    const upstream = BACKEND + url.pathname + url.search;

    // Build upstream request — strip CF-specific headers to avoid loops
    const upReq = new Request(upstream, {
      method:  request.method,
      headers: request.headers,
      body:    ["GET","HEAD"].includes(request.method) ? undefined : request.body,
    });

    // Non-GET or bypass routes — pass through without caching
    if (request.method !== "GET" || ttl === 0) {
      const res = await fetch(upReq);
      return new Response(res.body, {
        status:  res.status,
        headers: { ...Object.fromEntries(res.headers), ...CORS, "X-Cache": "BYPASS", "X-CDN": "CF-Soundwaves" },
      });
    }

    // Native Cloudflare CDN cache — fastest path
    // cf.cacheEverything + cf.cacheTtlByStatus caches at every PoP in memory
    const res = await fetch(upReq, {
      cf: {
        cacheEverything:    true,
        cacheTtl:           ttl,
        cacheTtlByStatus:   { "200-299": ttl, "404": 30, "500-599": 0 },
        cacheKey:           upstream,
      },
    });

    const cfCache = res.headers.get("CF-Cache-Status") ?? "UNKNOWN";
    // CF-Cache-Status: HIT | MISS | EXPIRED | REVALIDATED | UPDATING | STALE | BYPASS

    const headers = new Headers(res.headers);
    Object.entries(CORS).forEach(([k, v]) => headers.set(k, v));
    headers.set("X-Cache",      cfCache);
    headers.set("X-CDN",        "CF-Soundwaves");
    headers.set("Cache-Control", `public, max-age=${ttl}, s-maxage=${ttl}`);

    return new Response(res.body, { status: res.status, headers });
  },
};
