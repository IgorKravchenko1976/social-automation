"""Fresh travel news discovery via Perplexity Sonar web search.

Searches for current travel news, parses structured items with source URLs,
deduplicates against already-published posts, and filters banned territories.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from config.settings import settings, get_now_local
from content.perplexity_client import get_perplexity_client, is_configured
from content.tourism_topics import contains_blocked_territory, BANNED_RSS_KEYWORDS
from db.database import async_session
from db.models import Post

logger = logging.getLogger(__name__)

SEARCH_QUERIES = [
    "latest travel news today {date}",
    "new airline routes Europe {year}",
    "tourism events this week {date}",
    "Ukraine tourism news {year}",
    "travel warnings {month} {year}",
    "new hotel resort opening {year}",
    "festival Europe {month} {year}",
    "visa changes for tourists {year}",
    "cruise news Mediterranean {year}",
    "airport news Europe {month} {year}",
    "cheap flights deals Europe {date}",
    "travel trends {month} {year}",
    "national park reopening {year}",
    "UNESCO world heritage news {year}",
    "train routes Europe new {year}",
]

NEWS_SEARCH_SYSTEM_PROMPT = """\
You are a travel news researcher. Search for the LATEST travel news published TODAY or in the last 2-3 days.

Return ONLY valid JSON with this structure:
{
  "news": [
    {
      "title": "Short headline in English",
      "summary": "2-4 sentence summary with key facts: WHO, WHAT, WHERE, WHEN",
      "source_name": "Name of the media outlet (e.g. Reuters, CNN Travel)",
      "source_url": "Direct URL to the original article",
      "date": "Publication date YYYY-MM-DD",
      "location": "City, Country or region mentioned",
      "category": "airline|hotel|visa|event|warning|infrastructure|deal|other"
    }
  ]
}

CRITICAL RULES:
1. ONLY include news from the LAST 3 DAYS. Nothing older.
2. Every item MUST have a real source_url from a real media outlet.
3. Every item MUST have an exact date. If unsure — skip the item.
4. NEVER invent news. If you find nothing fresh — return {"news": []}.
5. Prefer news useful for travelers: new routes, visa changes, events, warnings, deals.
6. EXCLUDE: politics, wars, sanctions, Russia, Belarus, North Korea, Iran, Syria.
7. EXCLUDE: celebrity gossip, opinion pieces, listicles without news value.
8. Return 3-5 items maximum, sorted by relevance to travelers.
9. Write summaries in English (translation to Ukrainian happens later).
"""


@dataclass
class NewsItem:
    title: str = ""
    summary: str = ""
    source_name: str = ""
    source_url: str = ""
    date: str = ""
    location: str = ""
    category: str = "other"


@dataclass
class NewsSearchResult:
    items: list[NewsItem] = field(default_factory=list)
    query_used: str = ""


def _build_query() -> str:
    """Pick a random search query template and fill in date placeholders."""
    now = get_now_local()
    template = random.choice(SEARCH_QUERIES)
    return template.format(
        date=now.strftime("%d %B %Y"),
        month=now.strftime("%B"),
        year=now.strftime("%Y"),
    )


async def _get_published_urls(limit: int = 200) -> set[str]:
    """Fetch recently published source URLs to avoid duplicates."""
    async with async_session() as session:
        result = await session.execute(
            select(Post.source_url)
            .where(Post.source.in_(["web_news", "rss"]), Post.source_url.isnot(None))
            .order_by(Post.created_at.desc())
            .limit(limit)
        )
        return {row[0] for row in result.all() if row[0]}


def _is_banned_news(item: NewsItem) -> bool:
    """Check if a news item mentions banned territories or topics."""
    text = f"{item.title} {item.summary} {item.location}".lower()
    if contains_blocked_territory(text):
        return True
    for kw in BANNED_RSS_KEYWORDS:
        if kw in text:
            return True
    return False


def _parse_news_response(content: str) -> list[NewsItem]:
    """Parse Perplexity JSON response into NewsItem list."""
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("[web-news] Failed to parse JSON: %s", content[:200])
        return []

    raw_items = data.get("news", [])
    if not isinstance(raw_items, list):
        return []

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = (raw.get("title") or "").strip()
        source_url = (raw.get("source_url") or "").strip()
        if not title or not source_url or not source_url.startswith("http"):
            continue
        items.append(NewsItem(
            title=title,
            summary=(raw.get("summary") or "").strip(),
            source_name=(raw.get("source_name") or "").strip(),
            source_url=source_url,
            date=(raw.get("date") or "").strip(),
            location=(raw.get("location") or "").strip(),
            category=(raw.get("category") or "other").strip(),
        ))

    return items


async def search_fresh_travel_news() -> Optional[NewsItem]:
    """Search for one fresh travel news item via Perplexity.

    Returns the best unused, non-banned news item, or None if nothing found.
    """
    if not is_configured():
        logger.debug("[web-news] Perplexity not configured, skipping")
        return None

    client = get_perplexity_client()
    if client is None:
        return None

    query = _build_query()
    logger.info("[web-news] Searching: %s", query)

    try:
        response = await client.chat.completions.create(
            model="sonar",
            messages=[
                {"role": "system", "content": NEWS_SEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            max_tokens=2000,
            temperature=0.2,
        )

        content = response.choices[0].message.content.strip()
        citations = getattr(response, "citations", []) or []

        items = _parse_news_response(content)
        if not items:
            logger.info("[web-news] No news items found for query: %s", query)
            return None

        logger.info("[web-news] Found %d items, %d citations", len(items), len(citations))

        published_urls = await _get_published_urls()

        for item in items:
            if item.source_url in published_urls:
                logger.debug("[web-news] Skipping duplicate: %s", item.source_url[:80])
                continue
            if _is_banned_news(item):
                logger.info("[web-news] Skipping banned: %s", item.title[:60])
                continue
            if not item.summary or len(item.summary) < 20:
                continue

            logger.info(
                "[web-news] Selected: '%s' from %s (%s)",
                item.title[:60], item.source_name, item.date,
            )
            return item

        logger.info("[web-news] All items filtered out (duplicates/banned)")
        return None

    except Exception as e:
        logger.warning("[web-news] Perplexity search failed: %s", e)
        return None


def format_news_for_ai(item: NewsItem) -> str:
    """Format a news item as structured input for GPT post generation."""
    parts = [
        "=== РЕАЛЬНА НОВИНА З ПЕРЕВІРЕНОГО ДЖЕРЕЛА ===",
        f"Заголовок: {item.title}",
        f"Зміст: {item.summary}",
    ]
    if item.location:
        parts.append(f"Місцевість: {item.location}")
    if item.date:
        parts.append(f"Дата публікації: {item.date}")
    parts.append(f"Джерело: {item.source_name}")
    parts.append(f"URL джерела: {item.source_url}")
    parts.append(f"Категорія: {item.category}")
    parts.append("")
    parts.append("ДЖЕРЕЛО ДАНИХ: " + (item.source_name or "web search"))

    return "\n".join(parts)
