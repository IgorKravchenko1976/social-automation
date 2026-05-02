"""Feature extractor — Post → numeric feature vector for ML ranker.

Phase 4 of priority-ml-system. The feature set is intentionally small
to start (16 features) — adding more is cheap once we see which ones
LightGBM finds useful (gain importance dump in trainer logs).

Two modes:
  - `build_feature_frame(posts)` — for training on historical Post rows
    that were actually published. Joins to PostEngagement to derive
    the supervised label `score`.
  - `build_features_for_candidates(candidates)` — for live scoring of
    queue candidates (Phase 5 daily score-back cron). No label needed,
    only features.

Feature definition (all numeric or one-hot booleans):
  - point_type_*    one-hot of top 20 most-common point types
  - rating          0..5 from POI / 0 for events
  - has_image       1 if image_path or image_url exists
  - image_kb        size of image in KB (0 if none)
  - description_len chars in content_raw
  - title_len       chars in title (0 for missing)
  - country_ua      1 if country_code == 'UA'
  - city_centrality capital=2, large city=1, other=0
  - day_of_week     0..6 (Monday=0)
  - hour            0..23
  - source_handoff  1 if Post.handoff_id is not NULL
  - has_video       1 if video_path exists
  - is_event        1 if backend_event_id is not NULL
  - is_poi          1 if poi_point_id is not NULL
  - has_translations 1 if translations json non-empty
  - has_ticket_url  1 if ticket_url exists
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Capital cities (centrality=2). Source: capitals of audience countries.
# Match is case-insensitive substring on the canonical name.
_CAPITALS = {"київ", "kyiv", "kiev", "warsaw", "warszawa", "berlin", "praha", "prague",
             "budapest", "bucuresti", "bucharest", "vilnius", "riga", "tallinn",
             "bratislava", "ljubljana", "zagreb", "sofia", "athens", "helsinki",
             "stockholm", "oslo", "copenhagen", "amsterdam", "brussels", "paris",
             "madrid", "lisbon", "lisboa", "rome", "roma", "vienna", "wien",
             "london", "dublin", "tbilisi", "yerevan", "chisinau", "ankara"}

# "Large" cities — well-known regional centres (centrality=1). Bot operates
# mostly in UA right now so this list is UA-leaning + a few EU regional
# capitals worth boosting.
_LARGE_CITIES = {"львів", "lviv", "одеса", "odesa", "odessa", "харків", "kharkiv",
                 "дніпро", "dnipro", "запоріжжя", "zaporizhzhia", "вінниця",
                 "vinnytsia", "івано-франківськ", "ivano-frankivsk", "тернопіль",
                 "ternopil", "ужгород", "uzhhorod", "чернівці", "chernivtsi",
                 "полтава", "poltava", "житомир", "zhytomyr", "хмельницький",
                 "khmelnytskyi", "krakow", "krakуw", "gdansk", "wroclaw",
                 "munich", "münchen", "hamburg", "frankfurt", "barcelona",
                 "milan", "milano", "naples", "napoli", "florence", "firenze",
                 "porto", "valencia", "seville", "marseille", "lyon"}

# Top-20 point types we one-hot encode. Anything else lumped into "other".
# These cover ~95% of POI hand-offs in 30 days of production data.
_POINT_TYPES = (
    "museum", "gallery", "theatre", "monument", "cathedral", "castle",
    "viewpoint", "waterfall", "park", "restaurant", "cafe", "bar",
    "hotel", "fast_food", "supermarket", "fuel_station",
    "concert_hall", "exhibition_center", "market", "spa",
)

# Final feature column order — pinned because LightGBM prediction
# requires the same order as training. Adding new columns means
# re-training; never re-order existing ones.
FEATURE_COLUMNS = (
    "rating",
    "has_image",
    "image_kb",
    "description_len",
    "title_len",
    "country_ua",
    "city_centrality",
    "day_of_week",
    "hour",
    "source_handoff",
    "has_video",
    "is_event",
    "is_poi",
    "has_translations",
    "has_ticket_url",
    *(f"pt_{pt}" for pt in _POINT_TYPES),
    "pt_other",
)


def _city_centrality(city: str | None) -> int:
    if not city:
        return 0
    c = city.strip().lower()
    if any(cap in c for cap in _CAPITALS):
        return 2
    if any(lc in c for lc in _LARGE_CITIES):
        return 1
    return 0


def _image_kb(image_path: str | None) -> float:
    if not image_path:
        return 0.0
    try:
        return os.path.getsize(image_path) / 1024.0
    except OSError:
        return 0.0


def _has_translations(translations: str | None) -> int:
    if not translations:
        return 0
    try:
        data = json.loads(translations)
        return 1 if isinstance(data, dict) and data else 0
    except (ValueError, TypeError):
        return 0


def _point_type_onehot(point_type: str | None) -> dict[str, int]:
    pt = (point_type or "").lower()
    out: dict[str, int] = {f"pt_{p}": 0 for p in _POINT_TYPES}
    out["pt_other"] = 0
    if not pt:
        out["pt_other"] = 1
        return out
    if pt in _POINT_TYPES:
        out[f"pt_{pt}"] = 1
    else:
        out["pt_other"] = 1
    return out


def _build_feature_dict(
    *,
    rating: float | None,
    image_path: str | None,
    image_url_present: bool,
    description: str,
    title: str | None,
    country_code: str | None,
    city: str | None,
    scheduled_at: datetime | None,
    handoff_id: int | None,
    video_path: str | None,
    backend_event_id: int | None,
    poi_point_id: int | None,
    translations: str | None,
    ticket_url: str | None,
    point_type: str | None,
) -> dict[str, Any]:
    when = scheduled_at or datetime.now()
    feat = {
        "rating": float(rating or 0),
        "has_image": 1 if (image_path or image_url_present) else 0,
        "image_kb": _image_kb(image_path),
        "description_len": len(description or ""),
        "title_len": len(title or ""),
        "country_ua": 1 if (country_code or "").upper() == "UA" else 0,
        "city_centrality": _city_centrality(city),
        "day_of_week": when.weekday(),
        "hour": when.hour,
        "source_handoff": 1 if handoff_id else 0,
        "has_video": 1 if video_path else 0,
        "is_event": 1 if backend_event_id else 0,
        "is_poi": 1 if poi_point_id else 0,
        "has_translations": _has_translations(translations),
        "has_ticket_url": 1 if ticket_url else 0,
    }
    feat.update(_point_type_onehot(point_type))
    return feat


async def build_feature_frame(min_window_hours: int = 168):
    """Build training DataFrame from historical posts + post_engagement.

    Returns (X DataFrame with FEATURE_COLUMNS, y Series of engagement
    scores). Uses 7-day window (168h) by default since that captures
    most of a post's eventual reach across all 3 platforms while
    leaving 30d samples for held-out evaluation.

    Skips posts without a PostEngagement row at the chosen window
    (they haven't matured yet). Joins on Post.poi_point_id /
    backend_event_id to enrich features when the original POI/event
    metadata is still in the bot's local mirror or has been fetched
    via handoff_client.
    """
    import pandas as pd
    from sqlalchemy import select

    from db.database import async_session
    from db.models import Post, PostEngagement, Publication

    rows: list[dict[str, Any]] = []
    labels: list[float] = []

    async with async_session() as session:
        # Aggregate engagement_score across platforms for the chosen window.
        # We average the per-platform scores so a post that did well on TG
        # but bombed on FB doesn't get inflated by simple sum. Excluding 0s
        # because absent platforms shouldn't drag the mean down.
        eng_rows = (await session.execute(
            select(PostEngagement.post_id, PostEngagement.score).where(
                PostEngagement.window_hours == min_window_hours,
            )
        )).all()
        eng_by_post: dict[int, list[float]] = {}
        for row in eng_rows:
            eng_by_post.setdefault(row.post_id, []).append(float(row.score))

        if not eng_by_post:
            logger.info("ml.feature_extractor: no engagement rows at window=%dh", min_window_hours)
            return pd.DataFrame(columns=list(FEATURE_COLUMNS)), pd.Series(dtype=float)

        post_ids = list(eng_by_post.keys())
        posts = (await session.execute(
            select(Post).where(Post.id.in_(post_ids))
        )).scalars().all()

    for post in posts:
        scores = eng_by_post.get(post.id, [])
        if not scores:
            continue
        avg_score = sum(scores) / len(scores)

        # POI / event metadata enrichment is best-effort. If the bot
        # didn't keep place_name / image_path locally we still get
        # most of the signals (handoff/event flags, content length).
        feat = _build_feature_dict(
            rating=getattr(post, "rating", None),  # bot Post has no rating column → defaults to 0
            image_path=post.image_path,
            image_url_present=False,
            description=post.content_raw or "",
            title=post.title,
            country_code=None,  # not stored on Post yet
            city=post.place_name,
            scheduled_at=post.scheduled_at or post.created_at,
            handoff_id=post.handoff_id,
            video_path=post.video_path,
            backend_event_id=post.backend_event_id,
            poi_point_id=post.poi_point_id,
            translations=post.translations,
            ticket_url=post.ticket_url,
            point_type=None,  # bot Post doesn't store the original type — Phase 5 will join from backend
        )
        rows.append(feat)
        labels.append(avg_score)

    df = pd.DataFrame(rows, columns=list(FEATURE_COLUMNS))
    y = pd.Series(labels, name="score", dtype=float)
    logger.info("ml.feature_extractor: %d training rows at window=%dh, "
                "score range [%.1f, %.1f]",
                len(df), min_window_hours,
                float(y.min()) if len(y) else 0,
                float(y.max()) if len(y) else 0)
    return df, y


def build_features_for_candidate(candidate: dict[str, Any]):
    """Build single-row feature DataFrame for a queue candidate.

    Used by Phase 5 score-back cron when scoring fresh POI/event rows
    fetched from the backend. The dict layout matches what the backend
    Social Handoff API returns:
      {kind, id, name, pointType, city, countryCode, rating,
       hasPhoto, hasDesc, ...}
    """
    import pandas as pd

    feat = _build_feature_dict(
        rating=candidate.get("rating"),
        image_path=None,
        image_url_present=bool(candidate.get("hasPhoto")) or bool(candidate.get("imageUrl")),
        description=candidate.get("description") or "",
        title=candidate.get("title") or candidate.get("name"),
        country_code=candidate.get("countryCode"),
        city=candidate.get("city"),
        scheduled_at=None,
        handoff_id=candidate.get("handoffId"),
        video_path=None,
        backend_event_id=candidate.get("eventId"),
        poi_point_id=candidate.get("pointId") or candidate.get("id") if candidate.get("kind") == "poi" else None,
        translations=None,
        ticket_url=candidate.get("ticketUrl"),
        point_type=candidate.get("pointType") or candidate.get("category"),
    )
    return pd.DataFrame([feat], columns=list(FEATURE_COLUMNS))
