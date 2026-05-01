"""City Pulse description enrichment.

Fills in rich descriptions for events that arrived from sources with sparse
text (e.g. "Concert in two parts."). Pipeline per cycle:

  1. Pull one job from backend (oldest pending upcoming event with low
     enrichment_attempts).
  2. Ask Perplexity Sonar for facts about (title, venue, city, country,
     ticket URL, source homepage). Sonar uses real web search and returns
     citations, so we get verifiable detail rather than GPT hallucinations.
  3. Hand the Sonar text to GPT-4o-mini and tell it to compose a tight
     180-600 character description in the original event language plus the
     other 7 supported languages. We also pull structured metadata
     (programme, artists, duration, transport, what_to_bring) when present.
  4. POST the result to backend, which merges into city_events.

The whole cycle is rate-limited to one event per 3-minute scheduler tick,
so we never exhaust Perplexity / OpenAI quotas. Backend handles retries
and final 'failed' state after 3 attempts.
"""
from __future__ import annotations

import asyncio
import json
import logging

from openai import AsyncOpenAI

from config.settings import settings
from content.perplexity_client import get_perplexity_client
from geo_agent import backend_client

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

_SUPPORTED_LANGS = ("uk", "en", "de", "fr", "es", "it", "el", "ru")

_RESEARCH_PROMPT_TEMPLATE = """You are a cultural-events researcher. Use web search to find concrete, verifiable details about ONE specific event. Focus on: programme / set list, performers / cast, what is exhibited, theme, target audience, languages spoken on stage, duration, dress code, age limit, transport (metro / bus / parking), nearby landmarks. Use ONLY information that appears in your search results — never invent dates, names, prices, or venue facts. If the event itself has almost no online presence, focus on the venue and the genre.

Return STRICT JSON with this shape (no commentary, no markdown):
{
  "summary": "2-4 sentence overview in the source language (__LANG__).",
  "programme": "Full programme / set list / works performed / lots / screenings (string, can be multi-line).",
  "artists": ["Performer or author names if known"],
  "audience": "Who is this for: families, students, opera fans, etc.",
  "duration_minutes": 120,
  "transport": "Nearest metro / bus / parking, in the source language.",
  "what_to_bring": "Dress code, ID, smart casual, comfortable shoes, etc.",
  "age_limit": null,
  "facilities": {"parking": true, "wheelchair": false, "wifi": false},
  "thumbnail_url": "Full-resolution poster/photo URL if you can verify one",
  "sources": ["https://...", "https://..."]
}

If a field has no verifiable info, use "" or null or [] — DO NOT invent.

Sources policy: never cite Russian or Belarusian domains (.ru, .by, vk.com, ok.ru, yandex.*, mail.ru, ria.ru, tass, rt.com, sputnik, etc.). If the event is in occupied Ukrainian territory or in Russia/Belarus, return all fields empty."""


_REWRITE_PROMPT_TEMPLATE = """You compose tight, factual cultural-event descriptions for a travel app.
Input: a JSON research blob with summary/programme/artists etc. about ONE event.
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
  "meta": {
    "programme": "…",
    "artists": ["…"],
    "audience": "…",
    "duration_minutes": 0,
    "sources": ["https://…"]
  },
  "facilities": {"parking": true},
  "transport_info": "…",
  "what_to_bring": "…"
}

Rules:
- description MUST be 180-600 chars. If research is thin, describe the format (concert in two parts at venue X, organ recital, classical programme) and venue context — never copy the input stub like 'Concert in two parts.' verbatim.
- Translations MUST cover ALL 8 languages: uk, en, de, fr, es, it, el, ru.
- Stay factual: only use facts present in the research blob.
- If a meta field is unknown, omit it (do not invent).
- Return ONLY JSON."""


_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _openai_client() -> AsyncOpenAI | None:
    if not settings.openai_api_key:
        return None
    return AsyncOpenAI(api_key=settings.openai_api_key)


