"""Automatic token renewal for platforms that support it."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import httpx

from config.settings import settings, ensure_utc, is_placeholder
from config.platforms import FACEBOOK_GRAPH_API as GRAPH_API, INSTAGRAM_GRAPH_API
from db.database import async_session
from db.models import TokenStore
from sqlalchemy import select

logger = logging.getLogger(__name__)

RENEW_THRESHOLD_DAYS = 7


async def get_active_token(platform: str) -> str | None:
    """Return the freshest valid token: DB first, then env fallback."""
    async with async_session() as session:
        result = await session.execute(
            select(TokenStore).where(TokenStore.platform == platform)
        )
        row = result.scalar_one_or_none()
        if row and row.token:
            if row.expires_at is None:
                return row.token
            exp = ensure_utc(row.expires_at)
            if exp > datetime.now(timezone.utc):
                return row.token
    return None


async def _save_token(platform: str, token: str, expires_at: datetime | None) -> None:
    naive_expires = None
    if expires_at is not None:
        naive_expires = expires_at.astimezone(timezone.utc).replace(tzinfo=None)

    async with async_session() as session:
        result = await session.execute(
            select(TokenStore).where(TokenStore.platform == platform)
        )
        row = result.scalar_one_or_none()
        if row:
            row.token = token
            row.expires_at = naive_expires
        else:
            session.add(TokenStore(platform=platform, token=token, expires_at=naive_expires))
        await session.commit()


async def _renew_facebook() -> bool:
    """Exchange current Facebook Page Token for a new long-lived one.

    Flow: current_page_token → fb_exchange_token → new long-lived page token.
    Requires facebook_app_id + facebook_app_secret.
    """
    app_id = settings.facebook_app_id
    app_secret = settings.facebook_app_secret
    if not app_id or not app_secret:
        logger.warning("Facebook App ID/Secret not configured — cannot auto-renew")
        return False

    current_token = await get_active_token("facebook")
    if not current_token:
        current_token = settings.facebook_page_access_token
    if is_placeholder(current_token):
        logger.warning("No valid Facebook token to renew")
        return False

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            debug_resp = await client.get(
                f"{GRAPH_API}/debug_token",
                params={"input_token": current_token, "access_token": current_token},
            )
            debug_data = debug_resp.json().get("data", {})
            expires_ts = debug_data.get("expires_at", 0)
            is_valid = debug_data.get("is_valid", False)

            if not is_valid:
                logger.error("Facebook token is invalid — manual renewal required")
                return False

            if expires_ts and expires_ts > 0:
                expires_at = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
                days_left = (expires_at - datetime.now(timezone.utc)).days
                if days_left > RENEW_THRESHOLD_DAYS:
                    logger.info("Facebook token still valid for %d days — no renewal needed", days_left)
                    await _save_token("facebook", current_token, expires_at)
                    return True
                logger.info("Facebook token expires in %d days — renewing...", days_left)
            else:
                logger.info("Facebook token has no expiry — saving and skipping renewal")
                await _save_token("facebook", current_token, None)
                return True

            exchange_resp = await client.get(
                f"{GRAPH_API}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "fb_exchange_token": current_token,
                },
            )
            exchange_data = exchange_resp.json()

            if "error" in exchange_data:
                err_msg = exchange_data["error"].get("message", str(exchange_data["error"]))
                logger.error("Facebook token exchange failed: %s", err_msg)
                return False

            new_token = exchange_data.get("access_token")
            new_expires_in = exchange_data.get("expires_in", 0)

            if not new_token:
                logger.error("No access_token in exchange response: %s", exchange_data)
                return False

            new_expires_at = None
            if new_expires_in:
                new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=new_expires_in)

            new_debug = await client.get(
                f"{GRAPH_API}/debug_token",
                params={"input_token": new_token, "access_token": new_token},
            )
            new_debug_data = new_debug.json().get("data", {})
            final_expires = new_debug_data.get("expires_at", 0)
            if final_expires and final_expires > 0:
                new_expires_at = datetime.fromtimestamp(final_expires, tz=timezone.utc)

            await _save_token("facebook", new_token, new_expires_at)

            days_valid = (new_expires_at - datetime.now(timezone.utc)).days if new_expires_at else "unlimited"
            logger.info("Facebook token renewed! Valid for %s days", days_valid)
            return True

    except Exception:
        logger.exception("Facebook token renewal failed")
        return False


async def _renew_instagram() -> bool:
    """Sync Instagram token from Facebook (they share the same Page Access Token for Business API).

    For Instagram Business accounts connected via Facebook Page, the Facebook Page
    Access Token is used for all Instagram Graph API calls. So we just copy the
    Facebook token to the Instagram slot.
    """
    fb_token = await get_active_token("facebook")
    if fb_token:
        async with async_session() as session:
            result = await session.execute(
                select(TokenStore).where(TokenStore.platform == "facebook")
            )
            fb_row = result.scalar_one_or_none()
            fb_expires = fb_row.expires_at if fb_row else None

        await _save_token("instagram", fb_token, fb_expires)
        logger.info("Instagram token synced from Facebook token")
        return True

    current_token = await get_active_token("instagram")
    if not current_token:
        current_token = settings.instagram_access_token
    if not current_token:
        logger.warning("No Instagram token to renew")
        return False

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{INSTAGRAM_GRAPH_API}/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": current_token,
                },
            )
            data = resp.json()

            if "error" in data:
                err = data["error"].get("message", str(data["error"]))
                logger.error("Instagram token refresh failed: %s", err)
                return False

            new_token = data.get("access_token")
            expires_in = data.get("expires_in", 0)

            if not new_token:
                logger.error("No access_token in Instagram refresh response")
                return False

            new_expires_at = None
            if expires_in:
                new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            await _save_token("instagram", new_token, new_expires_at)
            days_valid = expires_in // 86400 if expires_in else "unknown"
            logger.info("Instagram token refreshed! Valid for %s days", days_valid)
            return True

    except Exception:
        logger.exception("Instagram token renewal failed")
        return False


async def renew_all_tokens() -> dict[str, bool]:
    """Attempt to renew all platform tokens. Returns {platform: success}."""
    results: dict[str, bool] = {}

    token = settings.facebook_page_access_token
    db_token = await get_active_token("facebook")
    if token or db_token:
        results["facebook"] = await _renew_facebook()

    ig_token = settings.instagram_access_token
    ig_db = await get_active_token("instagram")
    if ig_token or ig_db or results.get("facebook"):
        results["instagram"] = await _renew_instagram()

    return results


async def seed_tokens_from_env() -> None:
    """On first startup, save env tokens to DB so renewal can work."""
    # Facebook
    fb_token = settings.facebook_page_access_token
    if not is_placeholder(fb_token):
        existing = await get_active_token("facebook")
        if not existing:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        f"{GRAPH_API}/debug_token",
                        params={"input_token": fb_token, "access_token": fb_token},
                    )
                    data = r.json().get("data", {})
                    expires_ts = data.get("expires_at", 0)
                    expires_at = None
                    if expires_ts and expires_ts > 0:
                        expires_at = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
                await _save_token("facebook", fb_token, expires_at)
                logger.info("Seeded Facebook token to DB (expires: %s)", expires_at)
            except Exception:
                logger.exception("Failed to seed Facebook token")

    # Instagram — use dedicated token if available, otherwise copy from Facebook
    ig_token = settings.instagram_access_token
    existing_ig = await get_active_token("instagram")
    if not existing_ig:
        if ig_token and not is_placeholder(ig_token):
            expires_at = datetime.now(timezone.utc) + timedelta(days=60)
            await _save_token("instagram", ig_token, expires_at)
            logger.info("Seeded Instagram token to DB (expires: %s)", expires_at)
        else:
            fb_db_token = await get_active_token("facebook")
            if fb_db_token:
                async with async_session() as session:
                    result = await session.execute(
                        select(TokenStore).where(TokenStore.platform == "facebook")
                    )
                    fb_row = result.scalar_one_or_none()
                    fb_expires = fb_row.expires_at if fb_row else None
                await _save_token("instagram", fb_db_token, fb_expires)
                logger.info("Seeded Instagram token from Facebook token")
