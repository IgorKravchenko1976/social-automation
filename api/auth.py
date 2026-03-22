"""Authentication and rate-limiting utilities for API endpoints."""
from __future__ import annotations

import time
import logging
from collections import defaultdict

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from config.settings import settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_admin(api_key: str | None = Depends(_api_key_header)) -> None:
    """Reject requests without a valid admin API key."""
    if not settings.admin_api_key:
        logger.warning("ADMIN_API_KEY is not set — admin endpoints are LOCKED")
        raise HTTPException(503, "Admin API key not configured")
    if not api_key or api_key != settings.admin_api_key:
        raise HTTPException(403, "Invalid or missing API key")


# ── Simple in-memory rate limiter ─────────────────────────────────────────────

_hits: dict[str, list[float]] = defaultdict(list)
RATE_WINDOW = 60
RATE_MAX_REQUESTS = 10


async def rate_limit_chat(request: Request) -> None:
    """Allow max RATE_MAX_REQUESTS chat calls per IP per minute."""
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _hits[ip]
    bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(bucket) >= RATE_MAX_REQUESTS:
        raise HTTPException(429, "Too many requests, please try later")
    bucket.append(now)
