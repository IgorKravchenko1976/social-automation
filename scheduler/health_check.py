"""Periodic health-check that audits the system and logs problems.

Runs every 30 minutes and writes a structured summary so that
the log file alone is enough to diagnose why posts are missing,
stats weren't collected, or messages went unanswered.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func as sa_func

from config.settings import settings, get_today_start_utc, get_now_local, parse_slot_time, utcnow_naive
from db.database import async_session
from db.models import (
    Post, Publication, PostStatus,
    Message, MessageDirection,
    DailyStats,
)

logger = logging.getLogger("health_check")

_SEP = "─" * 60


async def run_health_check() -> None:
    """Audit the whole system and log a structured report."""
    now = get_now_local()
    today_str = now.strftime("%Y-%m-%d")
    today_start_utc = get_today_start_utc()

    problems: list[str] = []

    logger.info(_SEP)
    logger.info("HEALTH CHECK  %s  %s", today_str, now.strftime("%H:%M:%S"))
    logger.info(_SEP)

    # ── 1. Posts ────────────────────────────────────────────────
    expected_posts = len(settings.post_schedule)
    async with async_session() as session:
        post_count = (await session.execute(
            select(sa_func.count(Post.id)).where(Post.created_at >= today_start_utc)
        )).scalar() or 0

    logger.info("[POSTS] Created today: %d / %d expected", post_count, expected_posts)
    if post_count == 0:
        problems.append(f"NO POSTS created today (expected {expected_posts})")
    elif post_count < expected_posts:
        problems.append(f"Only {post_count}/{expected_posts} posts created")

    # ── 2. Publications ─────────────────────────────────────────
    async with async_session() as session:
        pub_stats = {}
        for status in (PostStatus.PUBLISHED, PostStatus.FAILED, PostStatus.QUEUED):
            count = (await session.execute(
                select(sa_func.count(Publication.id))
                .join(Post)
                .where(Post.created_at >= today_start_utc, Publication.status == status)
            )).scalar() or 0
            pub_stats[status.value] = count

    published = pub_stats.get("published", 0)
    failed = pub_stats.get("failed", 0)
    queued = pub_stats.get("queued", 0)
    logger.info("[PUBS]  Published: %d | Failed: %d | Queued: %d", published, failed, queued)

    if failed > 0:
        problems.append(f"{failed} publications FAILED today")
        async with async_session() as session:
            result = await session.execute(
                select(Publication.platform, Publication.error_message)
                .join(Post)
                .where(Post.created_at >= today_start_utc, Publication.status == PostStatus.FAILED)
            )
            for platform, error in result.all():
                logger.warning("[PUBS]  FAIL %s: %s", platform, (error or "unknown")[:120])

    past_slots = sum(1 for t in settings.post_schedule if now > parse_slot_time(t, now))
    if published == 0 and past_slots > 0:
        problems.append(f"ZERO published posts but {past_slots} time slot(s) already passed")

    # ── 3. Missed time slots ────────────────────────────────────
    async with async_session() as session:
        today_posts = (await session.execute(
            select(Post).where(Post.created_at >= today_start_utc).order_by(Post.created_at)
        )).scalars().all()

    for idx, time_str in enumerate(settings.post_schedule):
        if now <= parse_slot_time(time_str, now):
            continue
        if idx >= len(today_posts):
            problems.append(f"Slot {idx} ({time_str}): no post exists for this slot")
            continue
        post = today_posts[idx]
        async with async_session() as session:
            still_queued = (await session.execute(
                select(sa_func.count(Publication.id)).where(
                    Publication.post_id == post.id, Publication.status == PostStatus.QUEUED,
                )
            )).scalar() or 0
        if still_queued > 0:
            problems.append(f"Slot {idx} ({time_str}): {still_queued} pubs still QUEUED (not published)")

    # ── 4. Unanswered messages ──────────────────────────────────
    async with async_session() as session:
        unanswered = (await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.direction == MessageDirection.INCOMING,
                Message.replied == False, Message.category != "spam",
            )
        )).scalar() or 0

        unanswered_old = (await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.direction == MessageDirection.INCOMING,
                Message.replied == False, Message.category != "spam",
                Message.created_at < utcnow_naive() - timedelta(hours=1),
            )
        )).scalar() or 0

    logger.info("[MSGS]  Unanswered: %d total (%d older than 1h)", unanswered, unanswered_old)
    if unanswered_old > 0:
        problems.append(f"{unanswered_old} messages unanswered for >1 hour")

    # ── 5. Statistics collection ────────────────────────────────
    async with async_session() as session:
        stats_count = (await session.execute(
            select(sa_func.count(DailyStats.id)).where(DailyStats.date == today_str)
        )).scalar() or 0

    logger.info("[STATS] Rows for today: %d", stats_count)
    if now.hour >= 20 and stats_count == 0:
        problems.append("No DailyStats collected today (report may be empty)")

    # ── 6. Token expiry + live page access check ──────────────
    try:
        from config.settings import ensure_utc
        from db.models import TokenStore
        async with async_session() as session:
            tokens = (await session.execute(select(TokenStore))).scalars().all()
        for tok in tokens:
            if tok.expires_at:
                days_left = (ensure_utc(tok.expires_at) - datetime.now(timezone.utc)).days
                if days_left <= 5:
                    problems.append(f"Token {tok.platform} expires in {days_left} days!")
                    logger.warning("[TOKEN] %s expires in %d days!", tok.platform, days_left)
                else:
                    logger.info("[TOKEN] %s OK (expires in %d days)", tok.platform, days_left)
    except Exception:
        logger.exception("[TOKEN] Failed to check tokens")

    # Live test: verify FB token can actually access the page (detects account blocks)
    try:
        import httpx
        from stats.token_renewer import get_active_token
        from config.platforms import FACEBOOK_GRAPH_API
        fb_token = await get_active_token("facebook") or settings.facebook_page_access_token
        if fb_token and settings.facebook_page_id:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}",
                    params={"access_token": fb_token, "fields": "id,name"},
                )
                live_data = r.json()
                if "error" in live_data:
                    err_msg = live_data["error"].get("message", "")[:120]
                    err_code = live_data["error"].get("code", 0)
                    problems.append(f"FB page access BLOCKED (code {err_code}): {err_msg}")
                    logger.warning("[TOKEN] FB live check FAILED (code %s): %s", err_code, err_msg)
                else:
                    logger.info("[TOKEN] FB live check OK — page '%s' accessible", live_data.get("name", "?"))
    except Exception:
        logger.exception("[TOKEN] FB live check failed")

    # ── Summary ─────────────────────────────────────────────────
    if problems:
        logger.warning(_SEP)
        logger.warning("PROBLEMS FOUND: %d", len(problems))
        for i, p in enumerate(problems, 1):
            logger.warning("  %d. %s", i, p)
        logger.warning(_SEP)
    else:
        logger.info("HEALTH CHECK OK — no problems detected")
        logger.info(_SEP)
