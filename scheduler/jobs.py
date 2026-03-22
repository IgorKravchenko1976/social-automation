from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform, configured_platforms, get_platform_instance
from config.settings import settings, get_today_start_utc, get_now_local, parse_slot_time
from content.generator import generate_post_text
from content.product_knowledge import FEATURE_TOPICS
from content.tourism_topics import TOURISM_RSS_FEEDS, ACTIVE_SPORTS_PLACES, LEISURE_TRAVEL_PLACES, BANNED_RSS_KEYWORDS
from content.media import get_image_for_post, create_slideshow_video, cleanup_media_file
from content.rss_parser import fetch_feed
from db.database import async_session
from db.models import Post, Publication, PostStatus, KVStore

logger = logging.getLogger(__name__)

ALL_PLATFORMS = configured_platforms()
logger.info("Active platforms: %s", [p.value for p in ALL_PLATFORMS])

MAX_RETRIES = 3


async def ensure_daily_posts_exist() -> None:
    """Create today's posts if insufficient for the schedule (called at startup)."""
    today_start_utc = get_today_start_utc()
    expected = len(settings.post_schedule)

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(Post.id)).where(Post.created_at >= today_start_utc)
        )
        count = result.scalar() or 0

    if count < expected:
        if count > 0:
            logger.info(
                "Found %d post(s) for today but need %d — marking old ones and creating fresh set",
                count, expected,
            )
            async with async_session() as session:
                old_pubs = await session.execute(
                    select(Publication)
                    .join(Post)
                    .where(
                        Post.created_at >= today_start_utc,
                        Publication.status == PostStatus.QUEUED,
                    )
                )
                for pub in old_pubs.scalars().all():
                    pub.status = PostStatus.FAILED
                    pub.error_message = "Replaced by new schedule"
                await session.commit()

        logger.info("Creating %d posts for today's schedule", expected)
        await create_daily_posts()
    else:
        logger.info("Found %d post(s) for today (need %d) — OK", count, expected)


async def publish_missed_slots() -> None:
    """Publish posts for time slots that were missed (e.g. after a restart).

    For each past time slot today, checks if the corresponding post still has
    queued publications, and publishes if so.
    """
    now_local = get_now_local()
    today_start_utc = get_today_start_utc()

    async with async_session() as session:
        today_posts_result = await session.execute(
            select(Post)
            .where(Post.created_at >= today_start_utc)
            .order_by(Post.created_at)
        )
        today_posts = today_posts_result.scalars().all()

    for idx, time_str in enumerate(settings.post_schedule):
        slot_time = parse_slot_time(time_str, now_local)

        if now_local <= slot_time:
            continue

        if idx >= len(today_posts):
            continue

        post = today_posts[idx]
        async with async_session() as session:
            result = await session.execute(
                select(sa_func.count(Publication.id))
                .where(
                    Publication.post_id == post.id,
                    Publication.status == PostStatus.QUEUED,
                )
            )
            queued = result.scalar() or 0

        if queued > 0:
            logger.info(
                "Missed slot %d (%s) post_id=%d — publishing now (%d queued pubs)",
                idx, time_str, post.id, queued,
            )
            try:
                await publish_scheduled_post(idx)
            except Exception:
                logger.exception("Error publishing missed slot %d", idx)


async def _next_from_pool(session, pool: list[str], index_key: str) -> str:
    """Pick the next item from a topic pool, cycling forever. Index persisted in DB."""
    result = await session.execute(
        select(KVStore).where(KVStore.key == index_key)
    )
    row = result.scalar_one_or_none()
    idx = int(row.value) if row else 0
    topic = pool[idx % len(pool)]
    new_idx = idx + 1

    if row:
        row.value = str(new_idx)
    else:
        session.add(KVStore(key=index_key, value=str(new_idx)))
    await session.flush()

    return topic


def _is_banned(title: str, summary: str) -> bool:
    """Check if an RSS entry contains banned political/military keywords."""
    text = (title + " " + summary).lower()
    return any(kw in text for kw in BANNED_RSS_KEYWORDS)


async def _fetch_tourism_news(session, count: int = 2) -> list[dict]:
    """Fetch fresh tourism news from RSS feeds, returning up to *count* new entries."""
    existing_urls_result = await session.execute(
        select(Post.source_url).where(Post.source == "rss")
    )
    existing_urls = {row[0] for row in existing_urls_result.all() if row[0]}

    all_entries: list[dict] = []
    feeds = list(TOURISM_RSS_FEEDS)
    random.shuffle(feeds)

    for name, url in feeds:
        if len(all_entries) >= count:
            break
        try:
            entries = await fetch_feed(url)
            for entry in entries:
                if not entry["link"] or entry["link"] in existing_urls:
                    continue
                if _is_banned(entry.get("title", ""), entry.get("summary", "")):
                    logger.info("RSS filtered (banned keywords): %s", entry.get("title", "")[:80])
                    continue
                entry["source_name"] = name
                all_entries.append(entry)
                existing_urls.add(entry["link"])
                if len(all_entries) >= count:
                    break
        except Exception:
            logger.warning("Failed to fetch RSS feed: %s", name)

    return all_entries[:count]


