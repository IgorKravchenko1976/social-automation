"""Check token validity and expiration dates for all platforms."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings, is_placeholder, ensure_utc
from config.platforms import FACEBOOK_GRAPH_API, INSTAGRAM_GRAPH_API

logger = logging.getLogger(__name__)


@dataclass
class TokenStatus:
    platform: str
    configured: bool
    valid: bool
    expires_at: Optional[datetime] = None
    days_remaining: Optional[int] = None
    error: Optional[str] = None
    token_source: Optional[str] = None


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
    if is_placeholder(token):
        return TokenStatus("Telegram", configured=False, valid=False)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = r.json()
            if data.get("ok"):
                return TokenStatus("Telegram", configured=True, valid=True,
                                   token_source="Bot Token")
            return TokenStatus("Telegram", configured=True, valid=False,
                               error=data.get("description", "invalid"),
                               token_source="Bot Token")
    except Exception as e:
        return TokenStatus("Telegram", configured=True, valid=False, error=str(e),
                           token_source="Bot Token")


async def _check_facebook() -> TokenStatus:
    from stats.token_renewer import get_active_token
    db_token = await get_active_token("facebook")
    token = db_token or settings.facebook_page_access_token
    if is_placeholder(token):
        return TokenStatus("Facebook", configured=False, valid=False)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{FACEBOOK_GRAPH_API}/debug_token",
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
                "Facebook", configured=True, valid=is_valid,
                expires_at=expires_at, days_remaining=days_remaining,
                token_source="Page Access Token",
            )
    except Exception as e:
        return TokenStatus("Facebook", configured=True, valid=False, error=str(e),
                           token_source="Page Access Token")


async def _check_instagram() -> TokenStatus:
    """Check Instagram using the same credential resolution as the platform adapter:
    1. Facebook Page Token + IG Business Account discovery (primary)
    2. Dedicated Instagram token + configured user ID (fallback)
    """
    from stats.token_renewer import get_active_token

    fb_token = await get_active_token("facebook")
    if not fb_token and not is_placeholder(settings.facebook_page_access_token):
        fb_token = settings.facebook_page_access_token

    # Strategy 1: Facebook Page Token + IG Business Account (matches InstagramPlatform._resolve_credentials)
    if fb_token and settings.facebook_page_id and not is_placeholder(settings.facebook_page_id):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}",
                    params={"access_token": fb_token, "fields": "instagram_business_account"},
                )
                data = resp.json()
                ig_biz = data.get("instagram_business_account", {})
                ig_id = ig_biz.get("id")
                if ig_id:
                    r2 = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{ig_id}",
                        params={"access_token": fb_token, "fields": "id,username"},
                    )
                    ig_data = r2.json()
                    if "error" not in ig_data:
                        expires_at, days_remaining = await _get_token_expiry("facebook")
                        return TokenStatus(
                            "Instagram", configured=True, valid=True,
                            expires_at=expires_at, days_remaining=days_remaining,
                            token_source="Facebook Page Token",
                        )
        except Exception as e:
            logger.warning("Instagram check via FB Page Token failed: %s", e)

    # Strategy 2: Dedicated Instagram token
    ig_token = await get_active_token("instagram")
    if not ig_token:
        ig_token = settings.instagram_access_token if not is_placeholder(settings.instagram_access_token) else None

    if not ig_token:
        has_fb = fb_token and settings.facebook_page_id
        if has_fb:
            return TokenStatus("Instagram", configured=True, valid=False,
                               error="FB token є, але IG Business Account не прив'язаний до сторінки",
                               token_source="Facebook Page Token")
        return TokenStatus("Instagram", configured=False, valid=False)

    ig_user_id = settings.instagram_user_id
    if not ig_user_id or is_placeholder(ig_user_id):
        return TokenStatus("Instagram", configured=False, valid=False)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{FACEBOOK_GRAPH_API}/{ig_user_id}",
                params={"access_token": ig_token, "fields": "id,username"},
            )
            data = r.json()
            if "error" in data:
                r2 = await client.get(
                    f"{INSTAGRAM_GRAPH_API}/{ig_user_id}",
                    params={"access_token": ig_token, "fields": "id,username"},
                )
                data = r2.json()
            if "error" in data:
                return TokenStatus("Instagram", configured=True, valid=False,
                                   error=data["error"].get("message", "invalid"),
                                   token_source="Instagram Token")

        expires_at, days_remaining = await _get_token_expiry("instagram")
        return TokenStatus("Instagram", configured=True, valid=True,
                           expires_at=expires_at, days_remaining=days_remaining,
                           token_source="Instagram Token")
    except Exception as e:
        return TokenStatus("Instagram", configured=True, valid=False, error=str(e),
                           token_source="Instagram Token")


async def _get_token_expiry(platform: str) -> tuple[Optional[datetime], Optional[int]]:
    """Read token expiry from DB for a given platform."""
    from db.database import async_session
    from db.models import TokenStore
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(
            select(TokenStore).where(TokenStore.platform == platform)
        )
        row = result.scalar_one_or_none()
        if row and row.expires_at:
            exp = ensure_utc(row.expires_at)
            days = (exp - datetime.now(timezone.utc)).days
            return exp, days
    return None, None


def _check_simple(name: str, credential: str) -> TokenStatus:
    if is_placeholder(credential):
        return TokenStatus(name, configured=False, valid=False)
    return TokenStatus(name, configured=True, valid=True, token_source="Access Token")
