"""Post publishing: text generation with fact-checking + multi-platform dispatch."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform, PLATFORM_LIMITS, get_platform_instance
from config.settings import settings, get_today_start_utc
from content.generator import (
    generate_post_text, build_map_link, translate_post,
    extract_location_coordinates, BlockedTerritoryError,
)
from content.fact_checker import fact_check_post, MAX_FACT_CHECK_RETRIES
from content.media import get_image_for_post, create_slideshow_video, cleanup_media_file
from db.database import async_session
from db.models import Post, Publication, PostStatus

from scheduler.post_creator import create_single_post, SLOT_CONTENT_TYPES

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

_publish_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Content type detection
# ---------------------------------------------------------------------------

def _detect_content_type(post: Post) -> str:
    """Determine content type from post title/content for correct AI prompt."""
    if post.source == "poi":
        return "poi_spotlight"
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


# ---------------------------------------------------------------------------
# Text generation + fact-checking loop
# ---------------------------------------------------------------------------

async def _extract_geo_for_post(post: Post, text: str) -> None:
    """Extract geo coordinates from generated text and update the Post object."""
    try:
        geo = await extract_location_coordinates(text[:500])
        if geo:
            post.latitude = geo["lat"]
            post.longitude = geo["lon"]
            post.place_name = (geo.get("name") or "")[:500]
            logger.info("GEO: post_id=%s → %s (%.4f, %.4f)",
                        post.id, post.place_name, geo["lat"], geo["lon"])
            post.log_pipeline("geo", "ok", f"{post.place_name} ({geo['lat']:.4f}, {geo['lon']:.4f})")
        else:
            post.latitude = None
            post.longitude = None
            post.place_name = None
            post.log_pipeline("geo", "warn", f"No location found in text: '{text[:80]}...'")
    except Exception as e:
        logger.warning("GEO: extraction failed for post_id=%s", post.id)
        post.log_pipeline("geo", "fail", str(e)[:200])


async def _generate_and_verify_text(
    post: Post,
    platform: Platform,
    content_type: str,
) -> Optional[str]:
    """Generate text → extract geo → fact-check. Repeat on failure.

    On each attempt the cycle is:
      1. Generate platform-adapted text (with editor hints on retries)
      2. Extract geo coordinates from the generated text
      3. Run fact-checker
    If fact-check fails, the text is regenerated with correction hints and
    geo is re-extracted from the new version.
    Returns None if all attempts are exhausted — caller should try a new topic
    or skip the slot entirely.
    """
    last_suggestion = ""
    territory_hint = ""

    for attempt in range(MAX_FACT_CHECK_RETRIES + 1):
        try:
            if post.source == "rss":
                source_text = post.content_raw
                if attempt > 0:
                    source_text += (
                        "\n\nУВАГА РЕДАКТОРА: попередня версія відхилена фактчекером. "
                        f"Проблема: {last_suggestion}\n"
                        "Перепиши ТОЧНО за оригіналом, НЕ додавай фактів від себе."
                        f"{territory_hint}"
                    )
                text = await generate_post_text(
                    topic="", platform=platform,
                    source_text=source_text,
                    content_type="tourism_news",
                )
            elif post.source == "poi":
                source_text = post.content_raw
                if attempt > 0:
                    source_text += (
                        "\n\nУВАГА РЕДАКТОРА: попередня версія відхилена. "
                        f"Проблема: {last_suggestion}\n"
                        "Перепиши ТІЛЬКИ на основі наданих даних, НЕ додавай нічого від себе."
                        f"{territory_hint}"
                    )
                text = await generate_post_text(
                    topic="", platform=platform,
                    source_text=source_text,
                    content_type="poi_spotlight",
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
                        f"{territory_hint}"
                    )
                text = await generate_post_text(
                    topic=topic, platform=platform,
                    content_type=content_type,
                )
        except BlockedTerritoryError as e:
            post.log_pipeline("text_gen", "fail",
                              f"TERRITORY BLOCK attempt {attempt+1}: '{e.keyword}'")
            logger.warning(
                "TERRITORY BLOCK: attempt %d/%d for %s post_id=%s: '%s'",
                attempt + 1, MAX_FACT_CHECK_RETRIES + 1,
                platform.value, post.id, e.keyword,
            )
            last_suggestion = f"Заборонена територія: {e.keyword}"
            territory_hint = (
                "\nКРИТИЧНО: НЕ згадуй Крим, Донецьк, Луганськ, Маріуполь, "
                "Росію, Білорусь — це ЗАБОРОНЕНІ території! "
                "Напиши про інше, безпечне місце."
            )
            continue

        post.log_pipeline("text_gen", "ok",
                          f"attempt {attempt+1}/{MAX_FACT_CHECK_RETRIES+1} "
                          f"for {platform.value}, len={len(text)}")

        await _extract_geo_for_post(post, text)

        check = await fact_check_post(text, content_type)
        if check.passed:
            post.log_pipeline("fact_check", "ok",
                              f"PASS attempt {attempt+1}/{MAX_FACT_CHECK_RETRIES+1}: {check.summary[:150]}")
            if attempt > 0:
                logger.info(
                    "FACT-CHECK: Passed on attempt %d/%d for %s post_id=%s",
                    attempt + 1, MAX_FACT_CHECK_RETRIES + 1,
                    platform.value, post.id,
                )
            return text

        last_suggestion = check.suggestion or check.summary
        post.log_pipeline("fact_check", "fail",
                          f"FAIL attempt {attempt+1}/{MAX_FACT_CHECK_RETRIES+1}: "
                          f"{check.summary[:100]} | hint: {check.suggestion[:100]}")
        logger.warning(
            "FACT-CHECK: Attempt %d/%d FAIL for %s post_id=%s: %s",
            attempt + 1, MAX_FACT_CHECK_RETRIES + 1,
            platform.value, post.id, check.summary[:150],
        )

    post.log_pipeline("fact_check", "fail",
                      f"All {MAX_FACT_CHECK_RETRIES+1} attempts exhausted — topic rejected")
    logger.error(
        "FACT-CHECK: All %d attempts exhausted for post_id=%s — topic rejected",
        MAX_FACT_CHECK_RETRIES + 1, post.id,
    )
    return None


# ---------------------------------------------------------------------------
# Phase 1: Generate text + extract geo (no publishing yet)
# ---------------------------------------------------------------------------

async def _prepare_publication_text(
    post: Post,
    pub: Publication,
    platform: Platform,
) -> bool:
    """Generate text + extract geo for a single publication.

    Returns True if text is ready (or was already set), False if rejected.
    """
    if pub.content_adapted:
        return True

    content_type = _detect_content_type(post)
    text = await _generate_and_verify_text(post, platform, content_type)
    if text is None:
        pub.status = PostStatus.FAILED
        pub.error_message = "Fact-check rejected all attempts"
        post.log_pipeline("publish", "fail",
                          f"{platform.value}: skipped — fact-check rejected text")
        return False

    pub.content_adapted = text

    limits = PLATFORM_LIMITS.get(platform, {})
    if limits.get("supports_links") and post.latitude and post.longitude:
        map_url = build_map_link(post.latitude, post.longitude, post.place_name or "")
        if "maps.google.com" not in (pub.content_adapted or ""):
            map_suffix = f"\n\n📍 {post.place_name or 'На карті'}: {map_url}"
            max_len = limits.get("max_text_length", 4096)
            if len(pub.content_adapted) + len(map_suffix) > max_len:
                available = max_len - len(map_suffix) - 3
                if available >= 80:
                    pub.content_adapted = pub.content_adapted[:available] + "..."
            pub.content_adapted += map_suffix

    return True


# ---------------------------------------------------------------------------
# Phase 2: Publish with final image
# ---------------------------------------------------------------------------

async def _publish_single(
    session: AsyncSession,
    post: Post,
    pub: Publication,
    platform: Platform,
    image_path: Optional[str],
) -> None:
    if not pub.content_adapted:
        return

    try:
        pub.status = PostStatus.PUBLISHING
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
            post.log_pipeline("publish", "ok",
                              f"{platform.value}: published, id={result.platform_post_id}")
            logger.info("=== PUBLISH === OK %s post_id=%d platform_post_id=%s",
                        platform.value, post.id, result.platform_post_id)
        else:
            pub.retry_count += 1
            if pub.retry_count >= MAX_RETRIES:
                pub.status = PostStatus.FAILED
                post.log_pipeline("publish", "fail",
                                  f"{platform.value}: FINAL FAIL after {pub.retry_count} retries: {result.error[:150]}")
                logger.error("=== PUBLISH === FINAL FAIL %s post_id=%d after %d retries: %s",
                             platform.value, post.id, pub.retry_count, result.error)
            else:
                pub.status = PostStatus.QUEUED
                post.log_pipeline("publish", "warn",
                                  f"{platform.value}: retry {pub.retry_count}/{MAX_RETRIES}: {result.error[:150]}")
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


# ---------------------------------------------------------------------------
# Media cleanup
# ---------------------------------------------------------------------------

async def _cleanup_post_media(session: AsyncSession, post: Post) -> None:
    """Delete local media files once no queued publications remain."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def count_published_today() -> int:
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


