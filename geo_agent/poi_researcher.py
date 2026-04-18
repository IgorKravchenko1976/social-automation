"""POI research processor — generates web-sourced research content for POI points.

Flow: fetch enriched POI → web search (Perplexity/Tavily/Brave) → parse blocks → translate → submit.
Each block has real source URLs for verification.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from content.ai_client import get_client
from content.perplexity_client import research_place, parse_research_json, is_configured as perplexity_configured
from content import web_search
from geo_agent import backend_client
from geo_agent.translator import translate_content

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
DAILY_LIMIT = 50


SUMMARIZE_SYSTEM_PROMPT = """You are a travel researcher. You are given web search results about a SPECIFIC VENUE/ESTABLISHMENT.
Create a structured JSON summary using ONLY information from the provided search results.

Return ONLY valid JSON:
{
  "summary": "2-3 sentence overview",
  "history": "Historical background (if found in sources)",
  "cuisine_info": "About the cuisine/food (if applicable)",
  "person_info": "About the person the place is named after (if found)",
  "cultural_context": "Cultural significance",
  "practical_tips": "Tips for visitors",
  "fun_facts": "Interesting facts from sources"
}

Rules:
1. ONLY use facts from the provided search results — never invent
2. If a field has no data in sources, set it to ""
3. Write in Ukrainian language
4. Include specific details: dates, names, numbers
5. Return ONLY valid JSON

