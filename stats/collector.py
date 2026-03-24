"""Collect daily metrics from each social-media platform."""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from config.platforms import Platform, FACEBOOK_GRAPH_API, INSTAGRAM_GRAPH_API, EMPTY_STATS
from config.settings import settings, get_now_local
from db.database import async_session
from db.models import DailyStats, Publication, PostStatus, Message, MessageDirection, ReactionSnapshot

from sqlalchemy import select, func as sa_func

logger = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None


def _tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


async def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30)
    return _http


async def _collect_reactions(platform: str, date_str: str) -> tuple[int, int]:
    """Aggregate positive and negative reaction counts for a given date."""
    async with async_session() as session:
        positive = await session.execute(
            select(sa_func.coalesce(sa_func.sum(ReactionSnapshot.total_count), 0)).where(
                ReactionSnapshot.platform == platform,
                ReactionSnapshot.category == "positive",
                ReactionSnapshot.message_date == date_str,
            )
        )
        negative = await session.execute(
            select(sa_func.coalesce(sa_func.sum(ReactionSnapshot.total_count), 0)).where(
                ReactionSnapshot.platform == platform,
                ReactionSnapshot.category == "negative",
                ReactionSnapshot.message_date == date_str,
            )
        )
        return positive.scalar() or 0, negative.scalar() or 0


async def _collect_telegram(date_str: str) -> dict:
    """Fetch Telegram channel stats via Bot API + DB."""
    stats = EMPTY_STATS.copy()

    client = await _http_client()

    try:
        resp = await client.post(_tg_url("getChatMemberCount"),
                                  json={"chat_id": settings.telegram_channel_id})
        data = resp.json()
        if data.get("ok"):
            stats["subscribers"] = data["result"]
    except Exception:
        logger.exception("Failed to get Telegram subscriber count")

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        posts_from_channel = result.scalar() or 0

        result = await session.execute(
            select(sa_func.count(Publication.id)).where(
                Publication.platform == Platform.TELEGRAM.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
            )
        )
        posts_from_scheduler = result.scalar() or 0
        stats["posts"] = max(posts_from_channel, posts_from_scheduler)

        result = await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "comment",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        stats["comments"] = result.scalar() or 0

    # Telegram views: aggregate from stored channel_post updates + per-post forwarding refresh
    await _refresh_telegram_views(date_str)
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.coalesce(sa_func.sum(Message.view_count), 0)).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        stats["views"] = result.scalar() or 0

    pos, neg = await _collect_reactions(Platform.TELEGRAM.value, date_str)
    stats["likes"] = pos
    stats["dislikes"] = neg

    return stats


