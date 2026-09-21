import { cacheDecision } from "./cache-policy";

type CacheStatus = "HIT" | "MISS" | "BYPASS";

// PROXY_SHARED_SECRET is a Wrangler secret, so it is absent from the generated
// Env type and from local development unless `wrangler secret put` was run.
type CdnEnv = Env & { PROXY_SHARED_SECRET?: string };

function canStore(response: Response): boolean {
  if (response.status !== 200 || response.headers.has("set-cookie")) {
    return false;
  }
  const cacheControl = response.headers.get("cache-control")?.toLowerCase() ?? "";
  if (cacheControl.includes("no-store") || cacheControl.includes("private")) {
    return false;
  }
  const vary = response.headers.get("vary")?.trim();
  return vary !== "*";
}

function cacheableResponse(response: Response, ttlSeconds: number): Response {
  const headers = new Headers(response.headers);
  headers.set("cache-control", `public, max-age=${ttlSeconds}`);
  headers.delete("set-cookie");
  return new Response(response.body, {
    headers,
    status: response.status,
    statusText: response.statusText,
  });
}

function withCdnHeaders(
  response: Response,
  status: CacheStatus,
  reason: string,
): Response {
  const headers = new Headers(response.headers);
  headers.set("x-music-hub-cache", status);
  headers.set("x-music-hub-cache-reason", reason);
  headers.set("x-content-type-options", "nosniff");
  return new Response(response.body, {
    headers,
    status: response.status,
    statusText: response.statusText,
  });
}

function originRequest(request: Request, originValue: string, proxySecret?: string): Request {
  const incomingUrl = new URL(request.url);
  const origin = new URL(originValue);
  if (origin.protocol !== "https:") {
    throw new Error("ORIGIN_URL must use HTTPS");
  }
  const destination = new URL(incomingUrl.pathname + incomingUrl.search, origin);
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.set("x-forwarded-host", incomingUrl.host);
  headers.set("x-forwarded-proto", "https");
  headers.set("x-music-hub-cdn", "cloudflare");
  // Lets the origin trust cf-connecting-ip for rate limiting. Mirror the same
  // value as PROXY_SHARED_SECRET on the backend. A client-supplied header is
  // always dropped so nobody can claim the edge's identity.
  headers.delete("x-proxy-secret");
  if (proxySecret) {
    headers.set("x-proxy-secret", proxySecret);
  }
  if (!headers.has("x-request-id")) {
    headers.set("x-request-id", crypto.randomUUID());
  }
  return new Request(destination, {
    body: request.body,
    headers,
    method: request.method,
    redirect: "manual",
  });
}

async function fetchOrigin(request: Request, env: CdnEnv): Promise<Response> {
  return fetch(originRequest(request, env.ORIGIN_URL, env.PROXY_SHARED_SECRET));
}

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const decision = cacheDecision(request);
    try {
      if (!decision.cacheable) {
        const response = await fetchOrigin(request, env);
        return withCdnHeaders(response, "BYPASS", decision.reason);
      }

      const cache = caches.default;
      const cacheKey = new Request(request.url, { method: "GET" });
      const cached = await cache.match(cacheKey);
      if (cached !== undefined) {
        return withCdnHeaders(cached, "HIT", decision.reason);
      }

      const originResponse = await fetchOrigin(request, env);
      if (!canStore(originResponse)) {
        return withCdnHeaders(originResponse, "BYPASS", "origin-policy");
      }

      const prepared = cacheableResponse(originResponse, decision.ttlSeconds);
      ctx.waitUntil(cache.put(cacheKey, prepared.clone()));
      return withCdnHeaders(prepared, "MISS", decision.reason);
    } catch (error) {
      console.error(
        JSON.stringify({
          error: error instanceof Error ? error.message : String(error),
          message: "cdn_origin_request_failed",
          method: request.method,
          path: new URL(request.url).pathname,
        }),
      );
      return Response.json(
        { detail: "The music service is temporarily unavailable" },
        {
          headers: {
            "cache-control": "no-store",
            "x-music-hub-cache": "BYPASS",
            "x-music-hub-cache-reason": "origin-error",
          },
          status: 502,
        },
      );
    }
  },
} satisfies ExportedHandler<CdnEnv>;
