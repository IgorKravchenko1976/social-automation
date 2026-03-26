from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform, PLATFORM_LIMITS, configured_platforms, get_platform_instance
from config.settings import settings, get_today_start_utc, get_now_local, parse_slot_time
from content.generator import (
    generate_post_text, generate_unique_topic,
    extract_location_coordinates, build_map_link,
    translate_post,
)
from content.fact_checker import fact_check_post, MAX_FACT_CHECK_RETRIES
from content.tourism_topics import (
    TOURISM_RSS_FEEDS, BANNED_RSS_KEYWORDS,
    ACTIVE_DIRECTIONS, LEISURE_DIRECTIONS, FEATURE_DIRECTIONS,
)
from content.media import get_image_for_post, create_slideshow_video, cleanup_media_file
from content.rss_parser import fetch_feed
from db.database import async_session
from db.models import Post, Publication, PostStatus

logger = logging.getLogger(__name__)

ALL_PLATFORMS = configured_platforms()
logger.info("Active platforms: %s", [p.value for p in ALL_PLATFORMS])

MAX_RETRIES = 3

_publish_lock = asyncio.Lock()


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
        "Posts today: %d created, %d slots remaining — each slot creates fresh content on demand",
        count, expected,
    )


async def publish_missed_slots() -> None:
    """Publish posts for time slots that were missed (e.g. after a restart).

    For each past time slot today, re-checks how many posts were already
    published to avoid duplicates (uses the same lock as scheduled publishing).
    """
    now_local = get_now_local()

    past_slots: list[int] = []
    for idx, time_str in enumerate(settings.post_schedule):
        slot_time = parse_slot_time(time_str, now_local)
        if now_local > slot_time:
            past_slots.append(idx)

    if not past_slots:
        logger.info("=== CATCHUP === No past slots yet today")
        return

    published_today = await _count_published_today()
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


async def _get_recent_titles(session: AsyncSession, days: int = 60) -> list[str]:
    """Fetch post titles from the last N days for uniqueness checks."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        select(Post.title)
        .where(Post.created_at >= cutoff, Post.title.isnot(None))
        .order_by(Post.created_at.desc())
    )
    return [row[0] for row in result.all() if row[0]]


async def _pick_unique_topic(
    session: AsyncSession,
    directions: list[str],
    content_type: str,
    recent_titles: list[str],
) -> str:
    """Pick a random direction and ask AI to generate a unique topic within it."""
    direction = random.choice(directions)
    topic = await generate_unique_topic(direction, content_type, recent_titles)
    return topic


async def _pick_feature_topic(
    session: AsyncSession,
    directions: list[str],
    recent_titles: list[str],
    travel_context: str,
) -> str:
    """Generate a feature topic tied to a real travel context from today's posts."""
    direction = random.choice(directions)
    topic = await generate_unique_topic(
        direction, "feature", recent_titles, travel_context=travel_context,
    )
    return topic


