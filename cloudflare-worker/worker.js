/**
 * Soundwaves CDN Worker
 * Proxies turks-young.onrender.com and caches responses at Cloudflare's edge.
 *
 * Cache TTLs match the backend Redis TTLs:
 *   /trending, /charts        → 10 min
 *   /newreleases              → 15 min
 *   /songs/search, /artists/search, /albums/search → 15 min
 *   /songs/info, /albums/info, /artists/info       → 6 h
 *   /playlists/info           → 1 h
 *   /artists/similar, /albums/similar              → 1 h
 *   /health, /ready, /        → no cache
 *   Personalization routes    → no cache (user-specific)
 */

const BACKEND = "https://turks-young.onrender.com";

// Route → TTL seconds (0 = bypass cache)
const TTL_MAP = [
  [/^\/(health|ready)\/?$/,              0],
  [/^\/me\//,                            0],   // personalization — never cache
  [/^\/trending\b/,                      600],
  [/^\/charts\b/,                        600],
  [/^\/newreleases\b/,                   900],
  [/^\/(songs|artists|albums)\/search\//,900],
  [/^\/(songs|albums|artists)\/info\//,  21600],
  [/^\/playlists\/info\//,               3600],
  [/^\/(artists|albums)\/similar\//,     3600],
];

function getTTL(pathname) {
  for (const [pattern, ttl] of TTL_MAP) {
    if (pattern.test(pathname)) return ttl;
  }
  return 300; // 5-min default for anything else
}

function addCorsHeaders(headers) {
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS");
  headers.set("Access-Control-Allow-Headers", "*");
  return headers;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: addCorsHeaders(new Headers()),
      });
    }

    const ttl = getTTL(url.pathname);

    // Build the upstream request
    const upstreamUrl = BACKEND + url.pathname + url.search;
    const upstreamReq = new Request(upstreamUrl, {
      method: request.method,
      headers: request.headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    });

    // Bypass cache for non-GET or ttl=0 routes
    if (request.method !== "GET" || ttl === 0) {
      const res = await fetch(upstreamReq);
      const headers = addCorsHeaders(new Headers(res.headers));
      headers.set("X-Cache", "BYPASS");
      headers.set("X-CDN", "Cloudflare-Soundwaves");
      return new Response(res.body, { status: res.status, headers });
    }

    // Use Cloudflare Cache API
    const cache = caches.default;
    const cacheKey = new Request(upstreamUrl, { method: "GET" });

    let response = await cache.match(cacheKey);
    if (response) {
      const headers = addCorsHeaders(new Headers(response.headers));
      headers.set("X-Cache", "HIT");
      headers.set("X-CDN", "Cloudflare-Soundwaves");
      return new Response(response.body, { status: response.status, headers });
    }

    // Cache miss — fetch from Render
    const upstream = await fetch(upstreamReq);

    if (!upstream.ok) {
      // Don't cache errors
      const headers = addCorsHeaders(new Headers(upstream.headers));
      headers.set("X-Cache", "MISS");
      headers.set("X-CDN", "Cloudflare-Soundwaves");
      return new Response(upstream.body, { status: upstream.status, headers });
    }

    // Clone and store in cache
    const headers = addCorsHeaders(new Headers(upstream.headers));
    headers.set("Cache-Control", `public, max-age=${ttl}, s-maxage=${ttl}`);
    headers.set("X-Cache", "MISS");
    headers.set("X-CDN", "Cloudflare-Soundwaves");
    headers.set("Content-Type", "application/json");

    const toCache = new Response(upstream.clone().body, {
      status: upstream.status,
      headers,
    });
    ctx.waitUntil(cache.put(cacheKey, toCache));

    return new Response(upstream.body, { status: upstream.status, headers });
  },
};
