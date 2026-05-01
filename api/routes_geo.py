"""Geo-research agent API endpoints.

POST /api/geo-research              — submit geodata to research queue
GET  /api/geo-research/status       — queue capacity (poll this once/hour)
GET  /api/geo-research/completed    — pick up ready results
GET  /api/geo-research              — list all tasks
GET  /api/geo-research/{id}         — get single task status & result
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func as sa_func

from api.auth import require_admin
from api.schemas import (
    GeoResearchRequest,
    GeoResearchSubmitResponse,
    GeoResearchQueueStatus,
    GeoResearchTaskOut,
)
from config.settings import utcnow_naive
from db.database import async_session
from db.models import GeoResearchTask, GeoResearchStatus
from geo_agent.processor import DAILY_LIMIT

logger = logging.getLogger(__name__)

MAX_QUEUE_DEPTH = 20

geo_router = APIRouter(
    prefix="/api",
    tags=["geo-research"],
    dependencies=[Depends(require_admin)],
)


@geo_router.get("/geo-research/status", response_model=GeoResearchQueueStatus)
async def geo_research_status():
    """Check queue capacity. External client polls this once per hour
    to decide whether to submit new geodata."""
    cutoff = utcnow_naive() - timedelta(hours=24)

    async with async_session() as session:
        queued = (await session.execute(
            select(sa_func.count(GeoResearchTask.id))
            .where(GeoResearchTask.status == GeoResearchStatus.QUEUED)
        )).scalar() or 0

        processing = (await session.execute(
            select(sa_func.count(GeoResearchTask.id))
            .where(GeoResearchTask.status == GeoResearchStatus.PROCESSING)
        )).scalar() or 0

        processed_24h = (await session.execute(
            select(sa_func.count(GeoResearchTask.id)).where(
                GeoResearchTask.completed_at >= cutoff,
                GeoResearchTask.status.in_([
                    GeoResearchStatus.COMPLETED,
                    GeoResearchStatus.EMPTY,
                ]),
            )
        )).scalar() or 0

        completed_pending = (await session.execute(
            select(sa_func.count(GeoResearchTask.id))
            .where(GeoResearchTask.status == GeoResearchStatus.COMPLETED)
        )).scalar() or 0

    queue_has_space = queued < MAX_QUEUE_DEPTH
    daily_has_budget = (processed_24h + queued + processing) < DAILY_LIMIT
    can_accept = queue_has_space and daily_has_budget

    return GeoResearchQueueStatus(
        can_accept=can_accept,
        queue_size=queued,
        processing=processing,
        processed_24h=processed_24h,
        daily_limit=DAILY_LIMIT,
        completed_pending_pickup=completed_pending,
    )


@geo_router.get("/geo-research/completed", response_model=list[GeoResearchTaskOut])
async def get_completed_research():
    """Return all completed tasks that haven't been picked up yet.
    External client calls this to collect ready results."""
    async with async_session() as session:
        result = await session.execute(
            select(GeoResearchTask)
            .where(GeoResearchTask.status.in_([
                GeoResearchStatus.COMPLETED,
                GeoResearchStatus.EMPTY,
            ]))
            .order_by(GeoResearchTask.completed_at.asc())
        )
        tasks = result.scalars().all()

    return [GeoResearchTaskOut.model_validate(t) for t in tasks]


@geo_router.post("/geo-research", response_model=GeoResearchSubmitResponse)
async def submit_geo_research(req: GeoResearchRequest):
    """Add a geodata research request to the queue.
    Returns 429 if queue is full or daily limit reached."""
    cutoff = utcnow_naive() - timedelta(hours=24)

    async with async_session() as session:
        queued = (await session.execute(
            select(sa_func.count(GeoResearchTask.id))
            .where(GeoResearchTask.status.in_([
                GeoResearchStatus.QUEUED,
                GeoResearchStatus.PROCESSING,
            ]))
        )).scalar() or 0

        processed_24h = (await session.execute(
            select(sa_func.count(GeoResearchTask.id)).where(
                GeoResearchTask.completed_at >= cutoff,
                GeoResearchTask.status.in_([
                    GeoResearchStatus.COMPLETED,
                    GeoResearchStatus.EMPTY,
                ]),
            )
        )).scalar() or 0

    if queued >= MAX_QUEUE_DEPTH:
        raise HTTPException(429, "Черга повна, спробуйте пізніше")
    if (processed_24h + queued) >= DAILY_LIMIT:
        raise HTTPException(429, "Досягнуто ліміт 10 запитів за 24 години")

    request_id = str(uuid.uuid4())
    now = utcnow_naive()

    task = GeoResearchTask(
        request_id=request_id,
        latitude=req.latitude,
        longitude=req.longitude,
        name=req.name,
        language=req.language,
        status=GeoResearchStatus.QUEUED,
        received_at=now,
    )

    async with async_session() as session:
        session.add(task)
        await session.commit()

    logger.info(
        "Geo-research queued: %s (%.4f, %.4f, %s)",
        request_id, req.latitude, req.longitude, req.name or "-",
    )

    return GeoResearchSubmitResponse(
        request_id=request_id,
        status="queued",
        received_at=now,
        message="Запит додано в чергу на дослідження",
    )


@geo_router.get("/geo-research", response_model=list[GeoResearchTaskOut])
async def list_geo_research(limit: int = 50):
    """List geo-research tasks, newest first."""
    async with async_session() as session:
        result = await session.execute(
            select(GeoResearchTask)
            .order_by(GeoResearchTask.received_at.desc())
            .limit(min(limit, 200))
        )
        tasks = result.scalars().all()

    return [GeoResearchTaskOut.model_validate(t) for t in tasks]


@geo_router.get("/geo-research/{request_id}", response_model=GeoResearchTaskOut)
async def get_geo_research(request_id: str):
    """Get status and result of a geo-research task."""
    async with async_session() as session:
        result = await session.execute(
            select(GeoResearchTask).where(GeoResearchTask.request_id == request_id)
        )
        task = result.scalars().first()

    if task is None:
        raise HTTPException(404, f"Task {request_id} not found")

    return GeoResearchTaskOut.model_validate(task)