# ---------------------------------------------------------------------------
# Main entry: publish a scheduled slot
# ---------------------------------------------------------------------------

async def publish_scheduled_post(time_slot: int) -> None:
    """Create a FRESH post and publish it immediately.

    Each slot has a content type (news, active, leisure, feature).
    Uses a lock to prevent race conditions between cron and catchup.
    """
    async with _publish_lock:
        await _publish_scheduled_post_inner(time_slot)


MAX_TOPIC_ATTEMPTS = 2


async def _try_publish_post(
    post: Post,
    time_slot: int,
    content_type: str,
) -> bool:
    """Attempt to publish a post on all platforms.

    Returns True if at least one platform published successfully.
    Returns False if fact-check rejected the text (all pubs marked FAILED).
    """
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

        # ── Phase 1: generate text + extract geo for all platforms ──
        for pub in publications:
            platform = Platform(pub.platform)
            await _prepare_publication_text(post, pub, platform)
        await session.commit()

        # ── Phase 2: fetch image AFTER text + geo are ready ──
        image_path = post.image_path
        if not image_path:
            place = post.place_name or ""

            if post.source == "poi":
                # POI posts: NEVER use Pexels/DALL-E — these return random
                # unrelated images (e.g. Pexels "Le Montclair" → museum in NJ).
                # Better no photo than a WRONG photo.
                logger.info(
                    "=== PUBLISH === POI post_id=%d has no real photo — "
                    "publishing WITHOUT image (Pexels/DALL-E disabled for POI)",
                    post.id,
                )
                post.log_pipeline("image", "skip",
                                  "No real POI photo; Pexels/DALL-E disabled for POI posts")
            else:
                query = f"{place} landmark" if place else "travel landscape"
                image_path = await get_image_for_post(
                    query, use_dalle=False, prefer_dalle=False,
                )
                if not image_path:
                    country = ""
                    if post.latitude and post.longitude:
                        country = f" ({post.latitude:.1f}, {post.longitude:.1f})"
                    best_text = next(
                        (p.content_adapted for p in publications if p.content_adapted),
                        post.content_raw or post.title or "travel",
                    )
                    dalle_hint = (
                        f"Photorealistic travel photography of {place}{country}. "
                        f"Context: {best_text[:200]}. "
                        f"Beautiful scenery, professional travel magazine style, bright daylight."
                    )
                    query = f"{place} {best_text[:80]}".strip() or "travel landscape"
                    image_path = await get_image_for_post(
                        query, use_dalle=True, prefer_dalle=True, dalle_prompt=dalle_hint,
                    )

            if image_path:
                post.image_path = image_path
                await session.commit()
            logger.info(
                "=== PUBLISH === Image for post_id=%d place='%s': %s",
                post.id, place, "found" if image_path else "none (text-only post)",
            )

        # ── Phase 3: publish with correct image ──
        for pub in publications:
            if pub.status == PostStatus.FAILED:
                continue
            platform = Platform(pub.platform)

            if platform == Platform.INSTAGRAM and not image_path and post.source == "poi":
                pub.status = PostStatus.FAILED
                pub.error_message = "POI post has no real photo; Instagram requires image; Pexels/DALL-E disabled for POI"
                post.log_pipeline("publish", "skip",
                                  f"{platform.value}: skipped — no real photo for POI, Instagram needs image")
                logger.info(
                    "=== PUBLISH === Skipping Instagram for POI post_id=%d — no real photo available",
                    post.id,
                )
                continue

            await _publish_single(session, post, pub, platform, image_path)

        any_published = any(p.status == PostStatus.PUBLISHED for p in publications)
        all_fact_check_failed = all(
            p.status == PostStatus.FAILED and p.error_message == "Fact-check rejected all attempts"
            for p in publications
        )

        if all_fact_check_failed:
            logger.warning(
                "=== PUBLISH === post_id=%d rejected by fact-check on all platforms",
                post.id,
            )
            await session.commit()
            return False

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
                        post.translations = json.dumps(tr, ensure_ascii=False)
                except Exception:
                    logger.warning("Re-translation failed for post %d", post.id, exc_info=True)

        await session.commit()

        if any_published:
            try:
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

        if any_published:
            try:
                from scheduler.blog_sync import sync_blog_to_vps
                synced = await sync_blog_to_vps()
                logger.info("Auto blog-sync after publish: %d files pushed", synced)
            except Exception:
                logger.warning("Auto blog-sync failed after post %d", post.id, exc_info=True)

        return any_published


