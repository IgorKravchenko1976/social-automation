"""Client for I'M IN Backend POI API — fetches enriched points for social posts."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 15


async def fetch_next_poi() -> Optional[dict]:
    """Fetch the richest enriched POI that hasn't been posted yet.

    Returns a dict with all available POI fields, or None if no POI available.
    """
    base = settings.imin_backend_api_base.rstrip("/")
    key = settings.imin_backend_sync_key
    if not base or not key:
        logger.warning("[poi_client] Backend API not configured (imin_backend_api_base / imin_backend_sync_key)")
        return None

    url = f"{base}/v1/api/research/next-poi-for-post"
    headers = {"X-Sync-Key": key}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            poi = data.get("poi")
            if not poi:
                logger.info("[poi_client] No enriched POI available for posting")
                return None
            logger.info("[poi_client] Got POI id=%s name='%s' type=%s city=%s",
                        poi.get("id"), poi.get("name", "")[:50],
                        poi.get("pointType"), poi.get("city"))
            return poi
    except Exception as e:
        logger.error("[poi_client] Failed to fetch next POI: %s", e)
        return None


async def mark_poi_posted(point_id: int) -> bool:
    """Mark a POI as posted to social media so it won't be selected again."""
    base = settings.imin_backend_api_base.rstrip("/")
    key = settings.imin_backend_sync_key
    if not base or not key:
        return False

    url = f"{base}/v1/api/research/mark-poi-posted"
    headers = {"X-Sync-Key": key}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json={"pointId": point_id})
            resp.raise_for_status()
            logger.info("[poi_client] Marked POI %d as posted", point_id)
            return True
    except Exception as e:
        logger.error("[poi_client] Failed to mark POI %d as posted: %s", point_id, e)
        return False


def format_poi_for_ai(poi: dict) -> str:
    """Format all POI data into a structured text block for AI post generation."""
    lines = []
    lines.append(f"=== ТОЧКА ІНТЕРЕСУ (POI) ===")
    lines.append(f"Назва: {poi.get('name', 'Невідомо')}")

    pt = poi.get("pointType", "").replace("_", " ").title()
    lines.append(f"Тип: {pt}")

    if poi.get("city"):
        lines.append(f"Місто: {poi['city']}")
    if poi.get("countryCode"):
        lines.append(f"Країна: {poi['countryCode'].upper()}")

    if poi.get("address"):
        lines.append(f"Адреса: {poi['address']}")
    if poi.get("phone"):
        lines.append(f"Телефон: {poi['phone']}")
    if poi.get("openingHours"):
        lines.append(f"Години роботи: {poi['openingHours']}")
    if poi.get("cuisine"):
        lines.append(f"Кухня: {poi['cuisine']}")
    if poi.get("website"):
        lines.append(f"Вебсайт: {poi['website']}")
    if poi.get("operatorName"):
        lines.append(f"Оператор: {poi['operatorName']}")
    if poi.get("foundedYear") and poi["foundedYear"] > 0:
        lines.append(f"Рік заснування: {poi['foundedYear']}")
    if poi.get("rating") and poi["rating"] > 0:
        lines.append(f"Рейтинг: {poi['rating']:.1f}")

    if poi.get("description"):
        desc = poi["description"]
        if len(desc) > 800:
            desc = desc[:797] + "..."
        lines.append(f"\nОпис: {desc}")

    if poi.get("wikipediaUrl"):
        lines.append(f"Wikipedia: {poi['wikipediaUrl']}")
    if poi.get("imageUrl"):
        lines.append(f"Зображення: {poi['imageUrl']}")

    lat = poi.get("latitude", 0)
    lon = poi.get("longitude", 0)
    if lat and lon:
        lines.append(f"Координати: {lat:.6f}, {lon:.6f}")

    lines.append("=== КІНЕЦЬ ДАНИХ ===")
    return "\n".join(lines)
