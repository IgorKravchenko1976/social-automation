"""Check token validity and expiration dates for all platforms."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class TokenStatus:
    platform: str
    configured: bool
    valid: bool
    expires_at: Optional[datetime] = None
    days_remaining: Optional[int] = None
    error: Optional[str] = None


async def check_all_tokens() -> list[TokenStatus]:
    results: list[TokenStatus] = []

    results.append(await _check_telegram())
    results.append(await _check_facebook())
    results.append(await _check_instagram())
    results.append(_check_simple("X / Twitter", settings.twitter_bearer_token))
    results.append(_check_simple("TikTok", settings.tiktok_access_token))

    return results


async def _check_telegram() -> TokenStatus:
    token = settings.telegram_bot_token
    if not token or token.startswith("your-"):
        return TokenStatus("Telegram", configured=False, valid=False)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = r.json()
            if data.get("ok"):
                return TokenStatus("Telegram", configured=True, valid=True)
            return TokenStatus("Telegram", configured=True, valid=False,
                               error=data.get("description", "invalid"))
    except Exception as e:
        return TokenStatus("Telegram", configured=True, valid=False, error=str(e))


async def _check_facebook() -> TokenStatus:
    from stats.token_renewer import get_active_token
    db_token = await get_active_token("facebook")
    token = db_token or settings.facebook_page_access_token
    if not token or token.startswith("your-"):
        return TokenStatus("Facebook", configured=False, valid=False)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://graph.facebook.com/v21.0/debug_token",
                params={"input_token": token, "access_token": token},
            )
            data = r.json().get("data", {})
            is_valid = data.get("is_valid", False)
            expires_ts = data.get("expires_at", 0)

            expires_at = None
            days_remaining = None
            if expires_ts and expires_ts > 0:
                expires_at = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
                days_remaining = (expires_at - datetime.now(timezone.utc)).days

            return TokenStatus(
                "Facebook",
                configured=True,
                valid=is_valid,
                expires_at=expires_at,
                days_remaining=days_remaining,
            )
    except Exception as e:
        return TokenStatus("Facebook", configured=True, valid=False, error=str(e))


async def _check_instagram() -> TokenStatus:
    from datetime import timedelta
    from stats.token_renewer import get_active_token
    from db.database import async_session
    from db.models import TokenStore
    from sqlalchemy import select

    db_token = await get_active_token("instagram")
    token = db_token or settings.instagram_access_token
    if not token or not settings.instagram_user_id:
        return TokenStatus("Instagram", configured=False, valid=False)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://graph.instagram.com/v21.0/{settings.instagram_user_id}",
                params={"access_token": token, "fields": "id,username"},
            )
            data = r.json()
            if "error" in data:
                return TokenStatus("Instagram", configured=True, valid=False,
                                   error=data["error"].get("message", "invalid"))

        expires_at = None
        days_remaining = None
        async with async_session() as session:
            result = await session.execute(
                select(TokenStore).where(TokenStore.platform == "instagram")
            )
            row = result.scalar_one_or_none()
            if row and row.expires_at:
                exp = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
                expires_at = exp
                days_remaining = (exp - datetime.now(timezone.utc)).days

        return TokenStatus("Instagram", configured=True, valid=True,
                           expires_at=expires_at, days_remaining=days_remaining)
    except Exception as e:
        return TokenStatus("Instagram", configured=True, valid=False, error=str(e))


def _check_simple(name: str, credential: str) -> TokenStatus:
    if not credential or credential.startswith("your-"):
        return TokenStatus(name, configured=False, valid=False)
    return TokenStatus(name, configured=True, valid=True)
