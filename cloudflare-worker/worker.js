
const TTL_RULES = [
  [/^\/me(?:\/|$)/, 0],
  [/^\/session(?:\/|$)/, 0],
  [/^\/account(?:\/|$)/, 0],
  [/^\/users(?:\/|$)/, 0],
  [/^\/api\/(?:me|home)(?:\/|$)/, 0],
  [/^\/api\/v1(?:\/|$)/, 0],
  [/^\/api\/pulse(?:\/|$)/, 0],
  [/^\/(health|ready)\/?$/, 0],
  [/^\/trending\b/, 600],
  [/^\/charts\b/, 600],
  [/^\/newreleases\b/, 900],
  [/^\/api\/artists(?:\/|$)/, 3600],
  [/^\/api\/artists\/[^\/]+/, 21600],
  [/^\/api\/albums(?:\/|$)/, 3600],
  [/^\/api\/albums\/[^\/]+/, 21600],
  [/^\/(songs|artists|albums|playlists)\/search\//, 900],
  [/^\/api\/search(?:\/|$)/, 300],
  [/^\/playlists\/info\//, 3600],
  [/^\/songs\/info\//, 1800],
  [/^\/(albums|artists)\/info\//, 21600],
  [/^\/(artists|albums)\/(similar|tracks)\//, 21600],
  [/^\/(?:api\/)?songs\/[^\/]+\/stream-info/, 86400],
];

const HOP_BY_HOP = [
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade", "host",
];

function getTtl(url) {
  if (typeof url === "object" && url !== null && url.searchParams) {
    if (url.searchParams.get("refresh") === "true" || url.searchParams.get("force_fresh") === "true") {
      return 0;
    }
    return getTtlFromPath(url.pathname);
  }
  return getTtlFromPath(String(url));
}

function getTtlFromPath(pathname) {
  for (const [pattern, ttl] of TTL_RULES) {
    if (pattern.test(pathname)) return ttl;
  }
  return 300;
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const configured = (env.ALLOWED_ORIGINS || "")
    .split(",").map((value) => value.trim()).filter(Boolean);
  // Mobile clients do not send an Origin header. Browser access must be
  // explicitly allowlisted so another site cannot read API responses.
  const allowOrigin = origin && configured.includes(origin) ? origin : null;
  const headers = {
    "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Request-ID",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
  if (allowOrigin) headers["Access-Control-Allow-Origin"] = allowOrigin;
  return headers;
}

function responseHeaders(source, request, env, cacheStatus) {
  const headers = new Headers(source);
  for (const name of HOP_BY_HOP) headers.delete(name);
  for (const [name, value] of Object.entries(corsHeaders(request, env))) {
    headers.set(name, value);
  }
  headers.set("X-Cache", cacheStatus);
  headers.set("X-CDN", "CF-Music-Hub");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  return headers;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const backend = (env.BACKEND_URL || "https://turks-young.onrender.com")
      .replace(/\/$/, "");

    if (request.method === "OPTIONS") {
      if (request.headers.get("Origin") && !corsHeaders(request, env)["Access-Control-Allow-Origin"]) {
        return new Response(null, { status: 403 });
      }
      return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    }

    if (url.pathname === "/" && request.method === "GET") {
      return Response.json(
        { name: "Music Hub API", status: "live", docs: "/docs", health: "/health", ready: "/ready" },
        { headers: responseHeaders({}, request, env, "EDGE") },
      );
    }

    const ttl = getTtl(url);
    const upstreamUrl = `${backend}${url.pathname}${url.search}`;
    const headers = new Headers(request.headers);
    for (const name of HOP_BY_HOP) headers.delete(name);
    headers.set("X-Forwarded-Host", url.host);
    headers.set("X-Forwarded-Proto", "https");
    // Lets the origin trust CF-Connecting-IP for rate limiting. Configure with
    // `wrangler secret put PROXY_SHARED_SECRET` and mirror the same value as
    // PROXY_SHARED_SECRET on the backend. A client-supplied header is dropped.
    headers.delete("X-Proxy-Secret");
    if (env.PROXY_SHARED_SECRET) headers.set("X-Proxy-Secret", env.PROXY_SHARED_SECRET);
    const upstreamRequest = new Request(upstreamUrl, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "follow",
    });

    // Never cache a request that carries credentials.  Cloudflare's cache key
    // below is deliberately URL-only for public catalogue responses, so using
    // it for a bearer-token request could expose one user's data to another.
    const hasCredentials = request.headers.has("Authorization") || request.headers.has("Cookie");
    try {
      if (request.method !== "GET" || ttl === 0 || hasCredentials) {
        const response = await fetch(upstreamRequest);
        const headers = responseHeaders(response.headers, request, env, "BYPASS");
        if (hasCredentials) {
          headers.set("Cache-Control", "private, no-store");
          headers.append("Vary", "Authorization, Cookie");
        }
        return new Response(response.body, {
          status: response.status,
          headers,
        });
      }

      // Never edge-cache an empty search page: a transient backend timeout
      // would otherwise pin "no results" for this query for the whole TTL.
      if (url.pathname === "/api/search" || url.pathname === "/api/catalog/search") {
        const response = await fetch(upstreamRequest);
        try {
          const cloned = response.clone();
          const json = await cloned.json();
          const data = json.data || json;
          const groups = ["items", "songs", "albums", "artists", "playlists"];
          const hasResults = groups.some((key) => Array.isArray(data[key]) && data[key].length > 0);
          if (!response.ok || !hasResults) {
            const outgoing = responseHeaders(response.headers, request, env, "BYPASS");
            outgoing.set("Cache-Control", "no-store, no-cache, must-revalidate");
            return new Response(response.body, { status: response.status, headers: outgoing });
          }
        } catch (_) {}
        // Non-empty: fall through to the caching fetch below so the edge
        // cache is populated (same double-fetch pattern as /api/albums/).
      }

      if (url.pathname.startsWith("/api/albums/")) {
        const response = await fetch(upstreamRequest);
        try {
          const cloned = response.clone();
          const json = await cloned.json();
          const alb = json.data || json;
          const tracks = alb.tracks || alb.songs || [];
          if (!Array.isArray(tracks) || tracks.length === 0) {
            const outgoing = responseHeaders(response.headers, request, env, "BYPASS");
            outgoing.set("Cache-Control", "no-store, no-cache, must-revalidate");
            return new Response(response.body, { status: response.status, headers: outgoing });
          }
        } catch (_) {}
      }

      const response = await fetch(upstreamRequest, {
        cf: {
          cacheEverything: true,
          cacheTtl: ttl,
          cacheTtlByStatus: { "200-299": ttl, "404": 30, "500-599": 0 },
          cacheKey: upstreamUrl,
        },
      });
      const cacheStatus = response.headers.get("CF-Cache-Status") || "MISS";
      const outgoing = responseHeaders(response.headers, request, env, cacheStatus);
      outgoing.set("Cache-Control", `public, max-age=${ttl}, s-maxage=${ttl}`);
      return new Response(response.body, { status: response.status, headers: outgoing });
    } catch (error) {
      console.error(JSON.stringify({
        event: "upstream_error",
        path: url.pathname,
        error: error instanceof Error ? error.message : String(error),
      }));
      return Response.json(
        { error: "Music service is temporarily unavailable" },
        { status: 502, headers: responseHeaders({}, request, env, "ERROR") },
      );
    }
  },
};
