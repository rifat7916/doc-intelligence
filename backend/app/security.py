"""API-key authentication and a Redis-backed fixed-window rate limiter."""
from __future__ import annotations

import hmac
import logging
import time

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from .config import get_settings

log = logging.getLogger(__name__)
_redis = None


# declared as a security scheme so the interactive docs (/docs) show an "Authorize" button
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Shared secret: API_KEY from .env")


def require_api_key(x_api_key: str | None = Security(_api_key_header)) -> None:
    key = get_settings().api_key
    if not key:  # auth disabled (local development)
        return
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key",
                            headers={"WWW-Authenticate": "ApiKey"})


def _redis_client():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
    return _redis


def rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_per_minute
    if not limit:
        return
    try:
        r = _redis_client()
        key = f"rl:{request.client.host if request.client else 'unknown'}:{int(time.time() // 60)}"
        n = r.incr(key)
        if n == 1:
            r.expire(key, 70)
    except Exception:  # noqa: BLE001 - fail open: a Redis outage must not take the API down
        return
    if n > limit:
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "30"})
