"""Post creation: daily batch and single-post generation from POI data / RSS / AI."""
from __future__ import annotations

import json as _json
import logging
import random
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import configured_platforms
from config.settings import settings, get_today_start_utc
from content.generator import (
    generate_unique_topic, translate_post, BlockedTerritoryError,
)
from content.tourism_topics import (
    contains_blocked_territory,
    TOURISM_RSS_FEEDS, BANNED_RSS_KEYWORDS,
    ACTIVE_DIRECTIONS, LEISURE_DIRECTIONS, FEATURE_DIRECTIONS,
)
from content.rss_parser import fetch_feed
from content.poi_client import fetch_next_poi, mark_poi_posted, format_poi_for_ai, ensure_event_for_point
from content.web_news import search_fresh_travel_news, format_news_for_ai
from db.database import async_session
from db.models import Post, Publication

logger = logging.getLogger(__name__)

ALL_PLATFORMS = configured_platforms()

SLOT_CONTENT_TYPES = [
    "web_news",       # 08:00  — fresh travel news (fallback: POI)
    "poi_spotlight",  # 10:00  — POI / interesting place on the map
    "web_news",       # 12:00  — fresh travel news (fallback: POI)
    "feature",        # 15:00  — app feature highlight (1/day)
    "web_news",       # 18:00  — fresh travel news (fallback: POI)
]

MAX_TERRITORY_RETRIES = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_recent_titles(session: AsyncSession, days: int = 60) -> list[str]:
    """Fetch post titles from the last N days for uniqueness checks."""
    from datetime import datetime, timezone, timedelta
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
    for attempt in range(MAX_TERRITORY_RETRIES):
        direction = random.choice(directions)
        try:
            return await generate_unique_topic(direction, content_type, recent_titles)
        except BlockedTerritoryError as e:
            logger.warning("Territory block in topic (attempt %d/%d): %s",
                           attempt + 1, MAX_TERRITORY_RETRIES, e.keyword)
    direction = random.choice(directions)
    return await generate_unique_topic(direction, content_type, recent_titles)


async def _pick_feature_topic(
    session: AsyncSession,
    directions: list[str],
    recent_titles: list[str],
    travel_context: str,
) -> str:
    """Generate a feature topic tied to a real travel context from today's posts."""
    for attempt in range(MAX_TERRITORY_RETRIES):
        direction = random.choice(directions)
        try:
            return await generate_unique_topic(
                direction, "feature", recent_titles, travel_context=travel_context,
            )
        except BlockedTerritoryError as e:
            logger.warning("Territory block in feature topic (attempt %d/%d): %s",
                           attempt + 1, MAX_TERRITORY_RETRIES, e.keyword)
    direction = random.choice(directions)
    return await generate_unique_topic(
        direction, "feature", recent_titles, travel_context=travel_context,
    )



def _is_banned(title: str, summary: str) -> bool:
    """Check if an RSS entry contains banned keywords or blocked territories."""
    text = (title + " " + summary).lower()
    if any(kw in text for kw in BANNED_RSS_KEYWORDS):
        return True
    if contains_blocked_territory(title + " " + summary):
        return True
    return False


async def _fetch_tourism_news(session: AsyncSession, count: int = 2) -> list[dict]:
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


# ---------------------------------------------------------------------------
# Post creation
# ---------------------------------------------------------------------------

