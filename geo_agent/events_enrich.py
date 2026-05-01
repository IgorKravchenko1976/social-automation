"""Researcher-event description enrichment.

Researcher-created events (user_id=82) sometimes ship with Wikipedia
disambiguation text ("Barry's may refer to: a fitness brand, a tea
company, an amusement park") instead of a description of the actual
venue at the given coordinates. Pipeline per cycle:

  1. Pull one job from backend (researcher event with shortest description
     and lowest enrichment_attempts).
  2. Ask Perplexity Sonar for facts about the SPECIFIC venue at
     (title, lat/lng, city, country, facility_type). The system prompt
     hammers home that NAME ≠ CONTENT — write about the gym at this
     address, not about every brand that shares the name.
  3. Hand the Sonar text to GPT-4o-mini and tell it to compose a tight
     180-600 character description in the original event language plus
     the other 7 supported languages.
  4. POST the result to backend, which UPDATEs in place (no new entity
     version — same content, just better text).
"""
from __future__ import annotations

import asyncio
import json
import logging

from openai import AsyncOpenAI

from config.settings import settings
from content.perplexity_client import get_perplexity_client
from geo_agent import backend_client
from geo_agent.city_pulse_enrich import _safe_load_json, _normalise_translations

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


_RESEARCH_PROMPT_TEMPLATE = """You are a venue researcher. Use web search to find concrete, verifiable facts about ONE SPECIFIC venue at a given address. Focus on what the venue is, what it offers, atmosphere, history, who runs it, signature features, recent reviews. Use ONLY information from your search results — never invent.

Return STRICT JSON with this shape (no commentary, no markdown):
{
  "summary": "3-5 sentence overview in the source language (__LANG__).",
  "venue_type": "What kind of place this actually is (gym, cafe, museum, monument, ...)",
  "what_to_expect": "Atmosphere, signature features, who comes here.",
  "history": "Founding date, owners, notable events (if known).",
  "tips": "Practical tips for visitors.",
  "address": "Street address as confirmed by your sources, if any.",
  "sources": ["https://...", "https://..."]
}

If a field has no verifiable info, use "" or [] — DO NOT invent.

CRITICAL — NAME vs CONTENT:
The place NAME is just a brand/label. You MUST research THE VENUE AT THE GIVEN COORDINATES, not unrelated things that share the name. Example: 'Barry's — San Francisco' at lat 37.79, lng -122.40 is the BARRY'S BOOTCAMP fitness studio in SF — write about THAT studio, NOT a Wikipedia disambiguation about every brand named Barry's. If your search keeps returning unrelated meanings of the name, narrow the search by adding the city, address, and venue_type from the prompt — e.g. 'Barry's gym San Francisco SOMA'. If after that you still cannot find anything specific, return all fields empty rather than ship a disambiguation.

Sources policy: never cite Russian or Belarusian domains (.ru, .by, vk.com, ok.ru, yandex.*, mail.ru, ria.ru, tass, rt.com, sputnik, etc.). If the venue is in Russia, Belarus, or in occupied Ukrainian territory, return all fields empty."""


_REWRITE_PROMPT_TEMPLATE = """You compose tight, factual venue descriptions for a travel app.
Input: a JSON research blob with summary/venue_type/what_to_expect/history/tips about ONE venue.
Output: STRICT JSON with this shape (no commentary, no markdown):
{
  "description": "180-600 char description in __SOURCE_LANG__",
  "translations": {
    "uk": "…",
    "en": "…",
    "de": "…",
    "fr": "…",
    "es": "…",
    "it": "…",
    "el": "…",
    "ru": "…"
  },
  "address": "…",
  "city": "…",
  "country": "…"
}

Rules:
- description MUST be 180-600 chars and describe THIS venue at the given coordinates.
- NEVER write a disambiguation ("X may refer to: a, b, c") — those are rejected.
- Translations MUST cover ALL 8 languages.
- Stay factual: only use facts present in the research blob.
- If research is empty/disambiguation, return description="" so backend marks failed.
- Return ONLY JSON."""


_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _openai_client() -> AsyncOpenAI | None:
    if not settings.openai_api_key:
        return None
    return AsyncOpenAI(api_key=settings.openai_api_key)


