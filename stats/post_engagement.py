"""Per-post engagement collector — Phase 3 of priority-ml-system.

For each PUBLISHED Publication, snapshot views/likes/comments/shares at
fixed window ages (1h, 24h, 7d, 30d after `published_at`) so the ML
ranker (Phase 4) has supervised training labels.

Cron entry: `collect_post_engagement()` runs every 30 minutes from
main.py. The cron is idempotent (UNIQUE on (post_id, platform,
window_hours)) and re-running it within the same window is cheap —
later collections at the same checkpoint just refresh the row.

Why per-post and not per-day
- DailyStats (existing) aggregates by date — fine for human dashboards
  but useless for "which POI/event in the queue should win next".
- Per-post lets us join back to the Post.poi_point_id /
  Post.backend_event_id and feed the ML model exactly what got
  posted, with what content, and how each individual post performed.

Score formula
- views weighted with log10 (caps virality so a single 100k-view post
  doesn't drown out 10 healthy 5k-view posts).
- likes ×0.5, comments ×2 (comments are far harder to earn), shares ×5.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

from config.platforms import Platform, FACEBOOK_GRAPH_API, INSTAGRAM_GRAPH_API
from config.settings import settings
from db.database import async_session
from db.models import (
    Publication, PostStatus, PostEngagement, ReactionSnapshot, TokenStore,
)

from sqlalchemy import select, and_, func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)

# Checkpoints in hours. The cron picks each PUBLISHED publication and
# only collects a snapshot for windows that have just elapsed
# (published_at + window_hours <= now < published_at + 2 * window_hours).
# That keeps writes proportional to publishing volume, not to total
# history.
ENGAGEMENT_WINDOWS_HOURS = (1, 24, 168, 720)  # 1h, 24h, 7d, 30d
WINDOW_GRACE_HOURS = 2  # allow 2h slack so a missed cron tick still collects

_http: httpx.AsyncClient | None = None


async def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30)
    return _http


def compute_engagement_score(views: int, likes: int, comments: int, shares: int) -> float:
    """Normalised 0..∞ engagement score used as the ML training label.

    log10(views+1) * 10  → [0..50] for views in [0..100k].
    Each like = 0.5, comment = 2 (harder to earn), share = 5.
    """
    return (
        math.log10(max(views, 0) + 1) * 10
        + max(likes, 0) * 0.5
        + max(comments, 0) * 2
        + max(shares, 0) * 5
    )


async def _facebook_token() -> str | None:
    """Fetch latest Facebook token from TokenStore (or settings fallback)."""
    async with async_session() as session:
        row = (await session.execute(
            select(TokenStore).where(TokenStore.platform == Platform.FACEBOOK.value)
        )).scalar_one_or_none()
        if row and row.token:
            return row.token
    return getattr(settings, "facebook_access_token", None)


async def _instagram_token() -> str | None:
    async with async_session() as session:
        row = (await session.execute(
            select(TokenStore).where(TokenStore.platform == Platform.INSTAGRAM.value)
        )).scalar_one_or_none()
        if row and row.token:
            return row.token
    return getattr(settings, "facebook_access_token", None)  # IG uses FB token


async def _collect_facebook_post(platform_post_id: str, token: str) -> dict:
    """Fetch one Facebook post's engagement counters."""
    page_id = settings.facebook_page_id
    full_id = platform_post_id if "_" in platform_post_id else f"{page_id}_{platform_post_id}"
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    client = await _http_client()
    try:
        # Reach via insights (post_impressions_unique).
        resp = await client.get(
            f"{FACEBOOK_GRAPH_API}/{full_id}/insights",
            params={"metric": "post_impressions_unique", "access_token": token},
        )
        data = resp.json()
        if "data" in data and data["data"]:
            values = data["data"][0].get("values", [])
            if values:
                out["views"] = int(values[-1].get("value", 0) or 0)
    except Exception:
        logger.debug("FB insights failed for %s", full_id, exc_info=True)
    try:
        resp = await client.get(
            f"{FACEBOOK_GRAPH_API}/{full_id}",
            params={
                "fields": "reactions.summary(total_count),comments.summary(total_count),shares",
                "access_token": token,
            },
        )
        data = resp.json()
        if "error" not in data:
            out["likes"] = int(data.get("reactions", {}).get("summary", {}).get("total_count", 0) or 0)
            out["comments"] = int(data.get("comments", {}).get("summary", {}).get("total_count", 0) or 0)
            out["shares"] = int((data.get("shares") or {}).get("count", 0) or 0)
    except Exception:
        logger.debug("FB engagement failed for %s", full_id, exc_info=True)
    return out


