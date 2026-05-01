"""Queue processor for region research tasks.

Two-phase pipeline:
  Phase A (seeder): Crawls Wikidata for admin subdivisions per country.
  Phase B (researcher): Enriches individual regions with AI descriptions.

Rate-limited to ~100 regions per day, runs every 15 minutes.
Seeder runs once daily at 05:30.
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
from geo_agent.region_researcher import (
    fetch_country_regions,
    fetch_region_cities,
    research_region,
)
from geo_agent.translator import translate_content, translate_name

logger = logging.getLogger(__name__)

DAILY_LIMIT = 100
_lock = asyncio.Lock()
_seed_lock = asyncio.Lock()
_AUDIT_PREFIX = "region:"
_SEED_AUDIT_PREFIX = "region_seed:"


async def _count_region_processed_last_24h() -> int:
    """Count region tasks completed in the last 24 hours (local DB audit)."""
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
    name: str,
    lat: float,
    lng: float,
    result_data: dict | None,
    status: GeoResearchStatus,
    error: str | None = None,
    prefix: str = _AUDIT_PREFIX,
) -> None:
    """Save a record of region task processing to local DB for audit."""
    try:
        async with async_session() as session:
            now = utcnow_naive()
            task = GeoResearchTask(
                request_id=f"region_{name}_{int(now.timestamp())}",
                latitude=lat,
                longitude=lng,
                name=f"{prefix}{name}",
                language="uk",
                status=status,
                received_at=now,
                completed_at=now,
                result=json.dumps(result_data, ensure_ascii=False) if result_data else None,
                error_message=error,
            )
            session.add(task)
            await session.commit()
    except Exception as exc:
        logger.warning("[region-processor] Audit log failed: %s", exc)


# ── Phase A: Structure Seeding ──


async def seed_country_structure() -> None:
    """Fetch next un-seeded country and crawl its admin structure from Wikidata.

    Called daily at 05:30. Seeds ~5 countries per run.
    """
    async with _seed_lock:
        if not backend_client.is_configured():
            return

        for _ in range(5):
            try:
                result = await backend_client.fetch_next_country_to_seed()
            except Exception as exc:
                logger.warning("[region-seeder] Failed to fetch country: %s", exc)
                return

            if result is None:
                logger.info("[region-seeder] No countries to seed")
                return

            country_code = result["countryCode"]
            logger.info("[region-seeder] Seeding %s (priority=%s)", country_code, result.get("priority"))

            try:
                await _seed_one_country(country_code)
            except Exception as exc:
                logger.exception("[region-seeder] Failed to seed %s", country_code)
                await _log_audit(
                    country_code, 0, 0, None,
                    GeoResearchStatus.FAILED, str(exc)[:1000],
                    prefix=_SEED_AUDIT_PREFIX,
                )

            await asyncio.sleep(5)


async def _seed_one_country(country_code: str) -> None:
    """Seed one country: fetch regions, then cities per region, then bulk-send to backend."""
    regions = await fetch_country_regions(country_code)
    if not regions:
        logger.info("[region-seeder] %s: no regions found", country_code)
        await _log_audit(
            country_code, 0, 0, {"regions": 0},
            GeoResearchStatus.EMPTY, prefix=_SEED_AUDIT_PREFIX,
        )
        return

    # Fetch cities for each region (with rate limiting)
    all_items = list(regions)
    for region in regions:
        wikidata_id = region.get("wikidataId", "")
        if not wikidata_id:
            continue

        try:
            cities = await fetch_region_cities(wikidata_id, country_code)
            for city in cities:
                city["parentWikidataId"] = wikidata_id
                all_items.append(city)
            await asyncio.sleep(2)
        except Exception as exc:
            logger.warning(
                "[region-seeder] Cities failed for %s/%s: %s",
                country_code, wikidata_id, exc,
            )

    # Translate region names
    for item in all_items:
        try:
            name_translations = await translate_name(item["name"], source_lang="uk")
            if name_translations:
                item["nameTranslations"] = name_translations
        except Exception:
            pass
        await asyncio.sleep(0.5)

    # Send to backend
    try:
        result = await backend_client.seed_country_regions(country_code, all_items)
        logger.info(
            "[region-seeder] %s seeded: %d items, backend=%s",
            country_code, len(all_items), result,
        )
        await _log_audit(
            country_code, 0, 0,
            {"regions": len(regions), "cities": len(all_items) - len(regions)},
            GeoResearchStatus.COMPLETED, prefix=_SEED_AUDIT_PREFIX,
        )
    except Exception as exc:
        logger.error("[region-seeder] Backend seed failed for %s: %s", country_code, exc)
        await _log_audit(
            country_code, 0, 0, None,
            GeoResearchStatus.FAILED, str(exc)[:1000],
            prefix=_SEED_AUDIT_PREFIX,
        )


# ── Phase B: Region Research ──


async def _process_one_region() -> bool:
    """Fetch and research one region from the backend queue."""
    try:
        task = await backend_client.fetch_next_region()
    except Exception as exc:
        logger.warning("[region-processor] Failed to fetch: %s", exc)
        return False

    if task is None:
        logger.info("[region-processor] No tasks in queue")
        return False

    logger.info(
        "[region-processor] Task: %s (%s) — %s [%s] (%.4f, %.4f)",
        task["name"], task["regionId"], task["countryCode"],
        task["level"], task.get("latitude", 0), task.get("longitude", 0),
    )

    region_id = task["regionId"]
    queue_id = task.get("queueId", 0)

    try:
        result = await research_region(
            name=task["name"],
            level=task["level"],
            country_code=task["countryCode"],
            parent_name=task.get("parentName", ""),
            latitude=task.get("latitude", 0),
            longitude=task.get("longitude", 0),
            population=task.get("population", 0),
            area_km2=task.get("areaKm2", 0),
            wikidata_id=task.get("wikidataId", ""),
            wikipedia_url=task.get("wikipediaUrl", ""),
        )

        if result is None:
            logger.info("[region-processor] %s: no research result", task["name"])
            await backend_client.submit_region_result(
                region_id=region_id, queue_id=queue_id, failed=True,
            )
            await _log_audit(
                task["name"], task.get("latitude", 0), task.get("longitude", 0),
                None, GeoResearchStatus.EMPTY,
            )
            return True

        summary = result.get("summary", "")
        description = result.get("description", "")

        # Translate summary and description
        summary_translations = {}
        description_translations = {}
        name_translations = {}

        if summary or description:
            try:
                translations = await translate_content(
                    summary, description, source_lang="uk",
                )
                if translations:
                    for lang_code, tr_data in translations.items():
                        if isinstance(tr_data, dict):
                            if tr_data.get("title"):
                                summary_translations[lang_code] = tr_data["title"]
                            if tr_data.get("description"):
                                description_translations[lang_code] = tr_data["description"]
            except Exception as exc:
                logger.warning("[region-processor] Translation failed: %s", exc)

        try:
            name_translations = await translate_name(task["name"], source_lang="uk")
        except Exception:
            pass

        extra = {}
        highlights = result.get("highlights", [])
        if highlights:
            extra["highlights"] = highlights

        await backend_client.submit_region_result(
            region_id=region_id,
            queue_id=queue_id,
            summary=summary,
            description=description,
            summary_translations=summary_translations,
            description_translations=description_translations,
            name_translations=name_translations or {},
            image_url=result.get("imageUrl", ""),
            wikipedia_url=result.get("wikipediaUrl", ""),
            population=task.get("population", 0),
            area_km2=task.get("areaKm2", 0),
            extra=extra,
        )

        await _log_audit(
            task["name"], task.get("latitude", 0), task.get("longitude", 0),
            result, GeoResearchStatus.COMPLETED,
        )

        logger.info("[region-processor] %s: researched successfully", task["name"])
        return True

    except Exception as exc:
        logger.exception("[region-processor] %s failed", task.get("name", "?"))
        try:
            await backend_client.submit_region_result(
                region_id=region_id, queue_id=queue_id, failed=True,
            )
        except Exception:
            pass
        await _log_audit(
            task.get("name", "?"), task.get("latitude", 0), task.get("longitude", 0),
            None, GeoResearchStatus.FAILED, str(exc)[:1000],
        )
        return False


async def process_region_queue() -> None:
    """Process the next region research task.

    Called by scheduler every 15 minutes.
    Respects 100-per-24h rate limit.
    """
    async with _lock:
        if not backend_client.is_configured():
            return

        try:
            processed = await _count_region_processed_last_24h()
        except Exception:
            processed = 0

        if processed >= DAILY_LIMIT:
            logger.info("[region-processor] Daily limit reached (%d/%d)", processed, DAILY_LIMIT)
            return

        logger.info("[region-processor] Cycle start (%d/%d processed)", processed, DAILY_LIMIT)

        await _process_one_region()
