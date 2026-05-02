"""City Pulse publisher driven by the backend hand-off API.

This is the v2 of publish_city_pulse_queue. Instead of polling our own
QUEUED Publication rows (the old design that turned a handful of
broken Instagram posts into a 9-hour outage on 2026-05-02), we ask
the backend for a small batch of pre-deduplicated, scored,
already-leased candidates and report each one's outcome back.

Cycle (every 15 min, when settings.use_handoff_api is true):

    1. handoff_client.next_batch(n=3)
    2. For each item:
        a. Fetch full city_event payload from backend (translations,
           description, ticket_url, ...).
        b. Reuse city_pulse_post_creator.prepare_local_post_for_event
           to (i) run quality gates, (ii) download thumbnail, (iii)
           insert Post + Publication rows in the bot's local DB. This
           keeps the rest of the publishing pipeline (text generation,
           fact-checking, blog page, writeback) untouched.
        c. _try_publish_post — the existing publisher entry point.
        d. handoff_client.report_result — close the lease with
           published / failed_permanent / failed_transient.
        e. _writeback_post_to_source — preserved from legacy code so
           social_links / blog_html_path / posted_to_social_at land
           on the city_event row exactly the same way they did before
           and the public /pulse/{id} page keeps working without
           any frontend change.

AI budget note
    The bot's text composition + fact-checking call OpenAI per platform
    inside _try_publish_post. THIS pipeline does NOT touch the
    Perplexity research key or the long-form description generator
    used by geo_agent/* — those have their own queues. A bursty social
    run can no longer starve the researcher even if both happen to
    share the same API key, because the backend side caps the batch
    size to SocialMaxBatch=10.

Rollback
    Set settings.use_handoff_api=false and APScheduler will keep
    calling the legacy publish_city_pulse_queue. All data structures
    are forward-compatible (the legacy endpoint still works on the
    backend side too).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import select

from db.database import async_session
from db.models import Post, Publication, PostStatus

from scheduler import handoff_client
from scheduler.city_pulse_post_creator import prepare_local_post_for_event

logger = logging.getLogger(__name__)

_handoff_lock = asyncio.Lock()

BATCH_SIZE = 3


# Substrings (case-insensitive) in Publication.error_message that mark a
# STRUCTURAL failure — same input next cycle gives the same output.
# Reporting these as failed_transient triggers backend's auto-retry policy
# and wastes 2 more handoff slots before auto-promote-to-permanent kicks
# in (incident 2026-05-02: McDonald's POI cycled 2× hourly because all
# fails looked transient).
#
# Anything NOT matched here is treated as transient: network blip, API
# timeout, expired token, daily platform cap (will recover next day).
_STRUCTURAL_FAILURE_MARKERS = (
    # Bot-side rejections
    "fact-check rejected",
    "fact_check rejected",
    "fact-checked rejected",
    "stale generic poi",
    "instagram requires image",
    "no real photo",
    "tiktok requires video",
    # Platform-side image-format rejections (Wikidata SVG-as-jpg, 1×1
    # placeholder, broken-header JPEG, etc.). The same image_path will
    # fail again next cycle.
    "image_process_failed",
    "could not get public url for image",
    "invalid parameter",
    # Platform reports the post itself is illegal / banned. Won't
    # change with another retry.
    "permanent error",
    "object with id",
    "duplicate event",
    "already_posted",
)


def _is_structural_error(error_message: str | None) -> bool:
    if not error_message:
        return False
    msg = error_message.lower()
    return any(marker in msg for marker in _STRUCTURAL_FAILURE_MARKERS)


async def _classify_publication_failure(post_id: int) -> tuple[str, str]:
    """Inspect Publications for the post and decide handoff result.

    Called only when _try_publish_post returned success=False (i.e. no
    platform succeeded). Returns ("failed_permanent" | "failed_transient",
    human reason). Treats the failure as permanent only when EVERY non-
    PUBLISHED publication has a structural error_message — one network /
    timeout / token failure is enough to keep the row eligible for retry.

    Why we read DB instead of getting publications passed in: _try_publish_post
    already commits per-pub status changes; reading fresh rows is the
    simplest race-free way to see the final per-platform verdict.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Publication).where(Publication.post_id == post_id)
        )
        pubs = result.scalars().all()

    if not pubs:
        return "failed_transient", f"no publications found for post {post_id}"

    # Anything still QUEUED or PUBLISHING means the publish loop never
    # reached a verdict for this platform (deferred for spacing, exception
    # before assigning status, etc.). Safer to retry.
    for p in pubs:
        if p.status in (PostStatus.QUEUED, PostStatus.PUBLISHING):
            return (
                "failed_transient",
                f"post {post_id}: {p.platform} still {p.status.value}",
            )

    failed_pubs = [p for p in pubs if p.status == PostStatus.FAILED]
    transient_pubs = [
        p for p in failed_pubs if not _is_structural_error(p.error_message)
    ]
    if transient_pubs:
        sample = transient_pubs[0]
        return (
            "failed_transient",
            (
                f"post {post_id}: {sample.platform} transient "
                f"({(sample.error_message or '?')[:120]})"
            ),
        )

    sample = failed_pubs[0] if failed_pubs else None
    sample_msg = (sample.error_message or "?")[:120] if sample else "no failed pubs"
    return (
        "failed_permanent",
        f"post {post_id}: all platforms structural ({sample_msg})",
    )


