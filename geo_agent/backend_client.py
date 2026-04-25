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
    point_id: int | None = None,
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
    if point_id:
        data["pointId"] = str(point_id)

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


async def try_enrich_photo(point_id: int) -> str | None:
    """GET /v1/api/research/try-enrich-photo — try Google Places photo for a POI without image.

    Returns image URL if found, None otherwise.
    """
    if not is_configured() or not point_id:
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_base()}/v1/api/research/try-enrich-photo",
                params={"pointId": str(point_id)},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            url = data.get("imageUrl", "")
            if url:
                logger.info("[backend] Google photo enriched for point %d: %s", point_id, url[:80])
            return url if url else None
    except Exception as e:
        logger.warning("[backend] try-enrich-photo failed for point %d: %s", point_id, e)
        return None


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


async def submit_poi_research(point_id: int, blocks: list[dict]) -> dict:
    """POST /v1/api/research/submit-poi-research — submit research blocks with sources."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/submit-poi-research",
            headers=_headers(),
            json={"pointId": point_id, "blocks": blocks},
        )
        resp.raise_for_status()
        return resp.json()


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


async def trigger_sync_airports_to_points() -> dict:
    """POST /v1/api/research/sync-airports-to-points — sync completed airports into map_points."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/sync-airports-to-points",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ── Region research pipeline ──────────────────────────────────────


async def fetch_next_country_to_seed() -> Optional[dict]:
    """GET /v1/api/research/next-country-to-seed — returns country to seed or None."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/next-country-to-seed",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None
    return data


async def seed_country_regions(country_code: str, regions: list[dict]) -> dict:
    """POST /v1/api/research/seed-country-regions — bulk insert region structure."""
    if not is_configured():
        return {"error": "not configured"}

    import json as _json

    # Clean up nameTranslations: ensure they are proper JSON
    for item in regions:
        if "nameTranslations" in item and isinstance(item["nameTranslations"], dict):
            item["nameTranslations"] = item["nameTranslations"]

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/seed-country-regions",
            headers=_headers(),
            json={"countryCode": country_code, "regions": regions},
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_build_region_queue() -> dict:
    """POST /v1/api/research/build-region-queue — rebuild daily region queue."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/build-region-queue",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_next_region() -> Optional[dict]:
    """GET /v1/api/research/next-region — returns region task or None."""
    if not is_configured():
        return None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/next-region",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None
    return data


async def submit_region_result(
    region_id: int,
    queue_id: int = 0,
    *,
    failed: bool = False,
    summary: str = "",
    description: str = "",
    summary_translations: dict | None = None,
    description_translations: dict | None = None,
    name_translations: dict | None = None,
    image_url: str = "",
    wikipedia_url: str = "",
    population: int = 0,
    area_km2: float = 0,
    timezone: str = "",
    extra: dict | None = None,
) -> bool:
    """POST /v1/api/research/region-result — submit research result for a region."""
    if not is_configured():
        return False

    import json as _json

    payload: dict = {
        "regionId": region_id,
        "queueId": queue_id,
        "failed": failed,
    }
    if summary:
        payload["summary"] = summary
    if description:
        payload["description"] = description
    if summary_translations:
        payload["summaryTranslations"] = summary_translations
    if description_translations:
        payload["descriptionTranslations"] = description_translations
    if name_translations:
        payload["nameTranslations"] = name_translations
    if image_url:
        payload["imageUrl"] = image_url
    if wikipedia_url:
        payload["wikipediaUrl"] = wikipedia_url
    if population > 0:
        payload["population"] = population
    if area_km2 > 0:
        payload["areaKm2"] = area_km2
    if timezone:
        payload["timezone"] = timezone
    if extra:
        payload["extra"] = extra

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/research/region-result",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json().get("ok", False)