async def create_daily_posts() -> None:
    """Generate 5 posts for today — all from enriched POI database.

    Fallback chain: poi_spotlight → leisure_travel (AI) if no POI available.
    Each post features a real, verified place with CTA to download the app.
    """
    logger.info("=== CREATE POSTS === Starting daily post creation for %d platforms: %s",
                len(ALL_PLATFORMS), [p.value for p in ALL_PLATFORMS])
    if not ALL_PLATFORMS:
        logger.error("=== CREATE POSTS === NO platforms configured! Check API keys in .env")
        return

    async with async_session() as session:
        created_posts: list[tuple[Post, str]] = []
        recent_titles = await _get_recent_titles(session, days=60)

        for slot_idx, content_type in enumerate(SLOT_CONTENT_TYPES):
            logger.info("=== CREATE POSTS === Slot %d: type=%s", slot_idx, content_type)

            if content_type == "web_news":
                post = await _create_web_news_post(session)
                if post:
                    created_posts.append((post, "web_news"))
                    recent_titles.append(post.title or "")
                    continue
                logger.info("=== CREATE POSTS === Slot %d: no web news, fallback to poi_spotlight", slot_idx)
                content_type = "poi_spotlight"

            if content_type == "poi_spotlight":
                post = await _create_poi_spotlight_post(session)
                if post:
                    created_posts.append((post, "poi_spotlight"))
                    recent_titles.append(post.title or "")
                    continue

                logger.info("=== CREATE POSTS === Slot %d: no POI, fallback to leisure_travel", slot_idx)
                content_type = "leisure_travel"

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
                    post.log_pipeline("topic", "ok", f"RSS: {source_name} — {entry['title'][:120]}")
                    session.add(post)
                    await session.flush()
                    for platform in ALL_PLATFORMS:
                        session.add(Publication(post_id=post.id, platform=platform.value))
                    created_posts.append((post, "tourism_news"))
                    recent_titles.append(entry["title"][:200])
                    continue
                content_type = "leisure_travel"

            if content_type in ("active_travel", "leisure_travel"):
                directions = ACTIVE_DIRECTIONS if content_type == "active_travel" else LEISURE_DIRECTIONS
                topic = await _pick_unique_topic(session, directions, content_type, recent_titles)
                post = Post(title=topic[:200], content_raw=topic, source="ai")
                post.log_pipeline("topic", "ok", f"AI {content_type}: {topic[:120]}")
                session.add(post)
                await session.flush()
                for platform in ALL_PLATFORMS:
                    session.add(Publication(post_id=post.id, platform=platform.value))
                created_posts.append((post, content_type))
                recent_titles.append(topic)

            elif content_type == "feature":
                travel_context = random.choice(recent_titles[-5:]) if recent_titles else ""
                topic = await _pick_feature_topic(session, FEATURE_DIRECTIONS, recent_titles, travel_context)
                post = Post(title=topic[:200], content_raw=topic, source="ai")
                post.log_pipeline("topic", "ok", f"AI feature: {topic[:120]}")
                session.add(post)
                await session.flush()
                for platform in ALL_PLATFORMS:
                    session.add(Publication(post_id=post.id, platform=platform.value))
                created_posts.append((post, "feature"))
                recent_titles.append(topic)

        for post_obj, ctype in created_posts:
            if ctype == "poi_spotlight":
                continue
            try:
                tr = await translate_post(
                    post_obj.title or "", post_obj.content_raw or "",
                )
                if tr:
                    post_obj.translations = _json.dumps(tr, ensure_ascii=False)
                    post_obj.log_pipeline("translate", "ok", f"{len(tr)} languages")
                else:
                    post_obj.log_pipeline("translate", "warn", "No translations returned")
            except Exception as e:
                logger.warning("Translation failed for post_id=%s", post_obj.id)
                post_obj.log_pipeline("translate", "fail", str(e)[:200])

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


async def _create_web_news_post(session: AsyncSession) -> Optional[Post]:
    """Try to create a post from fresh web news via Perplexity.

    Returns a Post if a fresh, non-duplicate news item was found, or None
    so the caller can fall back to POI spotlight.
    """
    news_item = await search_fresh_travel_news()
    if news_item is None:
        logger.info("=== FRESH === No fresh web news found, will fallback to POI")
        return None

    content_for_ai = format_news_for_ai(news_item)
    title = news_item.title[:200]

    post = Post(
        title=title,
        content_raw=content_for_ai,
        source="web_news",
        source_url=news_item.source_url,
        place_name=(news_item.location or "")[:500],
    )
    post.log_pipeline(
        "topic", "ok",
        f"web_news: {news_item.source_name} — {news_item.title[:80]} ({news_item.date})",
    )

    session.add(post)
    await session.flush()

    for platform in ALL_PLATFORMS:
        session.add(Publication(post_id=post.id, platform=platform.value))

    try:
        tr = await translate_post(title, content_for_ai[:1000])
        if tr:
            post.translations = _json.dumps(tr, ensure_ascii=False)
            post.log_pipeline("translate", "ok", f"{len(tr)} languages")
    except Exception as e:
        logger.warning("Translation failed for web_news post: %s", e)
        post.log_pipeline("translate", "fail", str(e)[:200])

    await session.commit()
    logger.info(
        "=== FRESH === Web news post created: post_id=%d title='%s' source=%s",
        post.id, title[:50], news_item.source_name,
    )
    return post


