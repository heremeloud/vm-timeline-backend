import os
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


class RateLimiter:
    """Small in-memory sliding-window limiter for a single API instance."""

    def __init__(self):
        self.api_limit = _positive_int("RATE_LIMIT_PER_MINUTE", 180)
        self.login_limit = _positive_int("LOGIN_RATE_LIMIT", 5)
        self.login_window = _positive_int("LOGIN_RATE_WINDOW_SECONDS", 900)
        self._requests = defaultdict(deque)
        self._lock = Lock()
        self._last_cleanup = time.monotonic()

    @staticmethod
    def _client_ip(request: Request) -> str:
        # Hosting proxies set these headers to the original visitor address.
        cloudflare_ip = request.headers.get("cf-connecting-ip")
        if cloudflare_ip:
            return cloudflare_ip.strip()

        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()

        return request.client.host if request.client else "unknown"

    def _rule(self, request: Request):
        if request.method == "OPTIONS":
            return None

        path = request.url.path.rstrip("/") or "/"
        if path in {"/", "/docs", "/redoc", "/openapi.json"} or path.startswith("/static/"):
            return None

        if path == "/auth/login":
            return "login", self.login_limit, self.login_window

        return "api", self.api_limit, 60

    async def __call__(self, request: Request, call_next):
        rule = self._rule(request)
        if rule is None:
            return await call_next(request)

        bucket, limit, window = rule
        now = time.monotonic()
        key = (bucket, self._client_ip(request))

        with self._lock:
            if now - self._last_cleanup >= self.login_window:
                stale_before = now - self.login_window
                stale_keys = [
                    stored_key
                    for stored_key, stored_times in self._requests.items()
                    if not stored_times or stored_times[-1] <= stale_before
                ]
                for stale_key in stale_keys:
                    del self._requests[stale_key]
                self._last_cleanup = now

            timestamps = self._requests[key]
            cutoff = now - window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                retry_after = max(1, int(window - (now - timestamps[0])) + 1)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please try again later."},
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(limit),
                        "X-RateLimit-Remaining": "0",
                    },
                )

            timestamps.append(now)
            remaining = max(0, limit - len(timestamps))

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