async def _perplexity_research(job: backend_client.EventEnrichmentJob) -> dict:
    """Ask Perplexity Sonar for facts about the venue. Returns parsed JSON dict."""
    client = get_perplexity_client()
    if client is None:
        return {}

    src_lang = job.content_language or "uk"

    parts = [f'Venue name: "{job.title}"']
    if job.facility_type:
        parts.append(f"Venue type hint: {job.facility_type}")
    if job.address:
        parts.append(f"Address: {job.address}")
    if job.city:
        parts.append(f"City: {job.city}")
    if job.country:
        parts.append(f"Country: {job.country}")
    parts.append(f"Coordinates: {job.latitude:.6f}, {job.longitude:.6f}")
    if job.description:
        parts.append(f"Existing (often wrong / disambiguation) description: {job.description!r}")

    user_prompt = (
        "Research the following SPECIFIC venue. Read its official site, Yelp, "
        "Google Maps, Tripadvisor, OSM, and reputable local listings. Return "
        "ONLY the JSON described in the system prompt. If the existing "
        "description looks like a Wikipedia disambiguation, IGNORE it and "
        "search for the actual venue at the coordinates instead.\n\n"
        + "\n".join(parts)
    )

    try:
        resp = await client.chat.completions.create(
            model="sonar",
            temperature=0.2,
            messages=[
                {"role": "system", "content": _RESEARCH_PROMPT_TEMPLATE.replace("__LANG__", src_lang)},
                {"role": "user", "content": user_prompt},
            ],
            timeout=60,
        )
    except Exception:
        logger.exception("[events-enrich] Perplexity call failed for %d", job.id)
        return {}

    content = (resp.choices[0].message.content or "").strip()
    if not content:
        return {}
    raw_resp = resp.model_dump() if hasattr(resp, "model_dump") else {}
    citations = raw_resp.get("citations") or []
    parsed = _safe_load_json(content)
    if not parsed:
        return {}
    if citations and not parsed.get("sources"):
        parsed["sources"] = citations
    return parsed


async def _gpt_compose(
    job: backend_client.EventEnrichmentJob,
    research: dict,
) -> dict:
    client = _openai_client()
    if client is None:
        return {}

    src_lang = job.content_language or "uk"
    user_msg = json.dumps(
        {
            "event": {
                "id": job.id,
                "title": job.title,
                "facility_type": job.facility_type,
                "latitude": job.latitude,
                "longitude": job.longitude,
                "city": job.city,
                "country": job.country,
                "address": job.address,
                "current_description": job.description,
            },
            "research": research,
        },
        ensure_ascii=False,
    )

    try:
        resp = await client.chat.completions.create(
            model=_DEFAULT_OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _REWRITE_PROMPT_TEMPLATE.replace("__SOURCE_LANG__", src_lang)},
                {"role": "user", "content": user_msg},
            ],
            timeout=60,
        )
    except Exception:
        logger.exception("[events-enrich] GPT compose failed for %d", job.id)
        return {}

    content = (resp.choices[0].message.content or "").strip()
    return _safe_load_json(content) or {}


def _looks_like_disambiguation(text: str) -> bool:
    """Reject obvious 'X may refer to:' descriptions."""
    if not text:
        return False
    head = text[:200].lower()
    markers = ("may refer to", "refers to:", "is a list of", "may also refer")
    return any(m in head for m in markers)


async def process_event_enrich() -> bool:
    """One enrichment cycle. Returns True if an event was enriched."""
    async with _lock:
        if not backend_client.is_configured():
            return False

        try:
            job = await backend_client.fetch_next_event_enrichment_job()
        except Exception as exc:
            logger.warning("[events-enrich] fetch job failed: %s", exc)
            return False
        if job is None:
            return False

        logger.info(
            "[events-enrich] event=%d '%s' @ %.4f,%.4f (%s/%s) — researching",
            job.id, job.title[:60], job.latitude, job.longitude,
            job.city or "?", job.country or "?",
        )

        research = await _perplexity_research(job)
        composed = await _gpt_compose(job, research)

        description = ""
        if isinstance(composed.get("description"), str):
            description = composed["description"].strip()

        if len(description) < 100:
            logger.info(
                "[events-enrich] event=%d — description too short (%d), marking failed",
                job.id, len(description),
            )
            await backend_client.submit_event_enrichment(
                job.id, failed=True,
                fail_reason="composer returned <100 char description",
            )
            return False

        if _looks_like_disambiguation(description):
            logger.info("[events-enrich] event=%d — composer returned disambiguation, marking failed", job.id)
            await backend_client.submit_event_enrichment(
                job.id, failed=True,
                fail_reason="composer returned disambiguation",
            )
            return False

        translations = _normalise_translations(composed.get("translations"))
        address = (composed.get("address") or "").strip()
        city = (composed.get("city") or "").strip()
        country = (composed.get("country") or "").strip()

        ok = await backend_client.submit_event_enrichment(
            job.id,
            description=description,
            translations=translations,
            address=address,
            city=city,
            country=country,
        )
        if ok:
            logger.info(
                "[events-enrich] event=%d enriched — desc=%d chars, +%d translations",
                job.id, len(description), len(translations),
            )
        else:
            logger.warning("[events-enrich] event=%d submit failed", job.id)
        return ok