MAX_POI_SKIP_RETRIES = 5


def _is_generic_poi_name(poi: dict) -> bool:
    """Detect POIs where name is just the type (e.g., name='monument', type='monument').

    These produce terrible AI content because there's no specific place info.
    """
    name = (poi.get("name") or "").strip().lower().replace("_", " ")
    point_type = (poi.get("pointType") or "").strip().lower().replace("_", " ")
    if not name or not point_type:
        return False
    if name == point_type:
        return True
    if len(name) <= 3:
        return True
    return False


async def _create_poi_spotlight_post(session: AsyncSession) -> Optional[Post]:
    """Create a post from the richest available POI in our database.

    EDITORIAL GATE: POI must have a verifiable source (description from
    Wikipedia/OSM or a wikipediaUrl). Without a source we skip this POI
    and let the caller try the next one or fall back.

    Retries up to MAX_POI_SKIP_RETRIES times when encountering generic/bad POIs.
    """
    for _skip_attempt in range(MAX_POI_SKIP_RETRIES):
        poi = await fetch_next_poi()
        if not poi:
            logger.warning("=== FRESH === No POI available, falling back to leisure_travel")
            return None

        point_id = poi.get("id")
        poi_name = poi.get("name", "")[:60]

        if _is_generic_poi_name(poi):
            logger.warning(
                "=== FRESH === EDITORIAL SKIP: POI #%s name='%s' is generic (same as type '%s') "
                "— cannot create quality post. Marking as posted to skip.",
                point_id, poi_name, poi.get("pointType"),
            )
            if point_id:
                await mark_poi_posted(point_id)
            continue

        has_description = bool((poi.get("description") or "").strip())
        has_wikipedia = bool((poi.get("wikipediaUrl") or "").strip())
        has_website = bool((poi.get("website") or "").strip())

        if not has_description and not has_wikipedia:
            logger.warning(
                "=== FRESH === EDITORIAL SKIP: POI #%s '%s' has NO description and NO Wikipedia URL "
                "— cannot create post without verifiable source. Marking as posted to avoid retry.",
                point_id, poi_name,
            )
            if point_id:
                await mark_poi_posted(point_id)
            continue

        poi_rating = poi.get("rating", 0) or 0
        if poi_rating > 0 and poi_rating < 3.0:
            logger.warning(
                "=== FRESH === EDITORIAL SKIP: POI #%s '%s' has LOW RATING %.1f "
                "— not suitable for social post. Marking as posted to avoid retry.",
                point_id, poi_name, poi_rating,
            )
            if point_id:
                await mark_poi_posted(point_id)
            continue

        break
    else:
        logger.error(
            "=== FRESH === Exhausted %d POI skip retries — all POIs were generic/bad",
            MAX_POI_SKIP_RETRIES,
        )
        return None

    poi_text = format_poi_for_ai(poi)
    title = f"{poi.get('name', '')} — {poi.get('city', '')}".strip(" —")

    post = Post(
        title=title[:200],
        content_raw=poi_text,
        source="poi",
        source_url=poi.get("wikipediaUrl") or poi.get("website") or "",
        latitude=poi.get("latitude"),
        longitude=poi.get("longitude"),
        place_name=poi.get("name", "")[:500],
        poi_point_id=poi.get("id"),
    )
    post.log_pipeline("topic", "ok",
                      f"POI #{poi.get('id')}: {poi.get('name', '')[:80]} ({poi.get('city', '')})")

    session.add(post)
    await session.flush()

    for platform in ALL_PLATFORMS:
        session.add(Publication(post_id=post.id, platform=platform.value))

    try:
        tr = await translate_post(title, poi_text[:1000])
        if tr:
            post.translations = _json.dumps(tr, ensure_ascii=False)
            post.log_pipeline("translate", "ok", f"{len(tr)} languages")
    except Exception as e:
        logger.warning("Translation failed for POI post: %s", e)
        post.log_pipeline("translate", "fail", str(e)[:200])

    point_id = poi.get("id")
    if point_id:
        await mark_poi_posted(point_id)
        post.log_pipeline("poi_mark", "ok", f"Point {point_id} marked as posted")

        backend_eid = await ensure_event_for_point(point_id)
        if backend_eid:
            post.backend_event_id = backend_eid
            post.log_pipeline("backend_event", "ok",
                              f"Event {backend_eid} for point {point_id}")
        else:
            post.log_pipeline("backend_event", "warn",
                              f"Could not ensure backend event for point {point_id}")

    poi_image_url = poi.get("imageUrl") or ""
    if not poi_image_url and point_id:
        from geo_agent.backend_client import try_enrich_photo
        google_url = await try_enrich_photo(point_id)
        if google_url:
            poi_image_url = google_url
            post.log_pipeline("image", "ok", f"Google photo enriched: {google_url[:80]}")
    if poi_image_url:
        from content.media import download_image_from_url
        downloaded = await download_image_from_url(poi_image_url)
        if downloaded:
            post.image_path = downloaded
            post.log_pipeline("image", "ok", f"Real photo from POI: {poi_image_url[:80]}")
        else:
            post.log_pipeline("image", "warn", f"POI image download failed: {poi_image_url[:80]}")

    await session.commit()
    logger.info("=== FRESH === POI post created: post_id=%d title='%s'",
                post.id, title[:50])
    return post