async def get_region_queue_status() -> dict:
    """GET /v1/api/research/region-queue-status — current region queue state."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/research/region-queue-status",
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


# ════════════════════════════════════════════════════════════════════════════
# City Pulse — cultural events vertical (added April 2026).
#
# Three job types feed it:
#   discover_sources : Perplexity-powered hunt for cinema/theater/concert
#                      websites & APIs in a given city.
#   verify_source    : weekly HEAD + parse check; updates reliability_score.
#   fetch_content    : daily RSS / iCal / sitemap parse → GPT-4o-mini
#                      normalization → upsert into city_events.
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class CityPulseSource:
    id: int
    name: str
    homepage_url: str
    feed_url: str
    api_endpoint: str
    source_type: str
    categories: list
    status: str
    reliability_score: float
    language: str
    fetch_config: dict
    country_code: str
    city: str


@dataclass
class CityPulseJob:
    id: int
    job_type: str
    country_code: str
    city: str
    source_id: int | None
    source: CityPulseSource | None
    priority: float
    attempt_count: int
    payload: dict


async def fetch_next_city_pulse_job(job_type: str | None = None) -> CityPulseJob | None:
    """GET /v1/api/city-pulse/next-job — pull the next pending job.

    job_type filter is optional; without it the bot processes whatever's hot.
    """
    if not is_configured():
        return None

    params = {}
    if job_type:
        params["type"] = job_type

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/city-pulse/next-job",
            headers=_headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("empty"):
        return None

    src_data = data.get("source")
    src = None
    if src_data:
        src = CityPulseSource(
            id=src_data.get("id", 0),
            name=src_data.get("name", ""),
            homepage_url=src_data.get("homepageUrl", ""),
            feed_url=src_data.get("feedUrl", ""),
            api_endpoint=src_data.get("apiEndpoint", ""),
            source_type=src_data.get("sourceType", ""),
            categories=src_data.get("categories", []),
            status=src_data.get("status", ""),
            reliability_score=src_data.get("reliabilityScore", 0.0),
            language=src_data.get("language", ""),
            fetch_config=src_data.get("fetchConfig") or {},
            country_code=src_data.get("countryCode", ""),
            city=src_data.get("city", ""),
        )

    return CityPulseJob(
        id=data.get("id", 0),
        job_type=data.get("jobType", ""),
        country_code=data.get("countryCode", ""),
        city=data.get("city", ""),
        source_id=data.get("sourceId"),
        source=src,
        priority=data.get("priority", 0.0),
        attempt_count=data.get("attemptCount", 0),
        payload=data.get("payload") or {},
    )


async def submit_sources_discovered(
    job_id: int,
    country_code: str,
    city: str,
    sources: list[dict],
    failed: bool = False,
    error: str = "",
) -> dict:
    """POST /v1/api/city-pulse/sources-discovered — return discovered sources.

    Each source dict should contain at minimum: name, homepageUrl, sourceType,
    categories. Optional: feedUrl, apiEndpoint, language, notes, fetchConfig.
    """
    if not is_configured():
        return {"error": "not configured"}

    payload = {
        "jobId": job_id,
        "countryCode": country_code,
        "city": city,
        "sources": sources,
        "failed": failed,
    }
    if error:
        payload["error"] = error

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/sources-discovered",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def submit_source_verified(
    job_id: int,
    source_id: int,
    *,
    http_status: int,
    parse_ok: bool,
    new_events_found: int = 0,
    total_events_seen: int = 0,
    duration_ms: int = 0,
    error_message: str = "",
) -> dict:
    """POST /v1/api/city-pulse/source-verified — submit weekly check result."""
    if not is_configured():
        return {"error": "not configured"}

    payload = {
        "jobId": job_id,
        "sourceId": source_id,
        "httpStatus": http_status,
        "parseOk": parse_ok,
        "newEventsFound": new_events_found,
        "totalEventsSeen": total_events_seen,
        "durationMs": duration_ms,
        "errorMessage": error_message,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/source-verified",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def submit_events_imported(
    job_id: int,
    source_id: int,
    events: list[dict],
    failed: bool = False,
    error: str = "",
) -> dict:
    """POST /v1/api/city-pulse/events-imported — upsert parsed events.

    Each event dict shape (camelCase to match Go handler):
      externalId, title, description, contentLanguage, translations,
      category, startsAt, endsAt, schedule, venueName, venueAddress,
      latitude, longitude, priceFrom, priceTo, currency, ticketUrl,
      thumbnailUrl, photos, ageLimit, spokenLanguage, meta.

    Backend dedups by (sourceId, externalId).
    """
    if not is_configured():
        return {"error": "not configured"}

    payload = {
        "jobId": job_id,
        "sourceId": source_id,
        "events": events,
        "failed": failed,
    }
    if error:
        payload["error"] = error

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/events-imported",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_city_pulse_build_verify_queue() -> dict:
    """POST /v1/api/city-pulse/build-verify-queue — enqueue weekly checks."""
    if not is_configured():
        return {"error": "not configured"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/build-verify-queue",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_city_pulse_build_fetch_queue() -> dict:
    """POST /v1/api/city-pulse/build-fetch-queue — enqueue daily content fetch."""
    if not is_configured():
        return {"error": "not configured"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/build-fetch-queue",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


@dataclass
class CityPulseVoiceJob:
    """One pending narration task pulled from the backend."""
    id: int
    content_language: str
    translations: dict
    title: str
    description: str
    category: str
    venue_name: str
    city: str
    country_code: str
    starts_at: str | None


async def fetch_pending_voice_jobs(lang: str, limit: int = 5) -> list[CityPulseVoiceJob]:
    """GET /v1/api/city-pulse/pending-voice — events needing narration in `lang`.

    Backend returns events where:
      - audio_status IN ('pending', 'failed')
      - audio_urls does NOT contain `lang`
      - either content_language = lang OR translations have `lang` entry
    """
    if not is_configured():
        return []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{_base()}/v1/api/city-pulse/pending-voice",
            headers=_headers(),
            params={"lang": lang, "limit": str(limit)},
        )
        resp.raise_for_status()
        data = resp.json()

    out: list[CityPulseVoiceJob] = []
    for j in data.get("jobs", []):
        out.append(CityPulseVoiceJob(
            id=j.get("id", 0),
            content_language=j.get("contentLanguage", ""),
            translations=j.get("translations") or {},
            title=j.get("title", ""),
            description=j.get("description", ""),
            category=j.get("category", ""),
            venue_name=j.get("venueName", ""),
            city=j.get("city", ""),
            country_code=j.get("countryCode", ""),
            starts_at=j.get("startsAt"),
        ))
    return out


async def upload_voice(
    city_event_id: int,
    lang: str,
    audio_bytes: bytes,
    content_type: str = "audio/mpeg",
) -> dict:
    """POST /v1/api/city-pulse/voice-uploaded — backend stores in B2.

    Returns {url, lang, cityEventId} on success. The url is presigned for
    1 hour but the backend keeps it in audio_urls for re-presigning later.
    """
    if not is_configured():
        return {"error": "not configured"}

    files = {
        "audio": (f"{lang}.mp3", audio_bytes, content_type),
    }
    data = {
        "cityEventId": str(city_event_id),
        "lang": lang,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/voice-uploaded",
            headers=_headers(),  # X-Sync-Key only; no Content-Type — let httpx set multipart
            data=data,
            files=files,
        )
        resp.raise_for_status()
        return resp.json()


async def submit_voice_failed(city_event_id: int, reason: str) -> dict:
    """POST /v1/api/city-pulse/voice-failed — record TTS failure."""
    if not is_configured():
        return {"error": "not configured"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/voice-failed",
            headers=_headers(),
            json={"cityEventId": city_event_id, "reason": reason},
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_city_pulse_auto_discover(max_cities: int = 5) -> dict:
    """POST /v1/api/city-pulse/auto-discover — find new active cities w/o sources.

    Run weekly. Backend picks cities where ≥3 distinct users browsed POIs in
    the last 14 days but City Pulse has no sources yet, then enqueues
    discover_sources jobs for them.
    """
    if not is_configured():
        return {"error": "not configured"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/auto-discover",
            headers=_headers(),
            params={"max": str(max_cities)},
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_city_pulse_collective_interests(threshold: int = 3) -> dict:
    """POST /v1/api/city-pulse/collective-interests — fan out group pushes.

    Returns {matched, triggered, threshold}.
    """
    if not is_configured():
        return {"error": "not configured"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/collective-interests",
            headers=_headers(),
            params={"threshold": str(threshold)},
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_city_pulse_archive_expired() -> dict:
    """POST /v1/api/city-pulse/archive-expired — archive past events."""
    if not is_configured():
        return {"error": "not configured"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/archive-expired",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def trigger_city_pulse_enqueue_discover(
    country_code: str,
    city: str,
    *,
    region_id: int | None = None,
    priority: float = 0.0,
) -> dict:
    """POST /v1/api/city-pulse/enqueue-discover — seed a city for source hunt."""
    if not is_configured():
        return {"error": "not configured"}
    payload = {
        "countryCode": country_code,
        "city": city,
        "priority": priority,
    }
    if region_id is not None:
        payload["regionId"] = region_id
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{_base()}/v1/api/city-pulse/enqueue-discover",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()
