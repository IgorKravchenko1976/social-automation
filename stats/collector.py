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

    # Telegram views: Bot API only provides views at the time of channel_post update.
    # No reliable way to refresh later. We store whatever the initial update gave us.
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.coalesce(sa_func.sum(Message.view_count), 0)).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        stats["views"] = result.scalar() or 0
        logger.info("Telegram views from DB (sum of view_count): %d", stats["views"])

    pos, neg = await _collect_reactions(Platform.TELEGRAM.value, date_str)
    stats["likes"] = pos
    stats["dislikes"] = neg

    return stats


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
    """Collect Facebook views using multiple fallback strategies."""
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

    logger.info("Facebook: %d published posts today, collecting views...", len(post_ids))

    # Strategy 1: per-post insights (post_impressions)
    for post_id in post_ids:
        try:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{post_id}/insights",
                params={"metric": "post_impressions", "access_token": token},
            )
            data = resp.json()
            if "data" in data and data["data"]:
                values = data["data"][0].get("values", [])
                if values:
                    views = values[-1].get("value", 0)
                    total_views += views
                    logger.info("Facebook post %s insights: %d impressions", post_id, views)
                    continue
            if "error" in data:
                logger.warning("Facebook post insights error for %s: %s",
                               post_id, data["error"].get("message", "unknown"))
        except Exception:
            logger.warning("Facebook insights request failed for %s", post_id, exc_info=True)

    if total_views > 0:
        logger.info("Facebook views (post_impressions): %d", total_views)
        return total_views

    # Strategy 2: per-post engagement (reactions + comments + shares)
    engagement_total = 0
    for post_id in post_ids:
        try:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{post_id}",
                params={
                    "fields": "shares,reactions.summary(total_count),comments.summary(total_count)",
                    "access_token": token,
                },
            )
            data = resp.json()
            if "error" not in data:
                reactions = data.get("reactions", {}).get("summary", {}).get("total_count", 0)
                comments = data.get("comments", {}).get("summary", {}).get("total_count", 0)
                shares = data.get("shares", {}).get("count", 0)
                eng = reactions + comments + shares
                engagement_total += eng
                logger.info("Facebook post %s engagement: reactions=%d comments=%d shares=%d",
                            post_id, reactions, comments, shares)
            else:
                logger.warning("Facebook post engagement error for %s: %s",
                               post_id, data["error"].get("message", ""))
        except Exception:
            logger.debug("Facebook engagement request failed for %s", post_id)

    if engagement_total > 0:
        logger.info("Facebook views (engagement fallback): %d", engagement_total)
        return engagement_total

    # Strategy 3: page-level views
    try:
        from datetime import datetime as _dt, timedelta
        dt_date = _dt.strptime(date_str, "%Y-%m-%d")
        since_ts = int(dt_date.timestamp())
        until_ts = int((dt_date + timedelta(days=1)).timestamp())
        resp = await client.get(
            f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}/insights",
            params={
                "metric": "page_views_total",
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
                logger.info("Facebook page_views_total: %d", total_views)
        elif "error" in data:
            logger.warning("Facebook page insights error: %s", data["error"].get("message", ""))
    except Exception:
        logger.warning("Facebook page insights failed", exc_info=True)

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
    """Collect Instagram views using multiple fallback strategies."""
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

    logger.info("Instagram: %d published media today, collecting views...", len(post_ids))

    # Strategy 1: per-media insights (impressions)
    for media_id in post_ids:
        try:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{media_id}/insights",
                params={"metric": "impressions,reach", "access_token": token},
            )
            data = resp.json()
            if "data" in data and data["data"]:
                for metric in data["data"]:
                    if metric.get("name") == "impressions":
                        values = metric.get("values", [])
                        if values:
                            val = values[0].get("value", 0)
                            total_views += val
                            logger.info("Instagram media %s impressions: %d", media_id, val)
                        break
            elif "error" in data:
                logger.warning("Instagram insights error for %s: %s",
                               media_id, data["error"].get("message", ""))
        except Exception:
            logger.warning("Instagram insights request failed for %s", media_id, exc_info=True)

    if total_views > 0:
        logger.info("Instagram views (insights): %d", total_views)
        return total_views

    # Strategy 2: per-media engagement (like_count + comments_count)
    engagement_total = 0
    for media_id in post_ids:
        try:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{media_id}",
                params={"fields": "like_count,comments_count", "access_token": token},
            )
            data = resp.json()
            if "error" not in data:
                likes = data.get("like_count", 0)
                comments = data.get("comments_count", 0)
                engagement_total += likes + comments
                logger.info("Instagram media %s engagement: likes=%d comments=%d",
                            media_id, likes, comments)
            else:
                logger.warning("Instagram media engagement error for %s: %s",
                               media_id, data["error"].get("message", ""))
        except Exception:
            logger.debug("Instagram engagement failed for %s", media_id)

    if engagement_total > 0:
        logger.info("Instagram views (engagement fallback): %d", engagement_total)
    return engagement_total


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
