"""Queue processor for geo-research tasks.

Two modes:
  1. Backend mode (primary): fetch tasks from imin-backend API, run AI, submit back.
  2. Local mode (fallback): pick from local SQLite queue if backend is not configured.

Rate-limited to 10 AI calls per 24 hours.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func

from db.database import async_session
from db.models import GeoResearchTask, GeoResearchStatus
from geo_agent.researcher import research_location
from geo_agent import backend_client
from geo_agent.translator import translate_content
from content.media import get_image_for_post, cleanup_media_file

logger = logging.getLogger(__name__)

DAILY_LIMIT = 10
_lock = asyncio.Lock()


async def _count_processed_last_24h() -> int:
    """Count tasks completed (or emptied) in the last 24 hours (local DB)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(GeoResearchTask.id)).where(
                GeoResearchTask.completed_at >= cutoff,
                GeoResearchTask.status.in_([
                    GeoResearchStatus.COMPLETED,
                    GeoResearchStatus.EMPTY,
                ]),
            )
        )
        return result.scalar() or 0


async def _pick_next_task() -> GeoResearchTask | None:
    """Return the oldest QUEUED task from local DB, or None."""
    async with async_session() as session:
        result = await session.execute(
            select(GeoResearchTask)
            .where(GeoResearchTask.status == GeoResearchStatus.QUEUED)
            .order_by(GeoResearchTask.received_at.asc())
            .limit(1)
        )
        return result.scalars().first()


async def _log_to_local_db(
    research_code: str,
    lat: float,
    lng: float,
    result_data: dict | None,
    status: GeoResearchStatus,
    error: str | None = None,
) -> None:
    """Save a record of backend task processing to local DB for audit."""
    try:
        async with async_session() as session:
            task = GeoResearchTask(
                request_id=research_code,
                latitude=lat,
                longitude=lng,
                name=f"backend:{research_code}",
                language="uk",
                status=status,
                received_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                result=json.dumps(result_data, ensure_ascii=False) if result_data else None,
                error_message=error,
            )
            session.add(task)
            await session.commit()
    except Exception as exc:
        logger.warning("[geo-processor] Local DB audit log failed: %s", exc)


async def _process_backend_task() -> bool:
    """Fetch one task from imin-backend, research location + auto-fill full chain."""
    try:
        task = await backend_client.fetch_next_task()
    except Exception as exc:
        logger.warning("[geo-processor] Failed to fetch from backend: %s", exc)
        return False

    if task is None:
        return False

    scope_keys = task.scope_keys or {}

    logger.info(
        "[geo-processor] Backend task: code=%s cluster=%s (%.4f, %.4f)",
        task.research_code, task.cluster_code,
        task.center_latitude, task.center_longitude,
    )

    try:
        location_result = await _process_single_level(
            task, "location", scope_keys.get("location", task.cluster_code),
        )

        await _auto_fill_chain(task, scope_keys)

        return location_result

    except Exception as exc:
        logger.exception("[geo-processor] Backend task %s failed", task.research_code)
        await _log_to_local_db(
            task.research_code, task.center_latitude, task.center_longitude,
            None, GeoResearchStatus.FAILED, str(exc)[:1000],
        )
        return False


async def _auto_fill_chain(task: backend_client.NextTask, scope_keys: dict) -> None:
    """Auto-generate district → city → country if they don't exist yet.

    Uses submit_level_result which skips if already exists (backend dedup).
    """
    for level in ("district", "city", "country"):
        sk = scope_keys.get(level, "")
        if level == "country" and not sk:
            sk = task.country_code
        if not sk:
            sk = task.cluster_code

        try:
            await _process_level_standalone(task, level, sk)
        except Exception as exc:
            logger.warning("[geo-processor] Auto-fill %s failed for %s: %s", level, task.cluster_code, exc)


async def _process_single_level(task: backend_client.NextTask, level: str, scope_key: str) -> bool:
    """Process a single research level using the main research_code from the queue."""
    result = await research_location(
        latitude=task.center_latitude,
        longitude=task.center_longitude,
        name=None,
        language="uk",
        expected_country=task.country_code,
        level=level,
    )

    if result is None:
        await backend_client.submit_result(
            research_code=task.research_code,
            content="", summary="", no_change=True,
            research_level=level, scope_key=scope_key,
        )
        await _log_to_local_db(
            task.research_code, task.center_latitude, task.center_longitude,
            None, GeoResearchStatus.EMPTY,
        )
        logger.info("[geo-processor] %s level=%s: no data", task.research_code, level)
        return True

    if result.get("_rejected"):
        reject_reason = result.get("_reject_reason", "editorial check failed")
        content = json.dumps(result, ensure_ascii=False)
        await backend_client.submit_rejected(
            research_code=task.research_code,
            content=content, summary=result.get("summary", ""),
            reject_reason=reject_reason,
        )
        await _log_to_local_db(
            task.research_code, task.center_latitude, task.center_longitude,
            result, GeoResearchStatus.FAILED, reject_reason,
        )
        logger.warning("[geo-processor] %s level=%s: REJECTED", task.research_code, level)
        return True

    content = json.dumps(result, ensure_ascii=False)
    summary = result.get("summary", "")
    await backend_client.submit_result(
        research_code=task.research_code,
        content=content, summary=summary, no_change=False,
        research_level=level, scope_key=scope_key,
    )
    await _log_to_local_db(
        task.research_code, task.center_latitude, task.center_longitude,
        result, GeoResearchStatus.COMPLETED,
    )
    logger.info("[geo-processor] %s level=%s: completed", task.research_code, level)

    if level == "location":
        await _create_event_for_research(task, result)

    return True


