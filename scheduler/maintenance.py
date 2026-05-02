"""Post lifecycle maintenance: missed slot catch-up, expiration, retries."""
from __future__ import annotations

import logging

from sqlalchemy import select, func as sa_func

from config.settings import settings, get_today_start_utc, get_now_local, parse_slot_time
from db.database import async_session
from db.models import Post, Publication, PostStatus

from scheduler.publisher import publish_scheduled_post, count_published_today, MAX_RETRIES
from scheduler.post_creator import ALL_PLATFORMS

logger = logging.getLogger(__name__)


async def ensure_daily_posts_exist() -> None:
    """Log today's post status. Posts are created fresh before each slot."""
    today_start_utc = get_today_start_utc()

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(Post.id)).where(Post.created_at >= today_start_utc)
        )
        count = result.scalar() or 0

    expected = len(settings.post_schedule)
    logger.info(
        "Posts today: %d created, %d total slots scheduled — each slot creates fresh content on demand",
        count, expected,
    )


async def publish_missed_slots() -> None:
    """Publish posts for time slots that were missed (e.g. after a restart)."""
    now_local = get_now_local()

    past_slots: list[int] = []
    for idx, time_str in enumerate(settings.post_schedule):
        slot_time = parse_slot_time(time_str, now_local)
        if now_local > slot_time:
            past_slots.append(idx)

    if not past_slots:
        logger.info("=== CATCHUP === No past slots yet today")
        return

    published_today = await count_published_today()
    missed = len(past_slots) - published_today

    if missed <= 0:
        logger.info("=== CATCHUP === No missed slots (published=%d, past_slots=%d)",
                     published_today, len(past_slots))
        return

    logger.info("=== CATCHUP === %d missed slot(s) detected, publishing now", missed)
    for slot_idx in past_slots:
        try:
            await publish_scheduled_post(slot_idx)
        except Exception:
            logger.exception("Error publishing missed slot %d", slot_idx)


async def expire_inactive_platform_publications() -> None:
    """Mark queued/retrying publications for unconfigured platforms as failed."""
    active = {p.value for p in ALL_PLATFORMS}
    async with async_session() as session:
        result = await session.execute(
            select(Publication).where(
                Publication.status.in_([PostStatus.QUEUED, PostStatus.PUBLISHING]),
            )
        )
        pubs = result.scalars().all()
        expired = 0
        for pub in pubs:
            if pub.platform not in active:
                pub.status = PostStatus.FAILED
                pub.error_message = f"Platform '{pub.platform}' is not active (no credentials)"
                expired += 1
        await session.commit()
        if expired:
            logger.info("Expired %d publications for inactive platforms", expired)


async def expire_old_queued_publications() -> None:
    """Mark queued publications from previous days as failed."""
    today_start_utc = get_today_start_utc()

    async with async_session() as session:
        result = await session.execute(
            select(Publication)
            .join(Post)
            .where(
                Publication.status == PostStatus.QUEUED,
                Post.created_at < today_start_utc,
            )
        )
        old_pubs = result.scalars().all()

        for pub in old_pubs:
            pub.status = PostStatus.FAILED
            pub.error_message = pub.error_message or "Expired: not published on scheduled day"

        await session.commit()
        if old_pubs:
            logger.info("Expired %d old queued publications from previous days", len(old_pubs))


# Substrings in error_message that mark a permanent failure. We must NOT
# requeue these — the retry would just hit the same wall (fact-check rejects
# the same content, the post is intentionally marked stale, the platform
# returned a permission/object-not-found error, etc.) and pin the publisher
# in an infinite loop, blocking every other post in the queue.
_PERMANENT_FAILURE_MARKERS = (
    "fact-check rejected",
    "fact_check rejected",
    "fact-checked rejected",
    "manual unblock",
    "stale generic poi",
    "permanent error",
    "object with id",
    "not active (no credentials)",
    # Instagram needs an image; if the source post has no real photo we
    # disable Pexels/DALL-E fallback for poi/city_pulse — retrying just
    # marks it FAILED again every cycle and burns slots on stale posts.
    "no real photo",
    "instagram requires image",
    # Backend reported the source event was already published / archived /
    # deduplicated. Re-trying the same post would just re-fail with the
    # same answer.
    "expired: not published on scheduled day",
    "already_posted",
    "duplicate event",
)


def _is_permanent_failure(error_message: str | None) -> bool:
    if not error_message:
        return False
    msg = error_message.lower()
    return any(marker in msg for marker in _PERMANENT_FAILURE_MARKERS)


async def retry_failed_publications() -> None:
    """Retry publications that failed transiently (network/API hiccup).

    Skips publications whose ``error_message`` indicates a permanent failure
    (fact-check rejection, deleted platform object, etc.) — those would just
    fail again and clog the publisher with the same item every hour.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Publication)
            .where(
                Publication.status == PostStatus.FAILED,
                Publication.retry_count < MAX_RETRIES,
            )
        )
        pubs = result.scalars().all()

        retried = 0
        skipped = 0
        for pub in pubs:
            if _is_permanent_failure(pub.error_message):
                skipped += 1
                continue
            pub.status = PostStatus.QUEUED
            pub.error_message = None
            retried += 1

        await session.commit()
        if retried:
            logger.info("Reset %d failed publications for retry", retried)
        if skipped:
            logger.info(
                "Kept %d publications FAILED (permanent reason — won't retry)",
                skipped,
            )
