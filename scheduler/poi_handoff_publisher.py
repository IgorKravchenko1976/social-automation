"""POI publisher driven by the backend social hand-off API.

Sister job to city_pulse_handoff_publisher.publish_via_handoff but
for source_kind='poi'. Runs less frequently (every 60 min, batch=1)
because POI cadence in production is ~5 posts/day total — the legacy
slot system already fires POI in slot 2 (10:00) and as web_news
fallbacks. The hand-off path adds at most one extra POI per hour on
top of that, with backend-side anti-burst protection (same
point_type in same city is penalised heavily so we don't post five
"cafe in Lviv" spotlights in a row).

Why a separate job and not the same publish_via_handoff() called
with kind='poi'?
  - Different lock so a stuck city_pulse cycle can't block POI.
  - Different client_id ('social-bot-poi') so concurrent claims
    don't fight over rows in the shared social_post_handoff table.
  - Different cadence (60 min vs 15 min).
  - Easier to disable just one channel via APScheduler if either
    pipeline misbehaves.

AI-budget separation
  This pipeline ONLY orchestrates which already-prepared map_point
  the bot publishes next. The POI's description, translations and
  photo enrichment all happened upstream in the geo_agent /
  fixer / datacollector pipelines that have their own queues and
  budget. A bursty social run can't starve them.
"""
from __future__ import annotations

import asyncio
import logging

from scheduler import handoff_client
from scheduler.city_pulse_handoff_publisher import _process_poi_handoff

logger = logging.getLogger(__name__)

_poi_lock = asyncio.Lock()

POI_BATCH_SIZE = 1
POI_CLIENT_ID = "social-bot-poi"


async def publish_poi_via_handoff() -> int:
    """One POI hand-off cycle. Returns number of posts published."""
    async with _poi_lock:
        if not handoff_client.is_configured():
            logger.debug("[poi-handoff] backend not configured, skipping")
            return 0

        try:
            batch = await handoff_client.next_batch(
                n=POI_BATCH_SIZE,
                client_id=POI_CLIENT_ID,
                kind="poi",
            )
        except Exception as exc:
            logger.warning("[poi-handoff] next-batch failed: %s", exc)
            return 0

        if not batch:
            logger.debug("[poi-handoff] empty batch")
            return 0

        logger.info(
            "[poi-handoff] received batch of %d (priorities: %s)",
            len(batch), [round(it.priority_hint, 2) for it in batch],
        )

        published = 0
        for item in batch:
            try:
                ok = await _process_poi_handoff(item)
                if ok:
                    published += 1
            except Exception as exc:
                logger.exception(
                    "[poi-handoff] unexpected failure on handoff %d: %s",
                    item.handoff_id, exc,
                )
        logger.info("[poi-handoff] published %d/%d", published, len(batch))
        return published
