from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import utcnow_naive
from db.models import RSSSource, Post

logger = logging.getLogger(__name__)


MAX_AGE_HOURS = 48


async def fetch_feed(url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    feed = feedparser.parse(resp.text)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    entries = []
    for entry in feed.entries[:20]:
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

        if published and published < cutoff:
            continue

        entries.append({
            "title": getattr(entry, "title", ""),
            "link": getattr(entry, "link", ""),
            "summary": getattr(entry, "summary", ""),
            "published": published,
        })
    return entries


async def parse_all_sources(session: AsyncSession) -> list[dict]:
    """Fetch all enabled RSS sources and return new entries not yet in DB."""
    result = await session.execute(
        select(RSSSource).where(RSSSource.enabled == True)
    )
    sources = result.scalars().all()

    existing_urls_result = await session.execute(
        select(Post.source_url).where(Post.source == "rss")
    )
    existing_urls = {row[0] for row in existing_urls_result.all() if row[0]}

    new_entries = []
    for source in sources:
        try:
            entries = await fetch_feed(source.url)
            for entry in entries:
                if entry["link"] and entry["link"] not in existing_urls:
                    new_entries.append(entry)
                    existing_urls.add(entry["link"])

            source.last_fetched_at = utcnow_naive()
        except Exception:
            logger.exception("Failed to fetch RSS source %s", source.name)

    await session.commit()
    return new_entries
