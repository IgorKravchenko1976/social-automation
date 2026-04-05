"""Backfill district + city levels using 2-step geo verification.

Reads completed location-level research from the backend,
generates district/city levels using the updated researcher.py prompts,
and submits them via the backend API.

Usage:
  cd social-automation
  python scripts/backfill_levels_v2.py
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("DATA_DIR", "/tmp/imin-backfill")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

from dotenv import load_dotenv
load_dotenv()

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BACKEND_BASE = os.getenv("IMIN_BACKEND_API_BASE", "https://api-v2.im-in.net")
SYNC_KEY = os.getenv("IMIN_BACKEND_SYNC_KEY", "")
HEADERS = {"X-Sync-Key": SYNC_KEY, "Content-Type": "application/json"}

CLUSTERS = [
    {"id": 57, "code": "8692:8108", "lat": 34.770, "lng": 32.434, "country": "CY"},
    {"id": 22, "code": "8694:8102", "lat": 34.778, "lng": 32.410, "country": "CY"},
    {"id": 50, "code": "8691:8103", "lat": 34.766, "lng": 32.414, "country": "CY"},
    {"id": 326, "code": "8692:8101", "lat": 34.770, "lng": 32.406, "country": "CY"},
    {"id": 198, "code": "8704:8103", "lat": 34.818, "lng": 32.414, "country": "CY"},
    {"id": 322, "code": "8691:8101", "lat": 34.766, "lng": 32.406, "country": "CY"},
    {"id": 318, "code": "8690:8101", "lat": 34.762, "lng": 32.406, "country": "CY"},
    {"id": 91, "code": "8694:8104", "lat": 34.778, "lng": 32.418, "country": "CY"},
    {"id": 192, "code": "8688:8101", "lat": 34.754, "lng": 32.406, "country": "CY"},
    {"id": 3, "code": "12221:584", "lat": 48.886, "lng": 2.338, "country": "FR"},
    {"id": 5, "code": "12217:580", "lat": 48.870, "lng": 2.322, "country": "FR"},
    {"id": 146, "code": "12216:578", "lat": 48.866, "lng": 2.314, "country": "FR"},
    {"id": 29, "code": "12221:585", "lat": 48.886, "lng": 2.342, "country": "FR"},
    {"id": 110, "code": "12216:580", "lat": 48.866, "lng": 2.322, "country": "FR"},
    {"id": 144, "code": "12591:7605", "lat": 50.366, "lng": 30.422, "country": "UA"},
    {"id": 56, "code": "12614:7625", "lat": 50.458, "lng": 30.502, "country": "UA"},
    {"id": 93, "code": "11634:2975", "lat": 46.538, "lng": 11.902, "country": "IT"},
]


async def generate_research(lat: float, lng: float, country: str, level: str) -> dict | None:
    from geo_agent.researcher import research_location
    result = await research_location(
        latitude=lat, longitude=lng,
        language="uk", expected_country=country, level=level,
    )
    if result is None:
        return None
    if result.get("_rejected"):
        logger.warning("REJECTED %s level=%s: %s", f"{lat},{lng}", level, result.get("_reject_reason"))
        return None
    return result


async def submit_level(level: str, scope_key: str, content: str, summary: str) -> bool:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BACKEND_BASE}/v1/api/research/level-result",
            headers=HEADERS,
            json={
                "researchLevel": level,
                "scopeKey": scope_key,
                "content": content,
                "summary": summary,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("skipped"):
                logger.info("  → skipped (already exists)")
                return True
            logger.info("  → submitted OK")
            return True
        logger.error("  → FAILED %d: %s", resp.status_code, resp.text[:200])
        return False


async def process_cluster(cluster: dict, level: str) -> bool:
    lat, lng, country = cluster["lat"], cluster["lng"], cluster["country"]
    code = cluster["code"]
    logger.info("Processing %s (%s, %s) level=%s...", code, lat, lng, level)

    result = await generate_research(lat, lng, country, level)
    if result is None:
        logger.warning("  → No result for %s level=%s", code, level)
        return False

    content = json.dumps(result, ensure_ascii=False)
    summary = result.get("summary", "")[:8000]
    scope_key = code if level != "country" else country

    return await submit_level(level, scope_key, content, summary)


async def main():
    for level in ("location", "district", "city"):
        logger.info("=== Generating %s level ===", level)
        for cluster in CLUSTERS:
            await process_cluster(cluster, level)
            await asyncio.sleep(1)

    logger.info("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