async def _process_level_standalone(task: backend_client.NextTask, level: str, scope_key: str) -> None:
    """Generate and submit a non-location level research (district/city/country)."""
    try:
        result = await research_location(
            latitude=task.center_latitude,
            longitude=task.center_longitude,
            name=None, language="uk",
            expected_country=task.country_code,
            level=level,
        )

        if result is None or result.get("_rejected"):
            logger.info("[geo-processor] %s level=%s: skipped (empty/rejected)", task.cluster_code, level)
            return

        content = json.dumps(result, ensure_ascii=False)
        summary = result.get("summary", "")
        await backend_client.submit_level_result(
            research_level=level,
            scope_key=scope_key,
            content=content,
            summary=summary,
        )
        logger.info("[geo-processor] %s level=%s scope=%s: submitted", task.cluster_code, level, scope_key)

    except Exception as exc:
        logger.warning("[geo-processor] Level %s for %s failed: %s", level, task.cluster_code, exc)


async def _create_event_for_research(task: backend_client.NextTask, result: dict) -> None:
    """Create a real event on imin-backend from completed research."""
    try:
        summary = result.get("summary", "")
        location_name = result.get("location_name", "")
        title = location_name or summary[:100] or f"Research: {task.cluster_code}"

        parts = []
        if summary:
            parts.append(summary)

        history_list = result.get("history", [])
        if history_list and isinstance(history_list, list):
            history_lines = [f"• {h.get('period', '')}: {h.get('description', '')}" for h in history_list[:5]]
            parts.append("📜 Історія\n" + "\n".join(history_lines))

        places_list = result.get("places", [])
        if places_list and isinstance(places_list, list):
            place_lines = [f"• {p.get('name', '')} — {p.get('description', '')}" for p in places_list[:5]]
            parts.append("📍 Цікаві місця\n" + "\n".join(place_lines))

        description = "\n\n".join(parts) if parts else summary

        source_lang = "uk"
        translations = await translate_content(title[:200], description[:4000], source_lang=source_lang)

        image_query = f"{location_name} travel landscape" if location_name else "travel landscape beautiful destination"
        photo_path = await get_image_for_post(image_query, use_dalle=True)
        logger.info(
            "[geo-processor] Event image for %s: %s",
            task.research_code, "found" if photo_path else "none",
        )

        resp = await backend_client.create_research_event(
            research_code=task.research_code,
            title=title[:200],
            description=description[:4000],
            latitude=task.center_latitude,
            longitude=task.center_longitude,
            photo_path=photo_path,
            content_language=source_lang,
            translations=translations,
        )

        cleanup_media_file(photo_path)

        if resp.get("ok"):
            logger.info("[geo-processor] Event created: id=%s for %s", resp.get("eventId"), task.research_code)
        else:
            logger.warning("[geo-processor] Event creation response: %s", resp)

    except Exception as exc:
        logger.warning("[geo-processor] Event creation failed for %s: %s", task.research_code, exc)


async def _process_local_task() -> bool:
    """Process one task from local SQLite DB. Returns True if processed."""
    task = await _pick_next_task()
    if task is None:
        return False

    logger.info(
        "[geo-processor] Local task %s: %.4f, %.4f (%s)",
        task.request_id, task.latitude, task.longitude, task.name or "no name",
    )

    async with async_session() as session:
        db_task = await session.get(GeoResearchTask, task.id)
        if db_task is None or db_task.status != GeoResearchStatus.QUEUED:
            return False
        db_task.status = GeoResearchStatus.PROCESSING
        await session.commit()

    try:
        result = await research_location(
            latitude=task.latitude,
            longitude=task.longitude,
            name=task.name,
            language=task.language,
        )

        async with async_session() as session:
            db_task = await session.get(GeoResearchTask, task.id)
            now = datetime.now(timezone.utc)
            if result is None:
                db_task.status = GeoResearchStatus.EMPTY
                db_task.completed_at = now
            else:
                db_task.status = GeoResearchStatus.COMPLETED
                db_task.result = json.dumps(result, ensure_ascii=False)
                db_task.completed_at = now
            await session.commit()

        return True

    except Exception as exc:
        logger.exception("[geo-processor] Local task %s failed", task.request_id)
        async with async_session() as session:
            db_task = await session.get(GeoResearchTask, task.id)
            db_task.status = GeoResearchStatus.FAILED
            db_task.error_message = str(exc)[:1000]
            db_task.completed_at = datetime.now(timezone.utc)
            await session.commit()
        return False


async def process_geo_queue() -> None:
    """Process the next geo-research task.

    Called by scheduler every 2 minutes. Processes one task per run.
    Respects the 10-per-24h rate limit.
    Prefers backend API tasks; falls back to local queue.
    """
    async with _lock:
        try:
            processed = await _count_processed_last_24h()
        except Exception:
            processed = 0

        if processed >= DAILY_LIMIT:
            logger.debug("[geo-processor] Daily limit reached (%d/%d)", processed, DAILY_LIMIT)
            return

        if backend_client.is_configured():
            if await _process_backend_task():
                return

        try:
            await _process_local_task()
        except Exception as exc:
            logger.debug("[geo-processor] Local task skipped: %s", exc)