async def create_daily_posts() -> None:
    """Generate 5 posts for today:
    - 2 tourism news (from RSS feeds, with source links)
    - 1 app feature
    - 1 active sports/recreation place
    - 1 leisure travel place
    Order: news, active, news, feature, leisure
    """
    logger.info("=== CREATE POSTS === Starting daily post creation for %d platforms: %s",
                len(ALL_PLATFORMS), [p.value for p in ALL_PLATFORMS])
    if not ALL_PLATFORMS:
        logger.error("=== CREATE POSTS === NO platforms configured! Check API keys in .env")
        return

    async with async_session() as session:
        created_posts: list[tuple[Post, str]] = []

        # --- 2 Tourism news from RSS ---
        news_entries = await _fetch_tourism_news(session, count=2)
        logger.info("=== CREATE POSTS === RSS entries found: %d", len(news_entries))
        for entry in news_entries:
            source_name = entry.get("source_name", "")
            content = (
                f"{entry['title']}\n\n"
                f"{entry.get('summary', '')}\n\n"
                f"Джерело: {source_name}\n{entry['link']}"
            )
            post = Post(
                title=entry["title"][:200],
                content_raw=content,
                source="rss",
                source_url=entry["link"],
            )
            session.add(post)
            await session.flush()
            for platform in ALL_PLATFORMS:
                session.add(Publication(post_id=post.id, platform=platform.value))
            created_posts.append((post, "tourism_news"))

        while len([p for p in created_posts if p[1] in ("tourism_news", "leisure_travel")]) < 2:
            leisure_topic = await _next_from_pool(session, LEISURE_TRAVEL_PLACES, "pool_leisure_fill")
            post = Post(title=leisure_topic[:200], content_raw=leisure_topic, source="ai")
            session.add(post)
            await session.flush()
            for platform in ALL_PLATFORMS:
                session.add(Publication(post_id=post.id, platform=platform.value))
            created_posts.append((post, "leisure_travel"))

        # --- 1 Active sports/recreation place ---
        active_topic = await _next_from_pool(session, ACTIVE_SPORTS_PLACES, "pool_active")
        post = Post(title=active_topic[:200], content_raw=active_topic, source="ai")
        session.add(post)
        await session.flush()
        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))
        created_posts.append((post, "active_travel"))

        # --- 1 App feature ---
        feature_topic = await _next_from_pool(session, FEATURE_TOPICS, "pool_feature")
        post = Post(title=feature_topic[:200], content_raw=feature_topic, source="ai")
        session.add(post)
        await session.flush()
        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))
        created_posts.append((post, "feature"))

        # --- 1 Leisure travel place ---
        leisure_topic = await _next_from_pool(session, LEISURE_TRAVEL_PLACES, "pool_leisure")
        post = Post(title=leisure_topic[:200], content_raw=leisure_topic, source="ai")
        session.add(post)
        await session.flush()
        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))
        created_posts.append((post, "leisure_travel"))

        await session.commit()
        logger.info(
            "=== CREATE POSTS === Done: %d posts created [%s] for %d platform(s)",
            len(created_posts),
            ", ".join(ct for _, ct in created_posts),
            len(ALL_PLATFORMS),
        )
        if len(created_posts) < len(settings.post_schedule):
            logger.warning(
                "=== CREATE POSTS === Only %d/%d posts created — some slots may be empty!",
                len(created_posts), len(settings.post_schedule),
            )


async def publish_scheduled_post(time_slot: int) -> None:
    """Publish the post for a specific time slot (0-4).

    Only considers today's posts that still have QUEUED publications.
    Picks the Nth queued post (by creation time) for the given slot index.
    """
    today_start_utc = get_today_start_utc()

    async with async_session() as session:
        queued_posts_result = await session.execute(
            select(Post)
            .join(Publication)
            .where(
                Post.created_at >= today_start_utc,
                Publication.status == PostStatus.QUEUED,
            )
            .group_by(Post.id)
            .order_by(Post.created_at)
        )
        queued_posts = queued_posts_result.scalars().all()

        if not queued_posts:
            logger.warning("=== PUBLISH === No queued posts for slot %d — all may be published or none created", time_slot)
            return

        post = queued_posts[0]
        logger.info(
            "=== PUBLISH === Slot %d: post_id=%d '%s' (%d queued remaining)",
            time_slot, post.id, (post.title or "")[:50], len(queued_posts),
        )

        pubs_result = await session.execute(
            select(Publication)
            .where(Publication.post_id == post.id, Publication.status == PostStatus.QUEUED)
        )
        publications = pubs_result.scalars().all()

        image_path = post.image_path
        if not image_path:
            image_path = await get_image_for_post(post.content_raw[:100])
            if image_path:
                post.image_path = image_path
                await session.commit()

        for pub in publications:
            platform = Platform(pub.platform)
            await _publish_single(session, post, pub, platform, image_path)

        await session.commit()

        await _cleanup_post_media(session, post)


