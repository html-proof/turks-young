import hmac

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def client_key(request: Request, shared_secret: str = "") -> str:
    """Identify the caller without trusting headers any client can forge.

    ``CF-Connecting-IP`` is honoured only when the request carries the shared
    secret that our own Cloudflare Worker attaches; otherwise anyone reaching
    the origin directly could choose a fresh key per request and bypass the
    limit entirely. ``X-Forwarded-For`` is read from its *last* entry: the
    platform proxy appends the address it actually saw, while the first entry
    is whatever the client decided to send. Behind no proxy at all the socket
    address is used.
    """
    if shared_secret:
        presented = request.headers.get("x-proxy-secret", "")
        if presented and hmac.compare_digest(presented, shared_secret):
            edge_ip = request.headers.get("cf-connecting-ip")
            if edge_ip:
                return edge_ip.strip()[:128]
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()[:128]
    return (request.client.host if request.client else "unknown")[:128]


class RedisRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/health", "/ready"} or request.url.path.startswith("/docs"):
            return await call_next(request)
        container = getattr(request.app.state, "container", None)
        if container is None or not container.cache.connected:
            return await call_next(request)
        secret = container.settings.proxy_shared_secret
        client = client_key(request, secret.get_secret_value() if secret else "")
        window = container.settings.rate_limit_window_seconds
        try:
            count = await container.cache.increment_window(f"rate:{client}", window)
        except Exception:
            # A cache outage should reduce protection, not take down the API.
            return await call_next(request)
        if count > container.settings.rate_limit_requests:
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "rate_limited", "message": "Too many requests"}},
                headers={"Retry-After": str(window)},
            )
        return await call_next(request)
