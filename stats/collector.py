"""Collect daily metrics from each social-media platform."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from config.platforms import Platform
from config.settings import settings
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
    stats = dict(subscribers=0, posts=0, comments=0, views=0, likes=0, dislikes=0)

    client = await _http_client()

    # Subscribers
    try:
        resp = await client.post(_tg_url("getChatMemberCount"),
                                  json={"chat_id": settings.telegram_channel_id})
        data = resp.json()
        if data.get("ok"):
            stats["subscribers"] = data["result"]
    except Exception:
        logger.exception("Failed to get Telegram subscriber count")

    async with async_session() as session:
        # Posts = channel_post entries (includes manual posts from the channel)
        result = await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        posts_from_channel = result.scalar() or 0

        # Also count our own scheduler publications
        result = await session.execute(
            select(sa_func.count(Publication.id)).where(
                Publication.platform == Platform.TELEGRAM.value,
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
            )
        )
        posts_from_scheduler = result.scalar() or 0
        stats["posts"] = max(posts_from_channel, posts_from_scheduler)

        # Comments = discussion group messages (category="comment")
        result = await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.platform == Platform.TELEGRAM.value,
                Message.category == "comment",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        stats["comments"] = result.scalar() or 0

    # Emoji reactions (all emojis classified into positive/negative)
    pos, neg = await _collect_reactions(Platform.TELEGRAM.value, date_str)
    stats["likes"] = pos
    stats["dislikes"] = neg

    return stats


async def _collect_placeholder(platform: Platform, date_str: str) -> dict:
    """Placeholder for platforms without API credentials yet."""
    stats = dict(subscribers=0, posts=0, comments=0, views=0, likes=0, dislikes=0)

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
}


async def collect_all_stats() -> list[DailyStats]:
    """Collect today's stats for every platform and persist to DB."""
    tz = ZoneInfo(settings.timezone)
    date_str = datetime.now(tz).strftime("%Y-%m-%d")

    rows: list[DailyStats] = []

    for platform in Platform:
        collector = _COLLECTORS.get(platform)
        if collector:
            data = await collector(date_str)
        else:
            data = await _collect_placeholder(platform, date_str)

        row = DailyStats(
            date=date_str,
            platform=platform.value,
            **data,
        )
        rows.append(row)
        logger.info("Stats %s %s: subs=%d posts=%d views=%d",
                     date_str, platform.value, data["subscribers"], data["posts"], data["views"])

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