async def _perplexity_research(job: backend_client.CityPulseEnrichmentJob) -> dict:
    """Ask Perplexity Sonar for facts about the event. Returns parsed JSON dict."""
    client = get_perplexity_client()
    if client is None:
        logger.info("[city-pulse-enrich] Perplexity not configured — skipping research")
        return {}

    src_lang = job.content_language or "en"
    parts = [f'Event title: "{job.title}"']
    if job.venue_name:
        parts.append(f'Venue: "{job.venue_name}"')
    if job.venue_address:
        parts.append(f"Address: {job.venue_address}")
    if job.city:
        parts.append(f"City: {job.city}")
    if job.country_code:
        parts.append(f"Country code: {job.country_code}")
    if job.starts_at:
        parts.append(f"Starts at (UTC): {job.starts_at}")
    if job.category:
        parts.append(f"Category: {job.category}")
    if job.ticket_url:
        parts.append(f"Tickets / official page: {job.ticket_url}")
    if job.source_homepage_url:
        parts.append(f"Source site: {job.source_homepage_url}")
    if job.source_name:
        parts.append(f"Source name: {job.source_name}")
    parts.append(f"Existing one-line description: {job.description!r}")
    user_prompt = (
        "Research the following event using web search. Read the official site, "
        "the venue's site, ticket pages, and reputable cultural-listing sites. "
        "Return ONLY the JSON described in the system prompt.\n\n"
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
        logger.exception("[city-pulse-enrich] Perplexity call failed for %d", job.id)
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
    job: backend_client.CityPulseEnrichmentJob,
    research: dict,
) -> dict:
    """Hand research blob to GPT-4o-mini → return final enrichment dict."""
    client = _openai_client()
    if client is None:
        logger.info("[city-pulse-enrich] OpenAI not configured — skipping compose")
        return {}

    src_lang = job.content_language or "en"
    user_msg = json.dumps(
        {
            "event": {
                "id": job.id,
                "title": job.title,
                "category": job.category,
                "venue": job.venue_name,
                "city": job.city,
                "country_code": job.country_code,
                "starts_at": job.starts_at,
                "ticket_url": job.ticket_url,
                "source_homepage_url": job.source_homepage_url,
                "source_name": job.source_name,
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
        logger.exception("[city-pulse-enrich] GPT compose failed for %d", job.id)
        return {}

    content = (resp.choices[0].message.content or "").strip()
    parsed = _safe_load_json(content)
    return parsed or {}


def _safe_load_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        first = text.find("\n")
        if first > 0:
            text = text[first + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        # Sometimes the model adds prose before/after; try to grab the outermost {}.
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start: end + 1])
        except Exception:
            return None
    return None


def _normalise_translations(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for lang, val in raw.items():
        if lang not in _SUPPORTED_LANGS:
            continue
        if not isinstance(val, str):
            continue
        v = val.strip()
        if len(v) < 60:
            continue
        out[lang] = v
    return out


def _normalise_meta(raw: dict | None, research: dict | None) -> dict:
    out: dict = {}
    if isinstance(raw, dict):
        for k in ("programme", "artists", "audience", "duration_minutes", "sources", "thumbnail_url"):
            if k in raw and raw[k] not in (None, "", [], {}):
                out[k] = raw[k]
    if isinstance(research, dict):
        sources = research.get("sources") or []
        if sources and "sources" not in out:
            out["sources"] = sources
    return out


async def process_city_pulse_enrich() -> bool:
    """One enrichment cycle. Returns True if an event was enriched."""
    async with _lock:
        if not backend_client.is_configured():
            return False

        try:
            job = await backend_client.fetch_next_enrichment_job()
        except Exception as exc:
            logger.warning("[city-pulse-enrich] fetch job failed: %s", exc)
            return False
        if job is None:
            return False

        logger.info(
            "[city-pulse-enrich] event=%d '%s' @ %s/%s — researching",
            job.id, job.title[:60], job.venue_name[:40], job.city,
        )

        research = await _perplexity_research(job)
        composed = await _gpt_compose(job, research)

        description = ""
        if isinstance(composed.get("description"), str):
            description = composed["description"].strip()

        if len(description) < 100:
            logger.info(
                "[city-pulse-enrich] event=%d — description too short (%d), marking failed",
                job.id, len(description),
            )
            await backend_client.submit_enrichment(
                job.id, failed=True,
                fail_reason="composer returned <100 char description",
            )
            return False

        translations = _normalise_translations(composed.get("translations"))
        meta = _normalise_meta(composed.get("meta"), research)
        facilities = composed.get("facilities") if isinstance(composed.get("facilities"), dict) else {}
        transport_info = (composed.get("transport_info") or "").strip()
        what_to_bring = (composed.get("what_to_bring") or "").strip()
        thumbnail_url = ""
        if isinstance(research.get("thumbnail_url"), str):
            thumbnail_url = research["thumbnail_url"].strip()

        ok = await backend_client.submit_enrichment(
            job.id,
            description=description,
            translations=translations,
            meta=meta,
            facilities=facilities,
            transport_info=transport_info,
            what_to_bring=what_to_bring,
            thumbnail_url=thumbnail_url,
        )
        if ok:
            logger.info(
                "[city-pulse-enrich] event=%d enriched — desc=%d chars, +%d translations",
                job.id, len(description), len(translations),
            )
        else:
            logger.warning("[city-pulse-enrich] event=%d submit failed", job.id)
        return ok
