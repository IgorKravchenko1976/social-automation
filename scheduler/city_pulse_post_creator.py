"""City Pulse — auto-publish imported cultural events to TG/FB/IG.

Polls imin-backend's /v1/api/city-pulse/next-city-event-for-post every
5 minutes and creates a Post + Publication rows for the next available
Ukrainian event (country_code=UA, any city). publisher.py's dedicated
publish_city_pulse_queue() picks them up every 15 min and dispatches
to Telegram, Facebook, Instagram.

One event = one post (one-shot, deduplicated by posted_to_social_at on
the backend). Publication is in Ukrainian primarily; translations.en is
embedded as a second paragraph for FB/IG which have international
audiences.

Editorial gates baked into the backend SELECT:
  - is_pending_review = false (skip events from new sources awaiting approval)
  - posted_to_social_at IS NULL (one-shot per event)
  - starts_at IS NULL OR > now() (skip past events)

Bot-side gates here:
  - Skip if title or description is too short
  - Skip if the only language available is non-Ukrainian (leave to fixer)

The 5-minute interval gives natural pacing (1 event per cycle). A daily
cap of MAX_CITY_PULSE_POSTS_PER_DAY prevents over-posting.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any, Optional

import httpx

from sqlalchemy import select

from config.platforms import configured_platforms
from config.settings import settings
from db.database import async_session
from db.models import Post, Publication

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
ALL_PLATFORMS = configured_platforms()

MAX_CITY_PULSE_POSTS_PER_DAY = 16

# Backend base + sync key are reused from settings — same auth as other
# /research/* and /city-pulse/* endpoints in the bot.
REQUEST_TIMEOUT = 30


def _backend_configured() -> bool:
    return bool(settings.imin_backend_api_base and settings.imin_backend_sync_key)


async def _count_city_pulse_posts_today() -> int:
    """Count city_pulse posts created today (UTC)."""
    from datetime import date, datetime, timezone

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    async with async_session() as session:
        from sqlalchemy import func
        result = await session.execute(
            select(func.count(Post.id)).where(
                Post.source == "city_pulse",
                Post.created_at >= today_start,
            )
        )
        return result.scalar() or 0


def _backend_headers() -> dict[str, str]:
    return {"X-Sync-Key": settings.imin_backend_sync_key}


def _backend_base() -> str:
    return settings.imin_backend_api_base.rstrip("/")


# ── Backend client ──────────────────────────────────────────────

async def _fetch_next_city_event(country_code: str = "UA") -> Optional[dict]:
    """GET /v1/api/city-pulse/next-city-event-for-post (UA only for socials)."""
    if not _backend_configured():
        return None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_backend_base()}/v1/api/city-pulse/next-city-event-for-post",
            headers=_backend_headers(),
            params={"country_code": country_code},
        )
        resp.raise_for_status()
        data = resp.json()
    if data.get("empty"):
        return None
    return data


async def _mark_city_event_posted(
    city_event_id: int,
    *,
    social_post_id: Optional[int] = None,
    blog_html_path: str = "",
    failed: bool = False,
    error: str = "",
) -> dict:
    """POST /v1/api/city-pulse/mark-city-event-posted."""
    if not _backend_configured():
        return {}
    payload: dict[str, Any] = {"cityEventId": city_event_id}
    if social_post_id:
        payload["socialPostId"] = social_post_id
    if blog_html_path:
        payload["blogHtmlPath"] = blog_html_path
    if failed:
        payload["failed"] = True
        payload["error"] = error[:200]
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_backend_base()}/v1/api/city-pulse/mark-city-event-posted",
            headers=_backend_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _quality_gate(event: dict, city_event_id: int) -> str:
    """Return rejection reason or empty string if event passes all gates."""
    thumb = (event.get("thumbnailUrl") or "").strip()
    if not thumb or not thumb.startswith("http"):
        return "no_thumbnail_url"

    if not event.get("startsAt"):
        return "no_date"

    venue = (event.get("venueName") or "").strip()
    if not venue:
        return "no_venue"

    translations = event.get("translations") or {}
    if isinstance(translations, str):
        try:
            translations = _json.loads(translations)
        except Exception:
            translations = {}
    uk = translations.get("uk") if isinstance(translations, dict) else None
    if not isinstance(uk, dict) or not uk.get("title"):
        return "no_uk_translation"

    title = uk.get("title", "").strip()
    if len(title) < 5:
        return "title_too_short"

    return ""


def _is_precise_location(lat, lon) -> bool:
    """Return True only if coordinates look like a specific venue, not city center.

    City centers from GPT typically have 2 decimal places (e.g. 50.45, 30.52).
    Venue-level coordinates have 4+ decimal places (e.g. 50.4501, 30.5234).
    """
    if not lat or not lon:
        return False
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if lat_f == 0.0 or lon_f == 0.0:
        return False
    lat_str = f"{lat_f:.6f}".rstrip("0")
    lon_str = f"{lon_f:.6f}".rstrip("0")
    lat_decimals = len(lat_str.split(".")[1]) if "." in lat_str else 0
    lon_decimals = len(lon_str.split(".")[1]) if "." in lon_str else 0
    return lat_decimals >= 4 or lon_decimals >= 4


async def _already_queued(city_event_id: int) -> bool:
    """Check if a Post for this city_event_id already exists in local DB."""
    async with async_session() as session:
        result = await session.execute(
            select(Post.id).where(
                Post.source == "city_pulse",
                Post.poi_point_id == city_event_id,
            ).limit(1)
        )
        return result.scalar() is not None


# ── Post text builder ──────────────────────────────────────────

CATEGORY_EMOJI = {
    "cinema": "🎬",
    "theater": "🎭",
    "concert": "🎵",
    "exhibition": "🎨",
    "sale": "🏷️",
    "festival": "🎉",
    "workshop": "🛠️",
    "tour": "🧭",
}


def _format_city_event_for_post(event: dict) -> tuple[str, str]:
    """Build (title, content_raw) for the Post row.

    Uses translations.uk if available, otherwise the source-language fields.
    Format follows the existing POI post pattern — facts only, no fluff.
    Publisher.py later runs AI on this text to adapt per platform.

    Returns (title_short, content_full).
    """
    translations = event.get("translations") or {}
    if isinstance(translations, str):
        try:
            translations = _json.loads(translations)
        except Exception:
            translations = {}

    uk = translations.get("uk") if isinstance(translations, dict) else None
    if isinstance(uk, dict) and uk.get("title"):
        title = uk.get("title", "").strip()
        description = (uk.get("description") or "").strip()
    else:
        # Fallback to source-language fields (often English from Perplexity).
        title = (event.get("title") or "").strip()
        description = (event.get("description") or "").strip()

    category = (event.get("category") or "").lower()
    emoji = CATEGORY_EMOJI.get(category, "📍")
    venue = (event.get("venueName") or "").strip()
    address = (event.get("venueAddress") or "").strip()
    starts_at = event.get("startsAt") or ""

    # Prices block (only when at least one bound is present).
    price_line = ""
    pf = event.get("priceFrom")
    pt = event.get("priceTo")
    cur = event.get("currency") or ""
    if pf is not None and pt is not None and pf != pt:
        price_line = f"💳 {pf}–{pt} {cur}".strip()
    elif pf is not None or pt is not None:
        v = pf if pf is not None else pt
        price_line = f"💳 від {v} {cur}".strip()

    ticket_url = (event.get("ticketUrl") or "").strip()
    city_event_id = event.get("id")
    app_link = f"https://app.im-in.net/pulse/{city_event_id}" if city_event_id else ""

    parts: list[str] = []
    parts.append(f"{emoji} {title}")
    if description:
        parts.append(description)

    venue_line = ""
    if venue and address:
        venue_line = f"📍 {venue}, {address}"
    elif venue:
        venue_line = f"📍 {venue}"
    if venue_line:
        parts.append(venue_line)

    if starts_at:
        parts.append(f"🕒 {starts_at[:16].replace('T', ' ')}")

    if price_line:
        parts.append(price_line)

    if app_link:
        parts.append(f"📲 Деталі в I'M IN: {app_link}")

    source_name = (event.get("sourceName") or "").strip()
    parts.append(f"\n📋 Дані: IM-IN Pulse{' / ' + source_name if source_name else ''}")
    city_name = (event.get("city") or "").strip()
    parts.append(f"#Афіша{' #' + city_name if city_name else ''}")

    content = "\n\n".join(p for p in parts if p)
    return title[:200], content


# ── Main entry point ──────────────────────────────────────────

async def process_city_pulse_post() -> bool:
    """One cycle: pull → make Post → mark posted on backend.

    Returns True if a post was created, False otherwise. Designed to run
    every 5 minutes via APScheduler. Existing publisher.py loop processes
    the resulting Publication rows on its own cadence.
    """
    async with _lock:
        if not _backend_configured():
            logger.debug("[city-pulse-post] backend not configured")
            return False

        today_count = await _count_city_pulse_posts_today()
        if today_count >= MAX_CITY_PULSE_POSTS_PER_DAY:
            logger.debug("[city-pulse-post] daily limit reached (%d/%d)", today_count, MAX_CITY_PULSE_POSTS_PER_DAY)
            return False

        try:
            event = await _fetch_next_city_event()
        except Exception as exc:
            logger.warning("[city-pulse-post] fetch failed: %s", exc)
            return False

        if not event:
            logger.debug("[city-pulse-post] no events queued")
            return False

        city_event_id = event.get("id")
        if not city_event_id:
            logger.warning("[city-pulse-post] event missing id: %s", event)
            return False

        if await _already_queued(city_event_id):
            logger.debug("[city-pulse-post] event %d already has a local Post, skipping", city_event_id)
            try:
                await _mark_city_event_posted(city_event_id, failed=True, error="already_queued_locally")
            except Exception:
                pass
            return False

        reject = _quality_gate(event, city_event_id)
        if reject:
            logger.info("[city-pulse-post] event %d rejected: %s", city_event_id, reject)
            try:
                await _mark_city_event_posted(city_event_id, failed=True, error=reject)
            except Exception:
                pass
            return False

        image_path: Optional[str] = None
        thumb_url = (event.get("thumbnailUrl") or "").strip()
        if thumb_url and thumb_url.startswith("http"):
            try:
                from content.media import download_image_from_url
                image_path = await download_image_from_url(thumb_url)
            except Exception as exc:
                logger.warning(
                    "[city-pulse-post] thumbnail download failed for event %d: %s",
                    city_event_id, exc,
                )

        if not image_path:
            logger.info("[city-pulse-post] event %d rejected: no_photo", city_event_id)
            try:
                await _mark_city_event_posted(city_event_id, failed=True, error="no_photo")
            except Exception:
                pass
            return False

        title, content = _format_city_event_for_post(event)

        post_id: Optional[int] = None
        try:
            async with async_session() as session:
                lat = event.get("latitude")
                lon = event.get("longitude")
                if not _is_precise_location(lat, lon):
                    lat = None
                    lon = None

                post = Post(
                    title=title,
                    content_raw=content,
                    source="city_pulse",
                    source_url=event.get("sourceHomepageUrl") or "",
                    ticket_url=event.get("ticketUrl") or "",
                    image_path=image_path,
                    latitude=lat,
                    longitude=lon,
                    place_name=(event.get("venueName") or "")[:500],
                    poi_point_id=city_event_id,
                )
                post.log_pipeline(
                    "topic", "ok",
                    f"city event #{city_event_id}: {title[:80]} ({event.get('city', '')})",
                )

                session.add(post)
                await session.flush()

                for platform in ALL_PLATFORMS:
                    session.add(Publication(post_id=post.id, platform=platform.value))

                # Translations come pre-baked from city_events.translations
                translations = event.get("translations") or {}
                if isinstance(translations, str):
                    try:
                        translations = _json.loads(translations)
                    except Exception:
                        translations = {}
                if translations:
                    post.translations = _json.dumps(translations, ensure_ascii=False)

                await session.commit()
                post_id = post.id

            logger.info(
                "[city-pulse-post] event %d → post %d (title=%s)",
                city_event_id, post_id, title[:60],
            )

        except Exception as exc:
            logger.exception(
                "[city-pulse-post] failed to create post for event %d: %s",
                city_event_id, exc,
            )
            try:
                await _mark_city_event_posted(
                    city_event_id, failed=True, error=str(exc)[:200])
            except Exception:
                pass
            return False

        return True
