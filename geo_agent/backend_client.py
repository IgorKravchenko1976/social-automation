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
    content_language: str = "",
    translations: dict | None = None,
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
    if content_language:
        payload["contentLanguage"] = content_language
    if translations:
        payload["translations"] = translations

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
    content_language: str = "",
    translations: dict | None = None,
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
    if content_language:
        payload["contentLanguage"] = content_language
    if translations:
        payload["translations"] = translations

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
    facility_type: str = "",
    content_language: str = "",
    translations: dict | None = None,
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
    if facility_type:
        data["facilityType"] = facility_type
    if content_language:
        data["contentLanguage"] = content_language
    if translations:
        import json as _json
        data["translations"] = _json.dumps(translations, ensure_ascii=False)

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


# ── POI research pipeline ──


@dataclass
class POIResearchTask:
    point_id: int
    name: str
    point_type: str
    city: str
    country_code: str
    latitude: float
    longitude: float
    description: str = ""
    image_url: str = ""
    wikipedia_url: str = ""
    opening_hours: str = ""
    phone: str = ""
    website: str = ""
    address: str = ""
    cuisine: str = ""
    operator_name: str = ""
    founded_year: int = 0
    rating: float = 0


async def fetch_next_poi_for_research() -> Optional[POIResearchTask]:
    """GET /v1/api/research/next-poi-for-research — returns enriched POI or None."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/next-poi-for-research",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    poi = data.get("poi")
    if not poi:
        return None

    return POIResearchTask(
        point_id=poi["id"],
        name=poi.get("name", ""),
        point_type=poi.get("pointType", ""),
        city=poi.get("city", ""),
        country_code=poi.get("countryCode", ""),
        latitude=poi.get("latitude", 0),
        longitude=poi.get("longitude", 0),
        description=poi.get("description", ""),
        image_url=poi.get("imageUrl", ""),
        wikipedia_url=poi.get("wikipediaUrl", ""),
        opening_hours=poi.get("openingHours", ""),
        phone=poi.get("phone", ""),
        website=poi.get("website", ""),
        address=poi.get("address", ""),
        cuisine=poi.get("cuisine", ""),
        operator_name=poi.get("operatorName", ""),
        founded_year=poi.get("foundedYear", 0),
        rating=poi.get("rating", 0),
    )


async def mark_poi_researched(point_id: int, research_event_id: int = 0) -> bool:
    """POST /v1/api/research/mark-poi-researched — link research event to POI."""
    if not is_configured():
        return False

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/mark-poi-researched",
            headers=_headers(),
            json={"pointId": point_id, "researchEventId": research_event_id},
        )
        resp.raise_for_status()
        return resp.json().get("status") == "marked"


# ── Airport research pipeline ──


@dataclass
class AirportTask:
    airport_id: int
    iata_code: str
    name: str
    city: str
    country_code: str
    latitude: float
    longitude: float
    priority: float = 0
    facility_type: str = "airport"
    operational_status: str = "unknown"


async def fetch_next_airport() -> Optional[AirportTask]:
    """GET /v1/api/research/next-airport — returns AirportTask or None if empty."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(f"{_base()}/v1/api/research/next-airport", headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None

    return AirportTask(
        airport_id=data["airportId"],
        iata_code=data["iataCode"],
        name=data.get("name", ""),
        city=data.get("city", ""),
        country_code=data.get("countryCode", ""),
        latitude=data.get("latitude", 0),
        longitude=data.get("longitude", 0),
        priority=data.get("priority", 0),
        facility_type=data.get("facilityType", "airport"),
        operational_status=data.get("operationalStatus", "unknown"),
    )


async def submit_airport_result(
    airport_id: int,
    content: str = "",
    event_id: int = 0,
    failed: bool = False,
    name_translations: dict | None = None,
    operational_status: str = "",
    status_reason: str = "",
) -> bool:
    """POST /v1/api/research/airport-result — submit research result for an airport."""
    if not is_configured():
        return False

    payload = {
        "airportId": airport_id,
        "content": content,
        "eventId": event_id,
        "failed": failed,
    }
    if name_translations:
        import json as _json
        payload["nameTranslations"] = _json.dumps(name_translations, ensure_ascii=False)
    if operational_status:
        payload["operationalStatus"] = operational_status
    if status_reason:
        payload["statusReason"] = status_reason

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/airport-result",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("ok", False)


async def trigger_build_airport_queue() -> dict:
    """POST /v1/api/research/build-airport-queue — rebuild daily airport queue."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/build-airport-queue",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_sync_airports() -> dict:
    """POST /v1/api/research/sync-airports — re-sync airports from AirLabs, remove stale."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/sync-airports",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ── Fix/update pipeline ──────────────────────────────────────────


async def next_fix_event(mode: str = "translate") -> dict | None:
    """GET /v1/api/research/fix-queue?mode=... — next event needing fix."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/fix-queue",
            params={"mode": mode},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None
    return data


async def submit_fix_event(
    event_id: int,
    *,
    title: str = "",
    description: str = "",
    content_language: str = "",
    translations: dict | None = None,
    activate: bool = False,
) -> bool:
    """POST /v1/api/research/fix-event — update existing event."""
    if not is_configured():
        return False

    import json as _json

    payload: dict = {"eventId": event_id, "activate": activate}
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    if content_language:
        payload["contentLanguage"] = content_language
    if translations:
        payload["translations"] = _json.dumps(translations, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/fix-event",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json().get("ok", False)


async def next_fix_airport() -> dict | None:
    """GET /v1/api/research/fix-airport — next airport needing name translations."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/fix-airport",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None
    return data


async def submit_fix_airport(airport_id: int, name_translations: dict) -> bool:
    """POST /v1/api/research/fix-airport — save name translations for airport."""
    if not is_configured():
        return False

    import json as _json

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/fix-airport",
            headers=_headers(),
            json={
                "id": airport_id,
                "nameTranslations": _json.dumps(name_translations, ensure_ascii=False),
            },
        )
        resp.raise_for_status()
        return resp.json().get("ok", False)


async def next_fix_research() -> dict | None:
    """GET /v1/api/research/fix-research — next research needing translations."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/fix-research",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None
    return data


async def submit_fix_research(
    research_id: int,
    *,
    content_language: str = "",
    translations: dict | None = None,
    summary: str = "",
    content: str = "",
) -> bool:
    """POST /v1/api/research/fix-research — save translations for research."""
    if not is_configured():
        return False

    import json as _json

    payload: dict = {"id": research_id}
    if content_language:
        payload["contentLanguage"] = content_language
    if translations:
        payload["translations"] = _json.dumps(translations, ensure_ascii=False)  # FixResearch expects string field
    if summary:
        payload["summary"] = summary
    if content:
        payload["content"] = content

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/fix-research",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json().get("ok", False)