async def _refresh_telegram_views(date_str: str) -> None:
    """Refresh view counts for channel posts by forwarding to discussion group.

    The Bot API only provides view counts at the time of the initial update.
    We forward each channel post to the linked discussion group, read the
    forwarded message (which inherits the channel views counter), then delete it.
    """
    if not settings.telegram_bot_token or not settings.telegram_channel_id:
        return

    client = await _http_client()

    try:
        resp = await client.post(_tg_url("getChat"),
                                  json={"chat_id": settings.telegram_channel_id})
        chat_data = resp.json()
        if not chat_data.get("ok"):
            return
        discussion_id = chat_data["result"].get("linked_chat_id")
        if not discussion_id:
            logger.debug("No discussion group linked to channel, skipping view refresh")
            return
    except Exception:
        logger.warning("Cannot get discussion group for view refresh", exc_info=True)
        return

    async with async_session() as session:
        result = await session.execute(
            select(Message).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        posts = result.scalars().all()

    refreshed = 0
    for post in posts:
        if not post.platform_message_id:
            continue
        try:
            fwd_resp = await client.post(_tg_url("forwardMessage"), json={
                "chat_id": discussion_id,
                "from_chat_id": settings.telegram_channel_id,
                "message_id": int(post.platform_message_id),
                "disable_notification": True,
            })
            fwd_data = fwd_resp.json()
            if not fwd_data.get("ok"):
                continue

            fwd_msg = fwd_data["result"]
            views = fwd_msg.get("views", 0) or 0

            await client.post(_tg_url("deleteMessage"), json={
                "chat_id": discussion_id,
                "message_id": fwd_msg["message_id"],
            })

            if views > (post.view_count or 0):
                async with async_session() as session:
                    msg = await session.get(Message, post.id)
                    if msg:
                        msg.view_count = views
                        await session.commit()
                        refreshed += 1
        except Exception:
            logger.debug("View refresh failed for post #%s", post.platform_message_id)

    if refreshed:
        logger.info("Refreshed Telegram views for %d posts on %s", refreshed, date_str)


async def _collect_facebook(date_str: str) -> dict:
    """Fetch Facebook Page stats via Graph API + DB."""
    stats = EMPTY_STATS.copy()

    if not settings.facebook_page_id or not settings.facebook_page_access_token:
        return stats

    from stats.token_renewer import get_active_token
    token = await get_active_token("facebook") or settings.facebook_page_access_token

    client = await _http_client()

    try:
        resp = await client.get(
            f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}",
            params={
                "fields": "followers_count,fan_count",
                "access_token": token,
            },
        )
        data = resp.json()
        if "error" not in data:
            stats["subscribers"] = data.get("followers_count", 0) or data.get("fan_count", 0)
        else:
            logger.warning("Facebook API error (subscribers): %s", data["error"].get("message"))
    except Exception:
        logger.exception("Failed to get Facebook subscriber count")

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(Publication.id)).where(
                Publication.platform == Platform.FACEBOOK.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
            )
        )
        stats["posts"] = result.scalar() or 0

    stats["views"] = await _collect_facebook_post_views(date_str, token)

    pos, neg = await _collect_reactions(Platform.FACEBOOK.value, date_str)
    stats["likes"] = pos
    stats["dislikes"] = neg

    return stats


async def _collect_facebook_post_views(date_str: str, token: str) -> int:
    """Sum impressions from individual Facebook posts published today."""
    total_views = 0
    client = await _http_client()

    async with async_session() as session:
        result = await session.execute(
            select(Publication.platform_post_id).where(
                Publication.platform == Platform.FACEBOOK.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
                Publication.platform_post_id.isnot(None),
            )
        )
        post_ids = [r[0] for r in result.all()]

    for post_id in post_ids:
        try:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{post_id}/insights",
                params={
                    "metric": "post_impressions",
                    "access_token": token,
                },
            )
            data = resp.json()
            if "data" in data and data["data"]:
                values = data["data"][0].get("values", [])
                if values:
                    views = values[-1].get("value", 0)
                    total_views += views
            elif "error" in data:
                err_msg = data["error"].get("message", "")
                if "Unsupported request" not in err_msg:
                    logger.warning("Facebook post insights error for %s: %s", post_id, err_msg)
        except Exception:
            logger.debug("Failed to get insights for FB post %s", post_id)

    if not post_ids:
        try:
            from datetime import datetime as _dt, timedelta
            dt_date = _dt.strptime(date_str, "%Y-%m-%d")
            since_ts = int(dt_date.timestamp())
            until_ts = int((dt_date + timedelta(days=1)).timestamp())
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}/insights",
                params={
                    "metric": "page_impressions_unique",
                    "period": "day",
                    "since": since_ts,
                    "until": until_ts,
                    "access_token": token,
                },
            )
            data = resp.json()
            if "data" in data and data["data"]:
                values = data["data"][0].get("values", [])
                if values:
                    total_views = values[-1].get("value", 0)
        except Exception:
            pass

    if total_views:
        logger.info("Facebook views for %s: %d (from %d posts)", date_str, total_views, len(post_ids))
    return total_views