async def publish_via_handoff() -> int:
    """One full hand-off cycle. Returns number of posts published."""
    async with _handoff_lock:
        if not handoff_client.is_configured():
            logger.debug("[handoff-publish] backend not configured, skipping")
            return 0

        try:
            batch = await handoff_client.next_batch(n=BATCH_SIZE)
        except Exception as exc:
            logger.warning("[handoff-publish] next-batch failed: %s", exc)
            return 0

        if not batch:
            logger.debug("[handoff-publish] empty batch")
            return 0

        logger.info(
            "[handoff-publish] received batch of %d (priorities: %s)",
            len(batch), [round(it.priority_hint, 2) for it in batch],
        )

        published = 0
        for item in batch:
            try:
                ok = await _process_handoff_item(item)
                if ok:
                    published += 1
            except Exception as exc:
                logger.exception(
                    "[handoff-publish] unexpected failure on handoff %d: %s",
                    item.handoff_id, exc,
                )
                await _safe_report(
                    item.handoff_id,
                    result="failed_transient",
                    reason=f"unexpected: {str(exc)[:200]}",
                )
        logger.info("[handoff-publish] published %d/%d", published, len(batch))
        return published


async def _process_handoff_item(item: handoff_client.HandoffItem) -> bool:
    """Handle ONE hand-off claim end-to-end. Returns True iff published."""
    if item.source_kind == "poi":
        return await _process_poi_handoff(item)
    if item.source_kind != "city_event":
        # Unknown kinds (future expansion). Leave the lease to expire
        # naturally — backend will recycle after SocialLeaseDuration.
        await _safe_report(
            item.handoff_id,
            result="skipped",
            reason=f"source_kind={item.source_kind} not yet supported",
        )
        return False

    try:
        event = await handoff_client.fetch_city_event_payload(item.source_id)
    except Exception as exc:
        logger.warning(
            "[handoff-publish] fetch payload for ce=%d failed: %s",
            item.source_id, exc,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason=f"payload fetch: {str(exc)[:160]}",
        )
        return False

    if not event:
        logger.info(
            "[handoff-publish] city_event %d returned 404, marking permanent",
            item.source_id,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_permanent",
            reason="city_event 404 (deleted/archived)",
        )
        return False

    post_id, reject_reason = await prepare_local_post_for_event(event, handoff_id=item.handoff_id)
    if reject_reason == "already_queued_locally":
        # Local Post + Publications already exist for this event from a
        # previous cycle (probably a crash mid-publish). Defer: let
        # legacy publish_city_pulse_queue pick it up next tick.
        logger.info(
            "[handoff-publish] ce=%d already in local queue, deferring",
            item.source_id,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason="local Post exists; legacy publisher will pick up",
        )
        return False
    if reject_reason:
        logger.info(
            "[handoff-publish] ce=%d rejected at quality gate: %s",
            item.source_id, reject_reason,
        )
        # Quality gate failures (no_photo, no_venue, no_uk_translation,
        # title_too_short) are structural and won't change without a
        # researcher pass — mark permanent so the row stops cycling.
        await _safe_report(
            item.handoff_id,
            result="failed_permanent",
            reason=f"quality_gate:{reject_reason}",
        )
        return False
    if post_id is None:
        # db_error path — let it retry.
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason="post creation failed (no id)",
        )
        return False

    # Hand off to the existing publisher pipeline. _try_publish_post
    # internally handles fact-checking, per-platform text gen, image
    # fetch, and writes Publication.status. Returns True iff at least
    # one platform succeeded.
    from scheduler.publisher import _try_publish_post

    async with async_session() as session:
        post = await session.get(Post, post_id)
        if post is None:
            await _safe_report(
                item.handoff_id,
                result="failed_transient",
                reason=f"post {post_id} vanished after creation",
            )
            return False

    try:
        success = await _try_publish_post(post, time_slot=99, content_type="city_pulse")
    except Exception as exc:
        logger.exception(
            "[handoff-publish] publish post=%d threw: %s", post_id, exc,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason=f"publish exception: {str(exc)[:160]}",
        )
        return False

    if success:
        await _safe_report(
            item.handoff_id,
            result="published",
            social_post_id=post_id,
            reason="ok",
        )
        return True

    # Publication failed across all platforms. Classify so the backend
    # doesn't waste 2 more handoff cycles re-issuing the same row when
    # the failure is structural (image format, fact-check, etc.) and
    # cannot improve next attempt. See _classify_publication_failure.
    final_result, reason = await _classify_publication_failure(post_id)
    await _safe_report(item.handoff_id, result=final_result, reason=reason)
    return False


