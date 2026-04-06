"""Fix/update pipeline for existing researcher events.

Two modes:
  1. translate — take existing title+description, translate to all 8 languages
  2. regenerate — (future) re-research and rewrite content with current prompts

Also handles airport name translations backfill.

Runs as a separate scheduled job, batch-limited to avoid OpenAI rate limits.
"""
from __future__ import annotations

import asyncio
import logging

from geo_agent import backend_client
from geo_agent.translator import translate_content, translate_name, translate_research_content

logger = logging.getLogger(__name__)

EVENTS_PER_CYCLE = 5
AIRPORTS_PER_CYCLE = 20
RESEARCH_PER_CYCLE = 5
_lock = asyncio.Lock()


async def fix_events_batch(mode: str = "translate") -> int:
    """Process up to EVENTS_PER_CYCLE events from fix queue.

    Returns the number of events successfully fixed.
    """
    if not backend_client.is_configured():
        return 0

    async with _lock:
        fixed = 0
        for _ in range(EVENTS_PER_CYCLE):
            try:
                event = await backend_client.next_fix_event(mode=mode)
            except Exception as exc:
                logger.warning("[fixer] Failed to fetch next fix event: %s", exc)
                break

            if event is None:
                logger.info("[fixer] No more events in fix queue (mode=%s)", mode)
                break

            event_id = event["eventId"]
            title = event.get("title", "")
            description = event.get("description", "")
            content_lang = event.get("contentLanguage", "")

            if not content_lang:
                content_lang = _detect_source_lang(title, description)

            logger.info(
                "[fixer] Processing event %d (mode=%s, lang=%s): %s",
                event_id, mode, content_lang, title[:60],
            )

            try:
                translations = await translate_content(
                    title, description, source_lang=content_lang,
                )
            except Exception as exc:
                logger.warning("[fixer] Translation failed for event %d: %s", event_id, exc)
                continue

            lang_count = len(translations)
            if lang_count < 2:
                logger.warning("[fixer] Too few translations for event %d: %d", event_id, lang_count)
                continue

            try:
                ok = await backend_client.submit_fix_event(
                    event_id,
                    content_language=content_lang,
                    translations=translations,
                    activate=True,
                )
                if ok:
                    fixed += 1
                    logger.info("[fixer] Fixed event %d (%d languages)", event_id, lang_count)
                else:
                    logger.warning("[fixer] Backend rejected fix for event %d", event_id)
            except Exception as exc:
                logger.warning("[fixer] Submit failed for event %d: %s", event_id, exc)

        logger.info("[fixer] Events batch done: fixed=%d mode=%s", fixed, mode)
        return fixed


async def fix_airports_batch() -> int:
    """Translate names for up to AIRPORTS_PER_CYCLE airports.

    Returns the number of airports successfully fixed.
    """
    if not backend_client.is_configured():
        return 0

    fixed = 0
    for _ in range(AIRPORTS_PER_CYCLE):
        try:
            airport = await backend_client.next_fix_airport()
        except Exception as exc:
            logger.warning("[fixer] Failed to fetch next fix airport: %s", exc)
            break

        if airport is None:
            logger.info("[fixer] No more airports needing name translations")
            break

        airport_id = airport["id"]
        name = airport.get("name", "")

        if not name:
            continue

        try:
            name_trans = await translate_name(name, source_lang="en")
        except Exception as exc:
            logger.warning("[fixer] Name translation failed for airport %d: %s", airport_id, exc)
            continue

        if len(name_trans) < 2:
            continue

        try:
            ok = await backend_client.submit_fix_airport(airport_id, name_trans)
            if ok:
                fixed += 1
                logger.info("[fixer] Fixed airport %d: %s (%d langs)", airport_id, name[:40], len(name_trans))
        except Exception as exc:
            logger.warning("[fixer] Submit failed for airport %d: %s", airport_id, exc)

    logger.info("[fixer] Airports batch done: fixed=%d", fixed)
    return fixed


async def fix_research_batch() -> int:
    """Translate up to RESEARCH_PER_CYCLE geo_research records.

    Returns the number of records successfully fixed.
    """
    if not backend_client.is_configured():
        return 0

    fixed = 0
    for _ in range(RESEARCH_PER_CYCLE):
        try:
            item = await backend_client.next_fix_research()
        except Exception as exc:
            logger.warning("[fixer] Failed to fetch next fix research: %s", exc)
            break

        if item is None:
            logger.info("[fixer] No more research needing translations")
            break

        research_id = item["id"]
        summary = item.get("summary", "")
        content = item.get("content", "")
        content_lang = item.get("contentLanguage", "")

        if not content_lang:
            content_lang = _detect_source_lang(summary, content)

        logger.info(
            "[fixer] Processing research %d (lang=%s): %s",
            research_id, content_lang, summary[:60],
        )

        try:
            translations = await translate_research_content(
                summary, content, source_lang=content_lang,
            )
        except Exception as exc:
            logger.warning("[fixer] Research translation failed for %d: %s", research_id, exc)
            continue

        if len(translations) < 2:
            logger.warning("[fixer] Too few translations for research %d: %d", research_id, len(translations))
            continue

        try:
            ok = await backend_client.submit_fix_research(
                research_id,
                content_language=content_lang,
                translations=translations,
            )
            if ok:
                fixed += 1
                logger.info("[fixer] Fixed research %d (%d languages)", research_id, len(translations))
            else:
                logger.warning("[fixer] Backend rejected fix for research %d", research_id)
        except Exception as exc:
            logger.warning("[fixer] Submit failed for research %d: %s", research_id, exc)

    logger.info("[fixer] Research batch done: fixed=%d", fixed)
    return fixed


async def run_fix_cycle() -> dict:
    """Run one full fix cycle: events + airports + research.

    Called by scheduler on a regular interval.
    """
    events_fixed = await fix_events_batch(mode="translate")
    airports_fixed = await fix_airports_batch()
    research_fixed = await fix_research_batch()
    return {
        "events_fixed": events_fixed,
        "airports_fixed": airports_fixed,
        "research_fixed": research_fixed,
    }


def _detect_source_lang(title: str, description: str) -> str:
    """Simple heuristic to detect if content is Ukrainian or English."""
    text = (title + " " + description).lower()
    cyrillic_count = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    if cyrillic_count > len(text) * 0.3:
        return "uk"
    return "en"
