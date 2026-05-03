"""Queue processor for airport research tasks.

Fetches airports from imin-backend queue, researches them via AI,
creates events, and reports results back.

Rate-limited to 10 airports per 24 hours, separate from geo research.
Runs on a 2-minute interval via APScheduler.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func

from config.settings import utcnow_naive
from db.database import async_session
from db.models import GeoResearchTask, GeoResearchStatus
from geo_agent import backend_client
from geo_agent.airport_researcher import research_airport
from geo_agent.translator import translate_content, translate_name
from content.media import get_image_for_post, cleanup_media_file

logger = logging.getLogger(__name__)

DAILY_LIMIT = 30
_lock = asyncio.Lock()
_AUDIT_PREFIX = "airport:"


async def _count_airport_processed_last_24h() -> int:
    """Count airport tasks completed in the last 24 hours (local DB audit)."""
    cutoff = utcnow_naive() - timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(GeoResearchTask.id)).where(
                GeoResearchTask.completed_at >= cutoff,
                GeoResearchTask.name.like(f"{_AUDIT_PREFIX}%"),
                GeoResearchTask.status.in_([
                    GeoResearchStatus.COMPLETED,
                    GeoResearchStatus.EMPTY,
                ]),
            )
        )
        return result.scalar() or 0


async def _log_audit(
    iata: str,
    lat: float,
    lng: float,
    result_data: dict | None,
    status: GeoResearchStatus,
    error: str | None = None,
) -> None:
    """Save a record of airport task processing to local DB for audit."""
    try:
        async with async_session() as session:
            now = utcnow_naive()
            task = GeoResearchTask(
                request_id=f"airport_{iata}_{int(now.timestamp())}",
                latitude=lat,
                longitude=lng,
                name=f"{_AUDIT_PREFIX}{iata}",
                language="en",
                status=status,
                received_at=now,
                completed_at=now,
                result=json.dumps(result_data, ensure_ascii=False) if result_data else None,
                error_message=error,
            )
            session.add(task)
            await session.commit()
    except Exception as exc:
        logger.warning("[airport-processor] Audit log failed: %s", exc)


async def _process_one_airport() -> bool:
    """Fetch and process one airport from the backend queue."""
    try:
        task = await backend_client.fetch_next_airport()
    except Exception as exc:
        logger.warning("[airport-processor] Failed to fetch: %s", exc)
        return False

    if task is None:
        logger.info("[airport-processor] No tasks in queue")
        return False

    logger.info(
        "[airport-processor] Task: %s (%s) — %s, %s [%s] (%.4f, %.4f)",
        task.name, task.iata_code, task.city, task.country_code,
        task.facility_type, task.latitude, task.longitude,
    )

    try:
        result = await research_airport(
            name=task.name,
            iata=task.iata_code,
            city=task.city,
            country_code=task.country_code,
            lat=task.latitude,
            lng=task.longitude,
            facility_type=task.facility_type,
        )

        if result is None:
            logger.info("[airport-processor] %s: no research result", task.iata_code)
            await backend_client.submit_airport_result(
                airport_id=task.airport_id, failed=True,
            )
            await _log_audit(task.iata_code, task.latitude, task.longitude, None, GeoResearchStatus.EMPTY)
            return True

        title = result.get("title", task.name)[:200]
        description = _build_event_description(result)
        image_query = result.get("image_query", f"{task.name} airport")

        source_lang = "uk"
        translations = await translate_content(title, description, source_lang=source_lang)
        name_translations = await translate_name(task.name, source_lang="en")

        dalle_prompt = _build_dalle_prompt(task, result)
        photo_path = await get_image_for_post(
            image_query, use_dalle=True, prefer_dalle=True, dalle_prompt=dalle_prompt,
        )
        logger.info(
            "[airport-processor] Image for %s: %s (dalle-first)",
            task.iata_code, "found" if photo_path else "none",
        )

        research_code = f"airport_{task.iata_code}_{int(utcnow_naive().timestamp())}"

        resp = await backend_client.create_research_event(
            research_code=research_code,
            title=title,
            description=description[:4000],
            latitude=task.latitude,
            longitude=task.longitude,
            photo_path=photo_path,
            facility_type=task.facility_type,
            content_language=source_lang,
            translations=translations,
        )

        cleanup_media_file(photo_path)

        event_id = resp.get("eventId", 0)
        content_json = json.dumps(result, ensure_ascii=False)

        # Phase 1 of airport image classification plan: pass through
        # size_class when AI yielded a confident estimate. Empty string
        # (research didn't include the field, e.g. older prompt) →
        # backend keeps existing value via NULLIF/COALESCE; no impact.
        bot_size_class = ""
        if isinstance(result.get("size_class"), str):
            sc = result["size_class"].strip().lower()
            if sc in {"hub", "intl", "regional", "small"}:
                bot_size_class = sc

        await backend_client.submit_airport_result(
            airport_id=task.airport_id,
            content=content_json[:10000],
            event_id=event_id,
            name_translations=name_translations,
            operational_status=task.operational_status,
            size_class=bot_size_class,
        )

        await _log_audit(
            task.iata_code, task.latitude, task.longitude,
            result, GeoResearchStatus.COMPLETED,
        )

        logger.info(
            "[airport-processor] %s: event=%d created",
            task.iata_code, event_id,
        )
        return True

    except Exception as exc:
        logger.exception("[airport-processor] %s failed", task.iata_code)
        try:
            await backend_client.submit_airport_result(
                airport_id=task.airport_id, failed=True,
            )
        except Exception:
            pass
        await _log_audit(
            task.iata_code, task.latitude, task.longitude,
            None, GeoResearchStatus.FAILED, str(exc)[:1000],
        )
        return False


# Phase 1 of airport image classification plan: keys are now
# classification_key (airport_hub / airport_intl / etc.) instead of raw
# facility_type. Lets us differentiate visual style by airport size:
# major hubs get glossy modern terminals; regional airports get smaller
# functional buildings; small airports get single-runway airfield imagery.
# See ../airport_classification.py for the (facility_type, size_class)
# → key mapping.
_DALLE_PROMPT_TEMPLATES = {
    "airport_hub": (
        "Photorealistic aerial view of {name}, a major international airport hub "
        "serving {city}, {country}. Multiple long runways, large modern terminal "
        "complex with glass facades and air bridges, several wide-body aircraft "
        "parked at gates, control tower. Bright daylight, sharp detail, "
        "professional aviation photography style."
    ),
    "airport_intl": (
        "Photorealistic aerial view of {name}, an international airport in {city}, "
        "{country}. Modern terminal building, two runways, mid-size aircraft on "
        "taxiways, surrounding landscape typical for {country}. Bright daylight, "
        "professional travel photography style."
    ),
    "airport_regional": (
        "Photorealistic view of {name}, a regional airport serving {city}, "
        "{country}. Single runway, modest terminal building, regional jets and "
        "turboprop aircraft, surrounding landscape typical for {country}. "
        "Bright daylight, professional travel photography style."
    ),
    "airport_small": (
        "Photorealistic view of {name}, a small airport near {city}, {country}. "
        "Single short runway, small terminal building, light aircraft and a few "
        "small turboprops, surrounding landscape typical for {country}. "
        "Bright daylight, professional travel photography style."
    ),
    "aerodrome": (
        "Photorealistic view of {name} aerodrome near {city}, {country}. "
        "Small airfield with light aircraft, hangars, local landscape. "
        "Bright daylight, professional travel photography style."
    ),
    "heliport": (
        "Photorealistic view of {name} heliport near {city}, {country}. "
        "Helipad, helicopter, surrounding area. "
        "Bright daylight, professional travel photography style."
    ),
    "military": (
        "Photorealistic view of {name} air base area near {city}, {country}. "
        "Aviation facility, historical significance, surrounding landscape. "
        "Bright daylight, professional aerial photography style."
    ),
    "railway_hub": (
        "Photorealistic exterior view of {name}, a major railway hub in {city}, "
        "{country}. Grand historic station building, multiple platforms with "
        "intercity trains, glass and steel canopy, busy with travellers. "
        "Bright daylight, professional travel photography style."
    ),
    "railway": (
        "Photorealistic view of {name} railway station in {city}, {country}. "
        "Station building, platforms, trains, architectural details typical "
        "for the region. Bright daylight, professional travel photography style."
    ),
    "bus": (
        "Photorealistic view of {name} bus station in {city}, {country}. "
        "Bus terminal building, platforms, local architecture and landscape. "
        "Bright daylight, professional travel photography style."
    ),
    "transport": (
        "Photorealistic view of {name}, a transport hub in {city}, {country}. "
        "Functional building with passenger areas, surrounding landscape "
        "typical for {country}. Bright daylight, professional travel "
        "photography style."
    ),
}


def _build_dalle_prompt(task, result: dict) -> str:
    """Build a location-specific DALL-E prompt for unique image generation.

    Selects template by classification_key (preferred — backend-supplied
    via /next-airport) with local Python fallback when missing.
    """
    from geo_agent.airport_classification import resolve_classification_key

    key = resolve_classification_key(task)
    template = _DALLE_PROMPT_TEMPLATES.get(key, _DALLE_PROMPT_TEMPLATES["transport"])

    name = task.name or result.get("title", "Transport hub")
    city = task.city or result.get("city", "")
    country_label = task.country_code or ""

    prompt = template.format(name=name, city=city, country=country_label)
    if len(prompt) > 950:
        prompt = prompt[:950]

    return prompt


_FACILITY_EMOJIS = {
    "airport": ("🚌", "✈️"),
    "aerodrome": ("🚌", "✈️"),
    "railway": ("🚂", "🚆"),
    "heliport": ("🚁", "🚁"),
    "military": ("🏛️", "✈️"),
    "bus": ("🚌", "🚍"),
}


def _build_event_description(result: dict) -> str:
    """Build event description from AI research result."""
    facility_type = result.get("facility_type", "airport")
    transport_emoji, facts_emoji = _FACILITY_EMOJIS.get(facility_type, ("🚌", "✈️"))
    parts = []

    desc = result.get("description", "")
    if desc:
        parts.append(desc)

    city_info = result.get("city_info", "")
    if city_info:
        parts.append(f"\n{city_info}")

    transport = result.get("transport", "")
    if transport:
        parts.append(f"\n{transport_emoji} {transport}")

    facts = result.get("facts", [])
    if facts and isinstance(facts, list):
        fact_lines = [f"• {f}" for f in facts[:5] if isinstance(f, str)]
        if fact_lines:
            parts.append(f"\n{facts_emoji} " + "\n".join(fact_lines))

    return "\n".join(parts) if parts else desc


async def process_airport_queue() -> None:
    """Process the next airport research task.

    Called by scheduler every 2 minutes. Separate from geo research.
    Respects its own 10-per-24h rate limit.
    """
    async with _lock:
        if not backend_client.is_configured():
            return

        try:
            processed = await _count_airport_processed_last_24h()
        except Exception:
            processed = 0

        if processed >= DAILY_LIMIT:
            logger.info("[airport-processor] Daily limit reached (%d/%d)", processed, DAILY_LIMIT)
            return

        logger.info("[airport-processor] Cycle start (%d/%d processed)", processed, DAILY_LIMIT)

        await _process_one_airport()