CRITICAL — NAME vs CONTENT:
The place NAME is just a brand/label for the venue (bar, restaurant, gym, shop, monument).
You MUST write about THE VENUE ITSELF — NOT about the literal meaning of its name.
Example: if the bar is called "Лось" (Moose), write about the bar, NOT about the moose animal.
If search results contain info about the literal name meaning (animal, person, concept) instead of the venue — IGNORE those results and return empty fields."""


async def process_poi_research() -> bool:
    """Process one POI for research. Called by scheduler every 5 min."""
    async with _lock:
        return await _process_poi_research_inner()


async def _process_poi_research_inner() -> bool:
    if not backend_client.is_configured():
        return False

    try:
        poi = await backend_client.fetch_next_poi_for_research()
    except Exception as e:
        logger.warning("[poi-researcher] Failed to fetch next POI: %s", e)
        return False

    if poi is None:
        logger.debug("[poi-researcher] No POI available for research")
        return False

    logger.info("[poi-researcher] Researching POI %d: %s (%s, %s)",
                poi.point_id, poi.name, poi.city, poi.country_code)

    blocks = await _research_poi(poi)
    if not blocks:
        await backend_client.mark_poi_researched(poi.point_id, 0)
        logger.info("[poi-researcher] POI %d: no research data found", poi.point_id)
        return True

    translated_blocks = await _translate_blocks(blocks)

    try:
        resp = await backend_client.submit_poi_research(poi.point_id, translated_blocks)
        saved = resp.get("saved", 0)
        logger.info("[poi-researcher] POI %d: submitted %d research blocks", poi.point_id, saved)
    except Exception as e:
        logger.error("[poi-researcher] Failed to submit research for POI %d: %s", poi.point_id, e)
        await backend_client.mark_poi_researched(poi.point_id, 0)

    return True


async def _research_poi(poi: backend_client.POIResearchTask) -> list[dict]:
    """Research a POI using the web search fallback chain.

    Returns a list of block dicts ready for submission.
    """
    research_data = None
    sources: list[dict] = []
    ai_provider = ""

    # Strategy 1: Perplexity Sonar (best — has built-in web search + citations)
    if perplexity_configured():
        extra_ctx = ""
        if poi.description:
            extra_ctx = poi.description[:300]

        result = await research_place(
            name=poi.name,
            city=poi.city,
            country=poi.country_code,
            point_type=poi.point_type,
            extra_context=extra_ctx,
        )
        if result and result.content:
            research_data = parse_research_json(result)
            if research_data is None and len(result.content) > 50:
                research_data = {"summary": result.content}
            sources = [{"url": url, "title": "", "snippet": ""} for url in result.citations[:10]]
            ai_provider = "perplexity"
            logger.info("[poi-researcher] Perplexity found %d citations for POI %d (json=%s)",
                        len(result.citations), poi.point_id, research_data is not None and "summary" not in research_data)

    # Strategy 2: Tavily/Brave search + GPT-4o-mini summarization
    if research_data is None:
        query = f"{poi.name} {poi.city} {poi.country_code}".strip()
        search_resp = await web_search.search(query)

        if search_resp and search_resp.results:
            sources = [
                {"url": r.url, "title": r.title, "snippet": r.content[:200]}
                for r in search_resp.results
            ]
            ai_provider = f"{search_resp.provider}+gpt"

            context = web_search.format_search_context(search_resp)
            research_data = await _summarize_with_gpt(poi.name, poi.city, poi.point_type, context)
            logger.info("[poi-researcher] %s found %d results for POI %d",
                        search_resp.provider, len(search_resp.results), poi.point_id)

    # Strategy 3: GPT-4o-mini knowledge only (lowest confidence)
    if research_data is None:
        research_data = await _gpt_knowledge_only(poi)
        ai_provider = "gpt_only"
        logger.info("[poi-researcher] Using GPT knowledge only for POI %d", poi.point_id)

    if research_data is None:
        return []

    if _is_name_confusion(research_data, poi):
        logger.warning(
            "[poi-researcher] REJECTED POI %d (%s): content is about the literal name meaning, not the venue",
            poi.point_id, poi.name,
        )
        return []

    return _build_blocks(research_data, sources, ai_provider, poi)


_NAME_CONFUSION_MARKERS = [
    "тварина", "ссавець", "птах", "комаха", "рослина", "дерево", "квітка",
    "вид роду", "клас ", "ряд ", "родина ", "царство ",
    "(alces", "(cervus", "(ursus", "(canis", "(felis",
    "латинською", "наукова назва",
    "fictional character", "mytholog", "давньогрецьк",
    "множ. мн.:", "(множ.", "(англ.", "біологічн",
]


def _is_name_confusion(data: dict, poi: backend_client.POIResearchTask) -> bool:
    """Detect if AI wrote about the literal name meaning instead of the venue.

    E.g. bar "Лось" → AI wrote about the moose animal instead of the bar.
    """
    summary = (data.get("summary") or "").lower()
    history = (data.get("history") or "").lower()
    combined = f"{summary} {history}"

    if len(combined) < 20:
        return False

    venue_types = {"bar", "restaurant", "cafe", "gym", "shop", "hotel", "club", "бар",
                   "ресторан", "кафе", "зал", "магазин", "готель", "клуб", "заклад"}
    has_venue_mention = any(vt in combined for vt in venue_types)

    confusion_hits = sum(1 for marker in _NAME_CONFUSION_MARKERS if marker in combined)

    if confusion_hits >= 2 and not has_venue_mention:
        return True

    if confusion_hits >= 1 and not has_venue_mention:
        poi_type = poi.point_type.lower().replace("_", " ")
        if poi_type in ("bar", "restaurant", "cafe", "fast food", "gym", "shop", "hotel"):
            return True

    return False


def _build_blocks(
    data: dict,
    sources: list[dict],
    ai_provider: str,
    poi: backend_client.POIResearchTask,
) -> list[dict]:
    """Convert research data dict into typed blocks for submission."""
    blocks: list[dict] = []

    has_sources = len(sources) > 0
    base_confidence = 0.9 if ai_provider == "perplexity" else (0.7 if "gpt" in ai_provider and has_sources else 0.4)

    field_map = {
        "summary": ("summary", "Огляд"),
        "history": ("history", "Історія"),
        "cuisine_info": ("cuisine", "Кухня"),
        "person_info": ("person", "Персона"),
        "cultural_context": ("cultural", "Культурний контекст"),
        "practical_tips": ("practical", "Практична інформація"),
        "fun_facts": ("fun_facts", "Цікаві факти"),
    }

    for data_key, (block_type, default_title) in field_map.items():
        content = data.get(data_key, "")
        if not content or not isinstance(content, str) or len(content.strip()) < 10:
            continue

        blocks.append({
            "blockType": block_type,
            "title": default_title,
            "content": content.strip(),
            "sources": sources,
            "aiProvider": ai_provider,
            "confidence": base_confidence,
        })

    if not blocks:
        logger.warning("[poi-researcher] No blocks built from data keys: %s",
                       {k: len(str(v)) for k, v in data.items() if v})

    return blocks


async def _summarize_with_gpt(name: str, city: str, point_type: str, search_context: str) -> dict | None:
    """Summarize web search results using GPT-4o-mini."""
    client = get_client()

    type_label = point_type.replace("_", " ")
    user_prompt = (
        f"Venue: \"{name}\" (a {type_label} in {city})\n\n"
        f"IMPORTANT: \"{name}\" is the NAME of a {type_label}. "
        f"Write about the venue itself, NOT about the literal meaning of the word \"{name}\".\n\n"
        f"Web search results:\n{search_context}\n\n"
        "Summarize the above search results into structured JSON about this specific venue."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        return json.loads(raw)

    except Exception as e:
        logger.error("[poi-researcher] GPT summarization failed: %s", e)
        return None


async def _gpt_knowledge_only(poi: backend_client.POIResearchTask) -> dict | None:
    """Last resort: use GPT knowledge without web search (lowest confidence)."""
    client = get_client()

    type_label = poi.point_type.replace("_", " ")
    user_prompt = (
        f"Venue: \"{poi.name}\" (a {type_label})\n"
        f"City: {poi.city}\n"
        f"Country: {poi.country_code}\n"
    )
    if poi.description:
        user_prompt += f"Existing description: {poi.description[:500]}\n"

    user_prompt += (
        f"\nIMPORTANT: \"{poi.name}\" is the NAME of a {type_label}. "
        f"Write about the venue itself, NOT about the literal meaning of the word \"{poi.name}\".\n"
        "Tell me what you know about this specific venue. "
        "Only include facts you are confident about. "
        "If you don't know anything about this specific venue, return all fields as empty strings."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1500,
            temperature=0.4,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        return json.loads(raw)

    except Exception as e:
        logger.error("[poi-researcher] GPT knowledge-only failed: %s", e)
        return None


async def _translate_blocks(blocks: list[dict]) -> list[dict]:
    """Translate block content to 8 languages using the existing translator."""
    for block in blocks:
        title = block.get("title", "")
        content = block.get("content", "")
        if not content:
            continue

        try:
            translations = await translate_content(
                title[:200],
                content[:2000],
                source_lang="uk",
            )
            block["contentTranslations"] = translations
        except Exception as e:
            logger.warning("[poi-researcher] Translation failed for block %s: %s",
                           block.get("blockType"), e)
            block["contentTranslations"] = {}

    return blocks