async def create_single_post(content_type: str) -> Optional[Post]:
    """Create ONE fresh post of the given type. Returns the Post or None."""
    logger.info("=== FRESH === Creating single post type=%s", content_type)

    async with async_session() as session:
        if content_type == "web_news":
            post = await _create_web_news_post(session)
            if post:
                return post
            logger.info("=== FRESH === No web news, falling back to poi_spotlight")
            content_type = "poi_spotlight"

        if content_type == "poi_spotlight":
            post = await _create_poi_spotlight_post(session)
            if post:
                return post
            logger.info("=== FRESH === POI unavailable, falling back to leisure_travel")
            content_type = "leisure_travel"

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
                post.log_pipeline("topic", "ok", f"RSS: {source_name} — {entry['title'][:120]}")
            else:
                logger.info("=== FRESH === No RSS news, falling back to leisure_travel")
                content_type = "leisure_travel"

        if content_type == "active_travel":
            topic = await _pick_unique_topic(session, ACTIVE_DIRECTIONS, "active_travel", recent_titles)
            post = Post(title=topic[:200], content_raw=topic, source="ai")
            post.log_pipeline("topic", "ok", f"AI active_travel: {topic[:120]}")

        elif content_type == "leisure_travel":
            topic = await _pick_unique_topic(session, LEISURE_DIRECTIONS, "leisure_travel", recent_titles)
            post = Post(title=topic[:200], content_raw=topic, source="ai")
            post.log_pipeline("topic", "ok", f"AI leisure_travel: {topic[:120]}")

        elif content_type == "feature":
            today_start_utc = get_today_start_utc()
            result = await session.execute(
                select(Post.title).where(Post.created_at >= today_start_utc, Post.title.isnot(None)).limit(5)
            )
            recent_today = [r[0] for r in result.all() if r[0]]
            travel_context = random.choice(recent_today) if recent_today else ""
            topic = await _pick_feature_topic(session, FEATURE_DIRECTIONS, recent_titles, travel_context)
            post = Post(title=topic[:200], content_raw=topic, source="ai")
            post.log_pipeline("topic", "ok", f"AI feature: {topic[:120]}")

        if not post:
            logger.warning("=== FRESH === Failed to create post type=%s", content_type)
            return None

        session.add(post)
        await session.flush()

        for platform in ALL_PLATFORMS:
            session.add(Publication(post_id=post.id, platform=platform.value))

        try:
            tr = await translate_post(post.title or "", post.content_raw or "")
            if tr:
                post.translations = _json.dumps(tr, ensure_ascii=False)
                post.log_pipeline("translate", "ok", f"{len(tr)} languages")
        except Exception as e:
            logger.warning("Translation failed for post_id=%s", post.id)
            post.log_pipeline("translate", "fail", str(e)[:200])

        await session.commit()
        logger.info("=== FRESH === Created post_id=%d type=%s title='%s'",
                     post.id, content_type, (post.title or "")[:50])
        return post