async def _collect_instagram(date_str: str) -> dict:
    """Fetch Instagram stats via Graph API + DB."""
    stats = EMPTY_STATS.copy()

    if not settings.instagram_user_id or not settings.instagram_access_token:
        return stats

    from stats.token_renewer import get_active_token
    token = await get_active_token("instagram") or settings.instagram_access_token

    client = await _http_client()

    try:
        resp = await client.get(
            f"{FACEBOOK_GRAPH_API}/{settings.instagram_user_id}",
            params={
                "fields": "followers_count,media_count",
                "access_token": token,
            },
        )
        data = resp.json()
        if "error" not in data:
            stats["subscribers"] = data.get("followers_count", 0)
        else:
            logger.warning("Instagram API error (subscribers): %s", data["error"].get("message"))
            resp2 = await client.get(
                f"{INSTAGRAM_GRAPH_API}/{settings.instagram_user_id}",
                params={"fields": "followers_count,media_count", "access_token": token},
            )
            data2 = resp2.json()
            if "error" not in data2:
                stats["subscribers"] = data2.get("followers_count", 0)
    except Exception:
        logger.exception("Failed to get Instagram subscriber count")

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(Publication.id)).where(
                Publication.platform == Platform.INSTAGRAM.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
            )
        )
        stats["posts"] = result.scalar() or 0

    stats["views"] = await _collect_instagram_post_views(date_str, token)

    pos, neg = await _collect_reactions(Platform.INSTAGRAM.value, date_str)
    stats["likes"] = pos
    stats["dislikes"] = neg

    return stats


async def _collect_instagram_post_views(date_str: str, token: str) -> int:
    """Sum impressions from individual Instagram media published today."""
    total_views = 0
    client = await _http_client()

    async with async_session() as session:
        result = await session.execute(
            select(Publication.platform_post_id).where(
                Publication.platform == Platform.INSTAGRAM.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
                Publication.platform_post_id.isnot(None),
            )
        )
        post_ids = [r[0] for r in result.all()]

    for media_id in post_ids:
        try:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{media_id}/insights",
                params={
                    "metric": "impressions,reach",
                    "access_token": token,
                },
            )
            data = resp.json()
            if "data" in data:
                for metric in data["data"]:
                    if metric.get("name") == "impressions":
                        values = metric.get("values", [])
                        if values:
                            total_views += values[0].get("value", 0)
                        break
            elif "error" in data:
                err = data["error"].get("message", "")
                if "not available" not in err.lower():
                    logger.debug("Instagram media insights error for %s: %s", media_id, err)
        except Exception:
            logger.debug("Failed to get insights for IG media %s", media_id)

    if total_views:
        logger.info("Instagram views for %s: %d (from %d posts)", date_str, total_views, len(post_ids))
    return total_views


async def _collect_placeholder(platform: Platform, date_str: str) -> dict:
    """Placeholder for platforms without API credentials yet."""
    stats = EMPTY_STATS.copy()

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(Publication.id)).where(
                Publication.platform == platform.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
            )
        )
        stats["posts"] = result.scalar() or 0

    return stats


_COLLECTORS = {
    Platform.TELEGRAM: _collect_telegram,
    Platform.FACEBOOK: _collect_facebook,
    Platform.INSTAGRAM: _collect_instagram,
}


async def collect_all_stats() -> list[DailyStats]:
    """Collect today's stats for every platform and persist to DB."""
    date_str = get_now_local().strftime("%Y-%m-%d")

    logger.info("=== STATS === Collecting stats for %s", date_str)
    rows: list[DailyStats] = []
    errors: list[str] = []

    for platform in Platform:
        collector = _COLLECTORS.get(platform)
        try:
            if collector:
                data = await collector(date_str)
            else:
                data = await _collect_placeholder(platform, date_str)
        except Exception:
            logger.exception("=== STATS === FAILED to collect %s", platform.value)
            errors.append(platform.value)
            data = EMPTY_STATS.copy()

        row = DailyStats(
            date=date_str,
            platform=platform.value,
            **data,
        )
        rows.append(row)
        logger.info("=== STATS === %s %s: subs=%d posts=%d comments=%d views=%d likes=%d dislikes=%d",
                     date_str, platform.value,
                     data["subscribers"], data["posts"], data["comments"],
                     data["views"], data["likes"], data["dislikes"])

    if errors:
        logger.error("=== STATS === Collection ERRORS on: %s", ", ".join(errors))

    async with async_session() as session:
        for r in rows:
            existing = await session.execute(
                select(DailyStats).where(
                    DailyStats.date == r.date,
                    DailyStats.platform == r.platform,
                )
            )
            old = existing.scalar_one_or_none()
            if old:
                old.subscribers = r.subscribers
                old.posts = r.posts
                old.comments = r.comments
                old.views = r.views
                old.likes = r.likes
                old.dislikes = r.dislikes
            else:
                session.add(r)
        await session.commit()

    return rows
