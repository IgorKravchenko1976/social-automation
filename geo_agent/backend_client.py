"""HTTP client for imin-backend geo-research API.

Endpoints used:
  GET  /v1/api/research/next          — fetch next cluster to research
  POST /v1/api/research/result        — submit AI research result
  POST /v1/api/research/build-queue   — trigger daily queue rebuild
  GET  /v1/api/research/queue-status  — check queue state
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30


@dataclass
class NextTask:
    cluster_code: str
    cluster_id: int
    center_latitude: float
    center_longitude: float
    priority: float
    point_count: int
    research_code: str
    country_code: str = ""
    scope_keys: dict = None
    missing_levels: list = None


def _headers() -> dict[str, str]:
    return {"X-Sync-Key": settings.imin_backend_sync_key}


def _base() -> str:
    return settings.imin_backend_api_base.rstrip("/")


def is_configured() -> bool:
    return bool(settings.imin_backend_api_base and settings.imin_backend_sync_key)


async def fetch_next_task() -> Optional[NextTask]:
    """GET /v1/api/research/next — returns NextTask or None if queue empty."""
    if not is_configured():
        logger.debug("[backend] Not configured, skipping fetch_next_task")
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(f"{_base()}/v1/api/research/next", headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None

    return NextTask(
        cluster_code=data["clusterCode"],
        cluster_id=data["clusterId"],
        center_latitude=data["centerLatitude"],
        center_longitude=data["centerLongitude"],
        priority=data.get("priority", 0),
        point_count=data.get("pointCount", 0),
        research_code=data["researchCode"],
        country_code=data.get("countryCode", ""),
        scope_keys=data.get("scopeKeys", {}),
        missing_levels=data.get("missingLevels", []),
    )


async def submit_result(
    research_code: str,
    content: str,
    summary: str,
    no_change: bool = False,
    research_level: str = "location",
    scope_key: str = "",
) -> bool:
    """POST /v1/api/research/result — returns True on success."""
    if not is_configured():
        return False

    payload = {
        "researchCode": research_code,
        "content": content,
        "summary": summary,
        "noChange": no_change,
        "researchLevel": research_level,
        "scopeKey": scope_key,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/result",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("ok", False)


async def submit_level_result(
    research_level: str,
    scope_key: str,
    content: str,
    summary: str,
    no_change: bool = False,
) -> bool:
    """POST /v1/api/research/level-result — submit non-location level research."""
    if not is_configured():
        return False

    payload = {
        "researchLevel": research_level,
        "scopeKey": scope_key,
        "content": content,
        "summary": summary,
        "noChange": no_change,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/level-result",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("skipped"):
        logger.info("[backend] Level %s scope=%s already exists, skipped", research_level, scope_key)
    return data.get("ok", False)


async def submit_rejected(
    research_code: str,
    content: str,
    summary: str,
    reject_reason: str,
) -> bool:
    """POST /v1/api/research/reject — submit rejected research to errors DB."""
    if not is_configured():
        return False

    payload = {
        "researchCode": research_code,
        "content": content,
        "summary": summary,
        "rejectReason": reject_reason,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/reject",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("ok", False)


async def trigger_build_queue() -> dict:
    """POST /v1/api/research/build-queue — rebuild daily research queue."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/build-queue",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def create_research_event(
    research_code: str,
    title: str,
    description: str,
    latitude: float,
    longitude: float,
    photo_path: str | None = None,
) -> dict:
    """POST /v1/api/research/create-event — create a real event from research."""
    if not is_configured():
        return {"error": "not configured"}

    data = {
        "researchCode": research_code,
        "title": title,
        "description": description,
        "latitude": str(latitude),
        "longitude": str(longitude),
    }

    async with httpx.AsyncClient(timeout=60) as client:
        if photo_path:
            import os
            ct = "image/jpeg"
            if photo_path.lower().endswith(".png"):
                ct = "image/png"
            with open(photo_path, "rb") as f:
                files = {"photo": (os.path.basename(photo_path), f, ct)}
                resp = await client.post(
                    f"{_base()}/v1/api/research/create-event",
                    headers=_headers(),
                    data=data,
                    files=files,
                )
        else:
            resp = await client.post(
                f"{_base()}/v1/api/research/create-event",
                headers=_headers(),
                data=data,
            )

        resp.raise_for_status()
        return resp.json()


async def get_daily_stats() -> dict:
    """GET /v1/api/research/daily-stats — today's research statistics."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/daily-stats",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def get_queue_status() -> dict:
    """GET /v1/api/research/queue-status — current queue state."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/queue-status",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()