async def _enrich_post_with_geo(post: Post) -> None:
    """Extract geo coordinates from the post topic and store on the Post object."""
    try:
        geo = await extract_location_coordinates(post.title or post.content_raw[:300])
        if geo:
            post.latitude = geo["lat"]
            post.longitude = geo["lon"]
            post.place_name = (geo.get("name") or "")[:500]
    except Exception:
        logger.warning("Geo extraction failed for post_id=%s", post.id)


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
    - 2 tourism news (from RSS, priority Ukraine; fallback = leisure travel)
    - 1 active sports/events (tied to location)
    - 1 leisure travel (places, culture, gastro)
    - 1 app feature (tied to one of today's travel topics)
    Order: news, active, leisure, news, feature
    """
    logger.info("=== CREATE POSTS === Starting daily post creation for %d platforms: %s",
                len(ALL_PLATFORMS), [p.value for p in ALL_PLATFORMS])
    if not ALL_PLATFORMS:
        logger.error("=== CREATE POSTS === NO platforms configured! Check API keys in .env")
        return

    async with async_session() as session:
        created_posts: list[tuple[Post, str]] = []
        recent_titles = await _get_recent_titles(session, days=60)
        today_travel_topics: list[str] = []

        # --- 2 Tourism news from RSS ---
        news_entries = await _fetch_tourism_news(session, count=2)
        logger.info("=== CREATE POSTS === RSS entries found: %d", len(news_entries))
        for entry in news_entries:
            source_name = entry.get("source_name", "")
            pub_date = entry.get("published")
            date_str = pub_date.strftime("%d.%m.%Y") if pub_date else ""
            date_line = f"Дата публікації: {date_str}\n" if date_str else ""
            content = (
                f"{date_line}"
                f"{entry['title']}\n\n"
                f"{entry.get('summary', '')}\n\n"
                f"Джерело: {source_name}\n{entry['link']}"
            )
            post = Post(
                title=entry["title"][:200],
                content_raw=content,
                source="rss",
                source_url=entry["link"],
                source_published_at=pub_date,
            )
            session.add(post)
            await session.flush()
            for platform in ALL_PLATFORMS:
                session.add(Publication(post_id=post.id, platform=platform.value))
            created_posts.append((post, "tourism_news"))
            today_travel_topics.append(entry["title"][:200])

        while len([p for p in created_posts if p[1] in ("tourism_news", "leisure_travel")]) < 2:
            leisure_topic = await _pick_unique_topic(
                session, LEISURE_DIRECTIONS, "leisure_travel", recent_titles,
            )
            post = Post(title=leisure_topic[:200], content_raw=leisure_topic, source="ai")
            session.add(post)
            await session.flush()
            for platform in ALL_PLATFORMS:
                session.add(Publication(post_id=post.id, platform=platform.value))
            created_posts.append((post, "leisure_travel"))
            recent_titles.append(leisure_topic)
            today_travel_topics.append(leisure_topic[:200])

        # --- 1 Active sports/events (tied to location) ---
        active_topic = await _pick_unique_topic(
            session, ACTIVE_DIRECTIONS, "active_travel", recent_titles,
        )
        post = Post(title=active_topic[:200], content_raw=active_topic, source="ai")
        session.add(post)
        await session.flush()
        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))
        created_posts.append((post, "active_travel"))
        recent_titles.append(active_topic)
        today_travel_topics.append(active_topic[:200])

        # --- 1 Leisure travel (places, culture, gastro) ---
        leisure_topic = await _pick_unique_topic(
            session, LEISURE_DIRECTIONS, "leisure_travel", recent_titles,
        )
        post = Post(title=leisure_topic[:200], content_raw=leisure_topic, source="ai")
        session.add(post)
        await session.flush()
        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))
        created_posts.append((post, "leisure_travel"))
        recent_titles.append(leisure_topic)
        today_travel_topics.append(leisure_topic[:200])

        # --- 1 App feature (tied to one of today's travel topics) ---
        travel_context = random.choice(today_travel_topics) if today_travel_topics else ""
        feature_topic = await _pick_feature_topic(
            session, FEATURE_DIRECTIONS, recent_titles, travel_context,
        )
        post = Post(title=feature_topic[:200], content_raw=feature_topic, source="ai")
        session.add(post)
        await session.flush()
        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))
        created_posts.append((post, "feature"))
        recent_titles.append(feature_topic)

        for post_obj, _ in created_posts:
            await _enrich_post_with_geo(post_obj)

        for post_obj, _ in created_posts:
            try:
                import json as _json
                tr = await translate_post(
                    post_obj.title or "", post_obj.content_raw or "",
                )
                if tr:
                    post_obj.translations = _json.dumps(tr, ensure_ascii=False)
            except Exception:
                logger.warning("Translation failed for post_id=%s", post_obj.id)

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


SLOT_CONTENT_TYPES = ["tourism_news", "active_travel", "leisure_travel", "tourism_news", "feature"]


async def create_single_post(content_type: str) -> Optional[Post]:
    """Create ONE fresh post of the given type. Returns the Post or None."""
    logger.info("=== FRESH === Creating single post type=%s", content_type)

    async with async_session() as session:
        recent_titles = await _get_recent_titles(session, days=60)
        post: Optional[Post] = None

        if content_type == "tourism_news":
            entries = await _fetch_tourism_news(session, count=1)
            if entries:
                entry = entries[0]
                source_name = entry.get("source_name", "")
                pub_date = entry.get("published")
                date_str = pub_date.strftime("%d.%m.%Y") if pub_date else ""
                date_line = f"Дата публікації: {date_str}\n" if date_str else ""
                content = (
                    f"{date_line}"
                    f"{entry['title']}\n\n"
                    f"{entry.get('summary', '')}\n\n"
                    f"Джерело: {source_name}\n{entry['link']}"
                )
                post = Post(
                    title=entry["title"][:200],
                    content_raw=content,
                    source="rss",
                    source_url=entry["link"],
                    source_published_at=pub_date,
                )
            else:
                logger.info("=== FRESH === No RSS news, falling back to leisure_travel")
                content_type = "leisure_travel"

        if content_type == "active_travel":
            topic = await _pick_unique_topic(session, ACTIVE_DIRECTIONS, "active_travel", recent_titles)
            post = Post(title=topic[:200], content_raw=topic, source="ai")

        elif content_type == "leisure_travel":
            topic = await _pick_unique_topic(session, LEISURE_DIRECTIONS, "leisure_travel", recent_titles)
            post = Post(title=topic[:200], content_raw=topic, source="ai")

        elif content_type == "feature":
            today_start_utc = get_today_start_utc()
            result = await session.execute(
                select(Post.title).where(Post.created_at >= today_start_utc, Post.title.isnot(None)).limit(5)
            )
            recent_today = [r[0] for r in result.all() if r[0]]
            travel_context = random.choice(recent_today) if recent_today else ""
            topic = await _pick_feature_topic(session, FEATURE_DIRECTIONS, recent_titles, travel_context)
            post = Post(title=topic[:200], content_raw=topic, source="ai")

        if not post:
            logger.warning("=== FRESH === Failed to create post type=%s", content_type)
            return None

        session.add(post)
        await session.flush()

        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))

        await _enrich_post_with_geo(post)

        try:
            import json as _json
            tr = await translate_post(post.title or "", post.content_raw or "")
            if tr:
                post.translations = _json.dumps(tr, ensure_ascii=False)
        except Exception:
            logger.warning("Translation failed for post_id=%s", post.id)

        await session.commit()
        logger.info("=== FRESH === Created post_id=%d type=%s title='%s'",
                     post.id, content_type, (post.title or "")[:50])
        return post


async def _count_published_today() -> int:
    """Count distinct posts published today (across all platforms)."""
    today_start_utc = get_today_start_utc()
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(sa_func.distinct(Post.id)))
            .join(Publication)
            .where(
                Post.created_at >= today_start_utc,
                Publication.status == PostStatus.PUBLISHED,
            )
        )
        return result.scalar() or 0


async def publish_scheduled_post(time_slot: int) -> None:
    """Create a FRESH post and publish it immediately.

    Each slot has a content type (news, active, leisure, feature).
    The post is created right before publishing for maximum freshness.
    Uses a lock to prevent race conditions between cron and catchup.
    """
    async with _publish_lock:
        await _publish_scheduled_post_inner(time_slot)


async def _publish_scheduled_post_inner(time_slot: int) -> None:
    content_type = SLOT_CONTENT_TYPES[time_slot] if time_slot < len(SLOT_CONTENT_TYPES) else "leisure_travel"

    published_count = await _count_published_today()
    if published_count > time_slot:
        logger.info(
            "=== PUBLISH === Slot %d SKIPPED: already %d post(s) published today (slot needs at most %d)",
            time_slot, published_count, time_slot + 1,
        )
        return

    logger.info("=== PUBLISH === Slot %d: content_type=%s, published_today=%d — creating fresh post",
                time_slot, content_type, published_count)

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

    if queued_posts:
        post = queued_posts[0]
        logger.info("=== PUBLISH === Found pre-created post_id=%d, publishing it first", post.id)
    else:
        post = await create_single_post(content_type)
        if not post:
            logger.warning("=== PUBLISH === Failed to create post for slot %d", time_slot)
            return

    async with async_session() as session:
        post = await session.merge(post)
        logger.info(
            "=== PUBLISH === Slot %d: post_id=%d '%s'",
            time_slot, post.id, (post.title or "")[:50],
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

        best_text = next(
            (p.content_adapted for p in publications
             if p.content_adapted and p.platform == Platform.TELEGRAM.value),
            None,
        ) or next(
            (p.content_adapted for p in publications if p.content_adapted), None,
        )
        if best_text and len(best_text) > len(post.content_raw or ""):
            post.content_raw = best_text
            if post.translations:
                try:
                    tr = await translate_post(post.title or "", best_text)
                    if tr:
                        post.translations = __import__("json").dumps(tr, ensure_ascii=False)
                except Exception:
                    logger.warning("Re-translation failed for post %d", post.id, exc_info=True)

        await session.commit()

        try:
            import json as _json
            from content.blog_generator import generate_post_html, save_thumbnail, _parse_translations
            _thumb_url = None
            if post.image_path:
                _thumb_url = save_thumbnail(post.id, post.image_path)
            _pub_at = next(
                (p.published_at for p in publications if p.status == PostStatus.PUBLISHED),
                None,
            )
            generate_post_html(
                post_id=post.id, title=post.title or "",
                content=post.content_raw or "", published_at=_pub_at,
                image_url=_thumb_url, source_url=post.source_url,
                latitude=post.latitude, longitude=post.longitude,
                place_name=post.place_name,
                translations=_parse_translations(post.translations),
            )
        except Exception:
            logger.warning("Blog page generation failed for post_id=%d", post.id, exc_info=True)

        await _cleanup_post_media(session, post)

        try:
            from scheduler.blog_sync import sync_blog_to_vps
            synced = await sync_blog_to_vps()
            logger.info("Auto blog-sync after publish: %d files pushed", synced)
        except Exception:
            logger.warning("Auto blog-sync failed after post %d", post.id, exc_info=True)


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


async def _generate_and_verify_text(
    post: Post,
    platform: Platform,
    content_type: str,
) -> str:
    """Generate platform-adapted text with editorial fact-checking.

    Retries up to MAX_FACT_CHECK_RETRIES times with correction hints.
    If all retries fail, generates a safe version without specific dates/events.
    """
    last_suggestion = ""

    for attempt in range(MAX_FACT_CHECK_RETRIES + 1):
        if post.source == "rss":
            source_text = post.content_raw
            if attempt > 0:
                source_text += (
                    "\n\nУВАГА РЕДАКТОРА: попередня версія відхилена фактчекером. "
                    f"Проблема: {last_suggestion}\n"
                    "Перепиши ТОЧНО за оригіналом, НЕ додавай фактів від себе."
                )
            text = await generate_post_text(
                topic="", platform=platform,
                source_text=source_text,
                content_type="tourism_news",
            )
        else:
            topic = post.content_raw
            if attempt > 0:
                topic += (
                    f"\n\nУВАГА РЕДАКТОРА: попередня версія відхилена фактчекером. "
                    f"Проблема: {last_suggestion}\n"
                    "НЕ вигадуй конкретних дат та подій. "
                    "Якщо не впевнений у даті — НЕ ПИШИ дату. "
                    "Пиши лише загальновідомі та перевірені факти."
                )
            text = await generate_post_text(
                topic=topic, platform=platform,
                content_type=content_type,
            )

        check = await fact_check_post(text, content_type)
        if check.passed:
            if attempt > 0:
                logger.info(
                    "FACT-CHECK: Passed on attempt %d/%d for %s post_id=%s",
                    attempt + 1, MAX_FACT_CHECK_RETRIES + 1,
                    platform.value, post.id,
                )
            return text

        last_suggestion = check.suggestion or check.summary
        logger.warning(
            "FACT-CHECK: Attempt %d/%d FAIL for %s post_id=%s: %s",
            attempt + 1, MAX_FACT_CHECK_RETRIES + 1,
            platform.value, post.id, check.summary[:150],
        )

    logger.error(
        "FACT-CHECK: All %d attempts exhausted for post_id=%s — generating safe version",
        MAX_FACT_CHECK_RETRIES + 1, post.id,
    )

    safe_instruction = (
        "\n\nКРИТИЧНО: Попередні версії цього поста відхилені фактчекером через "
        "фактичні помилки. Ця версія ПОВИННА бути БЕЗПЕЧНОЮ:\n"
        "- НЕ вказуй ЖОДНИХ конкретних дат подій/змагань/фестивалів\n"
        "- НЕ згадуй конкретні змагання/турніри з датами\n"
        "- Пиши ТІЛЬКИ загальновідомі факти про місце (атмосфера, краса, поради)\n"
        "- Якщо тема про спорт — пиши про стадіон/трасу без прив'язки до конкретних подій\n"
        "- Використовуй формулювання 'в сезон', 'щороку', 'традиційно' замість конкретних дат"
    )

    if post.source == "rss":
        return await generate_post_text(
            topic="", platform=platform,
            source_text=post.content_raw + safe_instruction,
            content_type="tourism_news",
        )
    else:
        return await generate_post_text(
            topic=post.content_raw + safe_instruction,
            platform=platform,
            content_type=content_type,
        )


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
            pub.content_adapted = await _generate_and_verify_text(
                post, platform, content_type,
            )

            limits = PLATFORM_LIMITS.get(platform, {})
            if limits.get("supports_links") and post.latitude and post.longitude:
                map_url = build_map_link(post.latitude, post.longitude, post.place_name or "")
                pub.content_adapted += f"\n\n📍 {post.place_name or 'На карті'}: {map_url}"

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
