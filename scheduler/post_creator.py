"""Post creation: daily batch and single-post generation from RSS/AI."""
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
from db.database import async_session
from db.models import Post, Publication

logger = logging.getLogger(__name__)

ALL_PLATFORMS = configured_platforms()

SLOT_CONTENT_TYPES = ["tourism_news", "active_travel", "leisure_travel", "tourism_news", "feature"]

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
    """Generate 5 posts for today:
    - 2 tourism news (from RSS, priority Ukraine; fallback = leisure travel)
    - 1 active sports/events (tied to location)
    - 1 leisure travel (places, culture, gastro)
    - 1 app feature (tied to one of today's travel topics)
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
            try:
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

        try:
            tr = await translate_post(post.title or "", post.content_raw or "")
            if tr:
                post.translations = _json.dumps(tr, ensure_ascii=False)
        except Exception:
            logger.warning("Translation failed for post_id=%s", post.id)

        await session.commit()
        logger.info("=== FRESH === Created post_id=%d type=%s title='%s'",
                     post.id, content_type, (post.title or "")[:50])
        return post