async def _process_poi_handoff(item: handoff_client.HandoffItem) -> bool:
    """Hand-off path for source_kind='poi'. Mirrors the city_event path:
    fetch payload → quality gate → reuse existing publisher pipeline →
    close lease.
    """
    try:
        poi = await handoff_client.fetch_poi_payload(item.source_id)
    except Exception as exc:
        logger.warning(
            "[poi-handoff] fetch payload for poi=%d failed: %s",
            item.source_id, exc,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason=f"payload fetch: {str(exc)[:160]}",
        )
        return False

    if not poi:
        logger.info(
            "[poi-handoff] poi %d returned 404, marking permanent",
            item.source_id,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_permanent",
            reason="poi 404 (deleted/inactive)",
        )
        return False

    from scheduler.post_creator import prepare_local_post_for_poi

    post_id, reject_reason = await prepare_local_post_for_poi(poi, handoff_id=item.handoff_id)
    if reject_reason:
        # Quality gate failures (generic_name, no_description_no_wiki,
        # low_rating) are structural — same POI today and tomorrow has
        # the same data. Mark permanent so it stops cycling.
        logger.info(
            "[poi-handoff] poi=%d rejected: %s",
            item.source_id, reject_reason,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_permanent",
            reason=f"quality_gate:{reject_reason}",
        )
        return False
    if post_id is None:
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason="post creation failed (no id)",
        )
        return False

    from scheduler.publisher import _try_publish_post

    async with async_session() as session:
        post = await session.get(Post, post_id)
        if post is None:
            await _safe_report(
                item.handoff_id,
                result="failed_transient",
                reason=f"post {post_id} vanished after creation",
            )
            return False

    try:
        success = await _try_publish_post(post, time_slot=99, content_type="poi")
    except Exception as exc:
        logger.exception(
            "[poi-handoff] publish post=%d threw: %s", post_id, exc,
        )
        await _safe_report(
            item.handoff_id,
            result="failed_transient",
            reason=f"publish exception: {str(exc)[:160]}",
        )
        return False

    if success:
        await _safe_report(
            item.handoff_id,
            result="published",
            social_post_id=post_id,
            reason="ok",
        )
        return True

    final_result, reason = await _classify_publication_failure(post_id)
    await _safe_report(item.handoff_id, result=final_result, reason=reason)
    return False


async def _safe_report(handoff_id: int, **kwargs) -> None:
    """Best-effort lease close. Lost reports are recovered by the
    backend's ReleaseExpiredSocialLeases cron after the 15-min lease
    deadline, so a transient HTTP failure here is non-fatal."""
    try:
        await handoff_client.report_result(handoff_id, **kwargs)
    except Exception as exc:
        logger.warning(
            "[handoff-publish] report_result(%d, %s) failed: %s; lease will time out",
            handoff_id, kwargs.get("result"), exc,
        )