async def _publish_scheduled_post_inner(time_slot: int) -> None:
    content_type = SLOT_CONTENT_TYPES[time_slot] if time_slot < len(SLOT_CONTENT_TYPES) else "leisure_travel"

    published_count = await count_published_today()
    if published_count > time_slot:
        logger.info(
            "=== PUBLISH === Slot %d SKIPPED: already %d post(s) published today (slot needs at most %d)",
            time_slot, published_count, time_slot + 1,
        )
        return

    logger.info("=== PUBLISH === Slot %d: content_type=%s, published_today=%d — creating fresh post",
                time_slot, content_type, published_count)

    today_start_utc = get_today_start_utc()

    for topic_attempt in range(MAX_TOPIC_ATTEMPTS):
        if topic_attempt > 0:
            logger.info(
                "=== PUBLISH === Slot %d: generating NEW topic (attempt %d/%d)",
                time_slot, topic_attempt + 1, MAX_TOPIC_ATTEMPTS,
            )

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

        if queued_posts and topic_attempt == 0:
            post = queued_posts[0]
            logger.info("=== PUBLISH === Found pre-created post_id=%d, publishing it first", post.id)
        else:
            post = await create_single_post(content_type)
            if not post:
                logger.warning("=== PUBLISH === Failed to create post for slot %d", time_slot)
                continue

        success = await _try_publish_post(post, time_slot, content_type)
        if success:
            return

    logger.error(
        "=== PUBLISH === Slot %d SKIPPED: %d topic attempts failed fact-check — no post published",
        time_slot, MAX_TOPIC_ATTEMPTS,
    )
