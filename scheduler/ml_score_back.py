"""Daily ML score-back cron — Phase 5 of priority-ml-system (bot side).

Walks the bot's own historical Post + Publication mirror, scores each
unique POI / city_event the LightGBM ranker has features for, and
batch-PATCHes the predictions back to the backend via
/v1/api/admin/social/ml-scores.

Why the bot's own history (and not a fresh /score-candidates endpoint
on the backend yet)
- The Phase 5 backend MR ships a minimal surface (PATCH only). The
  bot already keeps every published Post with poi_point_id /
  backend_event_id, so we have a natural candidate pool of "things we
  *might* publish again" without adding another backend route.
- Future-work Phase 5b can add /admin/social/score-candidates that
  pulls the top-N FRESH POI from map_points so the model can also
  influence rows the bot has never seen before.

Runtime budget
- Cron runs at 04:00 — quiet publishing window, model file loaded
  once into the bot process.
- Predict loop is pure-CPU LightGBM, ~10 ms per 100 rows on a
  shared OVH VPS. 3000 predictions << 1 second; the HTTP PATCHes
  are the bottleneck.

Failure modes
- ML model not loaded → predict_scores returns 0.0 for every row →
  push_ml_scores still PATCHes. Backend formula treats 0 as "no
  signal", so worst case we fall back to rule-based.
- Backend down → push_ml_scores logs and exits cleanly. Next-day
  retry covers the gap.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from db.database import async_session
from db.models import Post

from scheduler.handoff_client import push_ml_scores, is_configured

logger = logging.getLogger(__name__)


def _build_candidate(post: Post, kind: str, source_id: int) -> dict[str, Any]:
    """Convert a historical Post into a {kind,id,...} candidate dict.

    Mirrors the social-handoff API shape so build_features_for_candidate
    sees the same keys whether we score live (Phase 4 endpoint) or
    batch (this cron).
    """
    return {
        "kind": kind,
        "id": source_id,
        "name": post.title,
        "title": post.title,
        "city": post.place_name,
        "countryCode": None,
        "rating": None,
        "hasPhoto": bool(post.image_path),
        "hasDesc": bool(post.content_raw and len(post.content_raw) >= 80),
        "ticketUrl": post.ticket_url,
    }


async def score_back_daily(
    *,
    poi_limit: int = 2000,
    event_limit: int = 1000,
) -> dict[str, int]:
    """Cron entrypoint. Returns counters {updatedPOI, updatedCityEvent, skipped, scored}.

    Implementation notes
    - Pulls DISTINCT poi_point_id / backend_event_id from `posts` to
      avoid re-scoring the same row N times within a single batch.
    - Builds one feature row per source_id, then a single predict()
      call so LightGBM can vectorise.
    - Limits per-kind keep the daily PATCH bounded even if the bot's
      history grows to tens of thousands.
    """
    if not is_configured():
        logger.info("[ml.score_back] backend not configured, skipping")
        return {"updatedPOI": 0, "updatedCityEvent": 0, "skipped": 0, "scored": 0}

    # Lazy-import ML stack so the cron is safe to schedule even on a
    # bot deploy that hasn't picked up Phase 4 (lightgbm not installed,
    # ml/ package missing). predict_scores returns 0.0 in that case;
    # backend formula's COALESCE keeps the rule-based ranking active.
    try:
        from ml import predict_scores
        from ml.feature_extractor import build_features_for_candidate
    except ImportError:
        logger.info("[ml.score_back] ml package not installed, skipping")
        return {"updatedPOI": 0, "updatedCityEvent": 0, "skipped": 0, "scored": 0}

    by_kind: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)

    async with async_session() as session:
        # POIs: distinct map_point ids the bot has ever published.
        rows = (await session.execute(
            select(Post)
            .where(Post.poi_point_id.isnot(None))
            .order_by(Post.created_at.desc())
            .limit(poi_limit * 2)  # x2 because dedup will collapse
        )).scalars().all()
        for post in rows:
            pid = post.poi_point_id
            if pid is None or pid in by_kind["poi"]:
                continue
            by_kind["poi"][pid] = _build_candidate(post, "poi", pid)
            if len(by_kind["poi"]) >= poi_limit:
                break

        # City events: distinct backend_event_id.
        rows = (await session.execute(
            select(Post)
            .where(Post.backend_event_id.isnot(None))
            .order_by(Post.created_at.desc())
            .limit(event_limit * 2)
        )).scalars().all()
        for post in rows:
            eid = post.backend_event_id
            if eid is None or eid in by_kind["city_event"]:
                continue
            by_kind["city_event"][eid] = _build_candidate(post, "city_event", eid)
            if len(by_kind["city_event"]) >= event_limit:
                break

    total = sum(len(v) for v in by_kind.values())
    if total == 0:
        logger.info("[ml.score_back] no historical POI/events to score")
        return {"updatedPOI": 0, "updatedCityEvent": 0, "skipped": 0, "scored": 0}

    try:
        import pandas as pd
    except ImportError:
        logger.info("[ml.score_back] pandas not installed, sending zeros")
        updates = []
        for kind, candidates in by_kind.items():
            for c in candidates.values():
                updates.append({"kind": kind, "id": int(c["id"]), "score": 0.0})
        counters = await push_ml_scores(updates)
        counters["scored"] = len(updates)
        return counters

    updates: list[dict[str, Any]] = []
    for kind, candidates in by_kind.items():
        items = list(candidates.values())
        if not items:
            continue
        feature_frames = [build_features_for_candidate(c) for c in items]
        df = pd.concat(feature_frames, ignore_index=True)
        scores = predict_scores(df)
        for cand, score in zip(items, scores):
            updates.append({"kind": kind, "id": int(cand["id"]), "score": float(score)})

    counters = await push_ml_scores(updates)
    counters["scored"] = len(updates)
    logger.info(
        "[ml.score_back] complete: scored=%d updatedPOI=%d updatedCityEvent=%d skipped=%d",
        counters["scored"], counters["updatedPOI"],
        counters["updatedCityEvent"], counters["skipped"],
    )
    return counters
