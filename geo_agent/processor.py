"""Queue processor for geo-research tasks.

Picks the oldest QUEUED task, runs AI research, stores result.
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

logger = logging.getLogger(__name__)

DAILY_LIMIT = 10
_lock = asyncio.Lock()


async def _count_processed_last_24h() -> int:
    """Count tasks completed (or emptied) in the last 24 hours."""
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
    """Return the oldest QUEUED task, or None."""
    async with async_session() as session:
        result = await session.execute(
            select(GeoResearchTask)
            .where(GeoResearchTask.status == GeoResearchStatus.QUEUED)
            .order_by(GeoResearchTask.received_at.asc())
            .limit(1)
        )
        return result.scalars().first()


async def process_geo_queue() -> None:
    """Process the next task in the geo-research queue.

    Called by scheduler every 2 minutes. Processes one task per run.
    Respects the 10-per-24h rate limit.
    """
    async with _lock:
        processed = await _count_processed_last_24h()
        if processed >= DAILY_LIMIT:
            logger.debug("Geo-research daily limit reached (%d/%d)", processed, DAILY_LIMIT)
            return

        task = await _pick_next_task()
        if task is None:
            return

        logger.info(
            "Processing geo-research %s: %.4f, %.4f (%s)",
            task.request_id, task.latitude, task.longitude, task.name or "no name",
        )

        async with async_session() as session:
            db_task = await session.get(GeoResearchTask, task.id)
            if db_task is None or db_task.status != GeoResearchStatus.QUEUED:
                return

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
                    logger.info("Geo-research %s: empty result", task.request_id)
                else:
                    db_task.status = GeoResearchStatus.COMPLETED
                    db_task.result = json.dumps(result, ensure_ascii=False)
                    db_task.completed_at = now
                    logger.info("Geo-research %s: completed", task.request_id)

                await session.commit()

        except Exception as exc:
            logger.exception("Geo-research %s failed", task.request_id)
            async with async_session() as session:
                db_task = await session.get(GeoResearchTask, task.id)
                db_task.status = GeoResearchStatus.FAILED
                db_task.error_message = str(exc)[:1000]
                db_task.completed_at = datetime.now(timezone.utc)
                await session.commit()