async def _collect_instagram_post(platform_post_id: str, token: str) -> dict:
    """Fetch one Instagram media's engagement counters."""
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    client = await _http_client()
    try:
        resp = await client.get(
            f"{INSTAGRAM_GRAPH_API}/{platform_post_id}/insights",
            params={"metric": "impressions,reach", "access_token": token},
        )
        data = resp.json()
        if "data" in data:
            for entry in data["data"]:
                if entry.get("name") == "impressions":
                    vals = entry.get("values", [])
                    if vals:
                        out["views"] = int(vals[-1].get("value", 0) or 0)
                        break
            if not out["views"]:
                # impressions not available for newer media types — fall back to reach
                for entry in data["data"]:
                    if entry.get("name") == "reach":
                        vals = entry.get("values", [])
                        if vals:
                            out["views"] = int(vals[-1].get("value", 0) or 0)
    except Exception:
        logger.debug("IG insights failed for %s", platform_post_id, exc_info=True)
    try:
        resp = await client.get(
            f"{INSTAGRAM_GRAPH_API}/{platform_post_id}",
            params={"fields": "like_count,comments_count", "access_token": token},
        )
        data = resp.json()
        if "error" not in data:
            out["likes"] = int(data.get("like_count", 0) or 0)
            out["comments"] = int(data.get("comments_count", 0) or 0)
    except Exception:
        logger.debug("IG counts failed for %s", platform_post_id, exc_info=True)
    return out


async def _collect_telegram_post(platform_post_id: str) -> dict:
    """Fetch Telegram channel post views via Telethon GetMessagesViewsRequest.

    Reactions counted from reaction_snapshots (already populated by the
    chat handler). Comments / shares are not exposed by Telegram for
    channel posts so they stay 0.
    """
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    try:
        msg_id = int(platform_post_id)
    except (ValueError, TypeError):
        return out

    # Reactions — sum positive (likes-equivalent) and negative (dislikes).
    async with async_session() as session:
        pos = (await session.execute(
            select(sa_func.coalesce(sa_func.sum(ReactionSnapshot.total_count), 0)).where(
                ReactionSnapshot.platform == Platform.TELEGRAM.value,
                ReactionSnapshot.message_id == str(msg_id),
                ReactionSnapshot.category == "positive",
            )
        )).scalar_one() or 0
        out["likes"] = int(pos)

    # Views via Telethon — guarded import + StringSession.
    try:
        from telethon import TelegramClient, functions
        from telethon.sessions import StringSession
        session_str = getattr(settings, "telegram_session", None)
        api_id = int(getattr(settings, "telegram_api_id", 0) or 0)
        api_hash = getattr(settings, "telegram_api_hash", None)
        channel_id = getattr(settings, "telegram_channel_id", None)
        if not (session_str and api_id and api_hash and channel_id):
            return out
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return out
        channel = await client.get_entity(channel_id)
        result = await client(functions.messages.GetMessagesViewsRequest(
            peer=channel, id=[msg_id], increment=False,
        ))
        if result.views:
            out["views"] = int(result.views[0].views or 0)
        await client.disconnect()
    except Exception:
        logger.debug("Telethon view fetch failed for %s", platform_post_id, exc_info=True)
    return out


async def _due_publications(
    now: datetime,
) -> list[tuple[Publication, int, datetime]]:
    """Return PUBLISHED publications whose checkpoints are due to be sampled.

    For each window in ENGAGEMENT_WINDOWS_HOURS, finds publications where
    `published_at + window_hours <= now < published_at + window_hours +
    WINDOW_GRACE_HOURS`. The grace window lets a missed cron tick still
    catch up without re-collecting historical windows forever.
    """
    out: list[tuple[Publication, int, datetime]] = []
    async with async_session() as session:
        for window_h in ENGAGEMENT_WINDOWS_HOURS:
            window_lower = now - timedelta(hours=window_h + WINDOW_GRACE_HOURS)
            window_upper = now - timedelta(hours=window_h)
            rows = (await session.execute(
                select(Publication).where(
                    Publication.status == PostStatus.PUBLISHED,
                    Publication.platform_post_id.isnot(None),
                    Publication.published_at.isnot(None),
                    Publication.published_at <= window_upper,
                    Publication.published_at > window_lower,
                )
            )).scalars().all()
            for pub in rows:
                out.append((pub, window_h, pub.published_at))
    return out