async def _cleanup_post_media(session: AsyncSession, post: Post) -> None:
    """Delete local media files once no queued publications remain for the post."""
    remaining = await session.execute(
        select(sa_func.count())
        .select_from(Publication)
        .where(Publication.post_id == post.id, Publication.status == PostStatus.QUEUED)
    )
    if remaining.scalar() > 0:
        return

    cleanup_media_file(post.image_path)
    cleanup_media_file(post.video_path)
    if post.image_path or post.video_path:
        post.image_path = None
        post.video_path = None
        await session.commit()
        logger.info("Media cleaned up for post_id=%d", post.id)


def _detect_content_type(post: Post) -> str:
    """Determine content type from post title/content for correct AI prompt."""
    if post.source == "rss":
        return "tourism_news"
    title = (post.title or "").lower()
    content = (post.content_raw or "").lower()
    text = title + " " + content

    active_keywords = [
        "f1", "formula", "tennis", "теніс", "marathon", "марафон",
        "surf", "серф", "ski", "лиж", "golf", "гольф", "dive", "дайв",
        "trek", "climb", "cycling", "вело", "sail", "вітрил", "race", "перегон",
        "camp nou", "wembley", "wimbledon", "silverstone", "monza",
        "le mans", "daytona", "nascar", "олімп", "olympic",
        "football", "футбол", "base camp", "tour du mont",
    ]
    for kw in active_keywords:
        if kw in text:
            return "active_travel"

    feature_keywords = [
        "i'm in", "додаток", "карта", "маркер", "3d", "авто-режим",
        "пакетне", "фільтр", "push", "діп-лінк", "гостьовий",
        "біометри", "приватн", "черга завантаж", "мов", "радіус",
        "коментар", "лайк", "профіл", "чат", "підпис",
    ]
    for kw in feature_keywords:
        if kw in text:
            return "feature"

    return "leisure_travel"


async def _publish_single(
    session: AsyncSession,
    post: Post,
    pub: Publication,
    platform: Platform,
    image_path: Optional[str],
) -> None:
    try:
        pub.status = PostStatus.PUBLISHING

        if not pub.content_adapted:
            content_type = _detect_content_type(post)
            if post.source == "rss":
                pub.content_adapted = await generate_post_text(
                    topic="", platform=platform,
                    source_text=post.content_raw,
                    content_type="tourism_news",
                )
            else:
                pub.content_adapted = await generate_post_text(
                    topic=post.content_raw, platform=platform,
                    content_type=content_type,
                )

        adapter = get_platform_instance(platform)

        if platform == Platform.TIKTOK:
            if post.video_path:
                result = await adapter.publish_video(pub.content_adapted, post.video_path)
            elif image_path:
                video_path = await create_slideshow_video(
                    [image_path], text_overlay=pub.content_adapted[:100]
                )
                if video_path:
                    result = await adapter.publish_video(pub.content_adapted, video_path)
                    cleanup_media_file(video_path)
                else:
                    result = await adapter.publish_text(pub.content_adapted)
            else:
                pub.status = PostStatus.FAILED
                pub.error_message = "TikTok requires video; no media available"
                return
        else:
            result = await adapter.publish_text(pub.content_adapted, image_path)

        if result.success:
            pub.status = PostStatus.PUBLISHED
            pub.platform_post_id = result.platform_post_id
            pub.published_at = datetime.now(timezone.utc)
            logger.info("=== PUBLISH === OK %s post_id=%d platform_post_id=%s",
                        platform.value, post.id, result.platform_post_id)
        else:
            pub.retry_count += 1
            if pub.retry_count >= MAX_RETRIES:
                pub.status = PostStatus.FAILED
                logger.error("=== PUBLISH === FINAL FAIL %s post_id=%d after %d retries: %s",
                             platform.value, post.id, pub.retry_count, result.error)
            else:
                pub.status = PostStatus.QUEUED
                logger.warning("=== PUBLISH === RETRY %s post_id=%d attempt=%d/%d: %s",
                               platform.value, post.id, pub.retry_count, MAX_RETRIES, result.error)
            pub.error_message = result.error

    except Exception as e:
        pub.retry_count += 1
        if pub.retry_count >= MAX_RETRIES:
            pub.status = PostStatus.FAILED
        else:
            pub.status = PostStatus.QUEUED
        pub.error_message = str(e)
        logger.exception("=== PUBLISH === EXCEPTION %s post_id=%d attempt=%d: %s",
                         platform.value, post.id, pub.retry_count, str(e)[:200])


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
    """Mark queued publications from previous days as failed so they don't block today's posts."""
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


async def retry_failed_publications() -> None:
    """Retry publications that failed but haven't exceeded max retries."""
    async with async_session() as session:
        result = await session.execute(
            select(Publication)
            .where(
                Publication.status == PostStatus.FAILED,
                Publication.retry_count < MAX_RETRIES,
            )
        )
        pubs = result.scalars().all()

        for pub in pubs:
            pub.status = PostStatus.QUEUED
            pub.error_message = None

        await session.commit()
        if pubs:
            logger.info("Reset %d failed publications for retry", len(pubs))
