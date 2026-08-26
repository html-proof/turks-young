const TTL_RULES = [
  [/^\/me(?:\/|$)/, 0],
  [/^\/(health|ready)\/?$/, 0],
  [/^\/trending\b/, 600],
  [/^\/charts\b/, 600],
  [/^\/newreleases\b/, 900],
  [/^\/(songs|artists|albums|playlists)\/search\//, 900],
  [/^\/playlists\/info\//, 3600],
  [/^\/(songs|albums|artists)\/info\//, 21600],
  [/^\/(artists|albums)\/(similar|tracks)\//, 21600],
];

const HOP_BY_HOP = [
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade", "host",
];

function getTtl(pathname) {
  for (const [pattern, ttl] of TTL_RULES) {
    if (pattern.test(pathname)) return ttl;
  }
  return 300;
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const configured = (env.ALLOWED_ORIGINS || "*")
    .split(",").map((value) => value.trim()).filter(Boolean);
  const allowOrigin = configured.includes("*")
    ? "*"
    : origin && configured.includes(origin)
      ? origin
      : configured[0] || "*";
  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Request-ID",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
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
      return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    }

    if (url.pathname === "/" && request.method === "GET") {
      return Response.json(
        { name: "Music Hub API", status: "live", docs: "/docs", health: "/health", ready: "/ready" },
        { headers: responseHeaders({}, request, env, "EDGE") },
      );
    }

    const ttl = getTtl(url.pathname);
    const upstreamUrl = `${backend}${url.pathname}${url.search}`;
    const headers = new Headers(request.headers);
    for (const name of HOP_BY_HOP) headers.delete(name);
    headers.set("X-Forwarded-Host", url.host);
    headers.set("X-Forwarded-Proto", "https");
    const upstreamRequest = new Request(upstreamUrl, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "follow",
    });

    try {
      if (request.method !== "GET" || ttl === 0) {
        const response = await fetch(upstreamRequest);
        return new Response(response.body, {
          status: response.status,
          headers: responseHeaders(response.headers, request, env, "BYPASS"),
        });
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