async def _upsert_engagement(
    *,
    post_id: int,
    platform: str,
    window_hours: int,
    counts: dict,
) -> None:
    """Idempotent upsert keyed on (post_id, platform, window_hours)."""
    score = compute_engagement_score(**counts)
    payload = dict(
        post_id=post_id,
        platform=platform,
        window_hours=window_hours,
        views=counts["views"],
        likes=counts["likes"],
        comments=counts["comments"],
        shares=counts["shares"],
        score=score,
    )
    async with async_session() as session:
        is_sqlite = settings.database_url.startswith("sqlite")
        ins = (sqlite_insert if is_sqlite else pg_insert)(PostEngagement).values(**payload)
        update_cols = {
            "views": ins.excluded.views,
            "likes": ins.excluded.likes,
            "comments": ins.excluded.comments,
            "shares": ins.excluded.shares,
            "score": ins.excluded.score,
        }
        stmt = ins.on_conflict_do_update(
            index_elements=["post_id", "platform", "window_hours"],
            set_=update_cols,
        )
        await session.execute(stmt)
        await session.commit()


async def collect_post_engagement(now: datetime | None = None) -> int:
    """Cron entry — collect snapshots for every due (post, platform, window).

    Returns the number of (post, platform, window_hours) rows
    inserted / updated this run.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    due = await _due_publications(now)
    if not due:
        logger.debug("post_engagement: nothing due at %s", now.isoformat())
        return 0

    fb_token = await _facebook_token()
    ig_token = await _instagram_token()

    written = 0
    for pub, window_h, _published_at in due:
        try:
            if pub.platform == Platform.FACEBOOK.value:
                if not fb_token:
                    continue
                counts = await _collect_facebook_post(pub.platform_post_id, fb_token)
            elif pub.platform == Platform.INSTAGRAM.value:
                if not ig_token:
                    continue
                counts = await _collect_instagram_post(pub.platform_post_id, ig_token)
            elif pub.platform == Platform.TELEGRAM.value:
                counts = await _collect_telegram_post(pub.platform_post_id)
            else:
                continue
            await _upsert_engagement(
                post_id=pub.post_id,
                platform=pub.platform,
                window_hours=window_h,
                counts=counts,
            )
            written += 1
            logger.info(
                "post_engagement %s post=%s window=%dh views=%d likes=%d comments=%d shares=%d score=%.1f",
                pub.platform, pub.post_id, window_h,
                counts["views"], counts["likes"], counts["comments"], counts["shares"],
                compute_engagement_score(**counts),
            )
        except Exception:
            logger.warning(
                "post_engagement collection failed for pub=%s platform=%s window=%dh",
                pub.id, pub.platform, window_h, exc_info=True,
            )
    logger.info("post_engagement: collected %d snapshots at %s", written, now.isoformat())
    return written


async def backfill_post_engagement(
    *,
    days: int = 7,
    platforms: Iterable[str] | None = None,
) -> int:
    """Manual one-shot backfill — re-collect every checkpoint up to `days` old.

    Used by /admin/backfill-engagement endpoint after a fresh deploy or
    when the cron has been silent. Walks ALL publications regardless of
    elapsed time, so it ignores ENGAGEMENT_WINDOWS_HOURS gating.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)
    fb_token = await _facebook_token()
    ig_token = await _instagram_token()
    allowed_platforms = set(platforms) if platforms else None

    async with async_session() as session:
        rows = (await session.execute(
            select(Publication).where(
                Publication.status == PostStatus.PUBLISHED,
                Publication.platform_post_id.isnot(None),
                Publication.published_at.isnot(None),
                Publication.published_at >= cutoff,
            )
        )).scalars().all()

    written = 0
    for pub in rows:
        if allowed_platforms and pub.platform not in allowed_platforms:
            continue
        for window_h in ENGAGEMENT_WINDOWS_HOURS:
            elapsed_h = (now - pub.published_at).total_seconds() / 3600
            if elapsed_h < window_h:
                continue
            try:
                if pub.platform == Platform.FACEBOOK.value and fb_token:
                    counts = await _collect_facebook_post(pub.platform_post_id, fb_token)
                elif pub.platform == Platform.INSTAGRAM.value and ig_token:
                    counts = await _collect_instagram_post(pub.platform_post_id, ig_token)
                elif pub.platform == Platform.TELEGRAM.value:
                    counts = await _collect_telegram_post(pub.platform_post_id)
                else:
                    continue
                await _upsert_engagement(
                    post_id=pub.post_id,
                    platform=pub.platform,
                    window_hours=window_h,
                    counts=counts,
                )
                written += 1
            except Exception:
                logger.debug("backfill engagement failed pub=%s w=%d", pub.id, window_h, exc_info=True)
    logger.info("post_engagement backfill: wrote %d snapshots over last %d days", written, days)
    return written
