"""Shared Telegram Bot API helpers (used by both adapter and bot)."""
from __future__ import annotations

import httpx

from config.settings import settings

API_BASE = "https://api.telegram.org/bot{token}"

_http_client: httpx.AsyncClient | None = None


def api_url(method: str) -> str:
    return f"{API_BASE.format(token=settings.telegram_bot_token)}/{method}"


async def ensure_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=60)
    return _http_client


async def request(method: str, **params):
    client = await ensure_client()
    resp = await client.post(api_url(method), json=params)
    return resp.json()


async def close_client() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None
