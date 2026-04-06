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

from db.database import async_session
from db.models import GeoResearchTask, GeoResearchStatus
from geo_agent import backend_client
from geo_agent.airport_researcher import research_airport
from content.media import get_image_for_post, cleanup_media_file

logger = logging.getLogger(__name__)

DAILY_LIMIT = 10
_lock = asyncio.Lock()
_AUDIT_PREFIX = "airport:"


async def _count_airport_processed_last_24h() -> int:
    """Count airport tasks completed in the last 24 hours (local DB audit)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
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
            task = GeoResearchTask(
                request_id=f"airport_{iata}",
                latitude=lat,
                longitude=lng,
                name=f"{_AUDIT_PREFIX}{iata}",
                language="en",
                status=status,
                received_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
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
        return False

    logger.info(
        "[airport-processor] Task: %s (%s) — %s, %s (%.4f, %.4f)",
        task.name, task.iata_code, task.city, task.country_code,
        task.latitude, task.longitude,
    )

    try:
        result = await research_airport(
            name=task.name,
            iata=task.iata_code,
            city=task.city,
            country_code=task.country_code,
            lat=task.latitude,
            lng=task.longitude,
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

        photo_path = await get_image_for_post(image_query, use_dalle=True)
        logger.info(
            "[airport-processor] Image for %s: %s",
            task.iata_code, "found" if photo_path else "none",
        )

        research_code = f"airport_{task.iata_code}_{int(datetime.now(timezone.utc).timestamp())}"

        resp = await backend_client.create_research_event(
            research_code=research_code,
            title=title,
            description=description[:4000],
            latitude=task.latitude,
            longitude=task.longitude,
            photo_path=photo_path,
        )

        cleanup_media_file(photo_path)

        event_id = resp.get("eventId", 0)
        content_json = json.dumps(result, ensure_ascii=False)

        await backend_client.submit_airport_result(
            airport_id=task.airport_id,
            content=content_json[:10000],
            event_id=event_id,
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


def _build_event_description(result: dict) -> str:
    """Build event description from AI research result."""
    parts = []

    desc = result.get("description", "")
    if desc:
        parts.append(desc)

    city_info = result.get("city_info", "")
    if city_info:
        parts.append(f"\n{city_info}")

    transport = result.get("transport", "")
    if transport:
        parts.append(f"\n🚌 {transport}")

    facts = result.get("facts", [])
    if facts and isinstance(facts, list):
        fact_lines = [f"• {f}" for f in facts[:5] if isinstance(f, str)]
        if fact_lines:
            parts.append("\n✈️ " + "\n".join(fact_lines))

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
            logger.debug("[airport-processor] Daily limit reached (%d/%d)", processed, DAILY_LIMIT)
            return

        await _process_one_airport()
