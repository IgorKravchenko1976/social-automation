"""HTTP client for the backend social hand-off API (added 2026-05-02).

Wraps the two endpoints introduced in imin-backend MR !237:

    GET  /v1/api/social/next-batch        — claim up to N work items
    POST /v1/api/social/report-result     — close a claim

The point of going through this client (instead of the bot keeping its
own QUEUED state) is that the BACKEND owns dedup, scoring, lease
expiry and retry policy. The bot is a dumb consumer: it asks for a
batch, publishes it, reports the result, and never decides what to
publish next on its own. See incident 2026-05-02 ("no posts for 9h"
caused by the bot's retry loop resurrecting permanently-broken pubs)
for the why.

This module is intentionally INDEPENDENT from any AI / research code
path (see geo_agent/*). Sharing rate-limit budget with the long-form
researcher used to throttle both — this client never touches OpenAI
or Perplexity, only the backend's own DB-backed queue.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
DEFAULT_BATCH = 3


@dataclass(slots=True)
class HandoffItem:
    """One claim returned by /next-batch.

    `handoff_id` is the lease handle the bot must echo back in
    /report-result. `source_kind` is currently always 'city_event'
    but the API is forward-compatible with 'map_point' and 'event'
    (see backend handler/social_handoff.go header).
    `preview` carries lightweight metadata so the post creator can
    cheap-skip without a second backend call (e.g. has_photo=False
    is a hard veto for IG-only posts).
    """
    handoff_id: int
    source_kind: str
    source_id: int
    expires_at: str
    attempts: int
    priority_hint: float
    preview: dict


def is_configured() -> bool:
    return bool(settings.imin_backend_api_base and settings.imin_backend_sync_key)


def _headers() -> dict[str, str]:
    return {"X-Sync-Key": settings.imin_backend_sync_key}


def _base() -> str:
    return settings.imin_backend_api_base.rstrip("/")


async def next_batch(n: int = DEFAULT_BATCH, client_id: str = "social-bot") -> list[HandoffItem]:
    """Claim up to N candidates from the backend queue.

    Each returned item carries an opaque `handoff_id` that the caller
    MUST close with report_result() (success or failure). Failing to
    do so is fine — the backend lease will expire after ~15 min and
    the row will re-enter the pool — but every "lost" lease counts
    against the row's attempt budget so a chronic crashing path will
    eventually be auto-promoted to failed_permanent.
    """
    if not is_configured():
        return []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
        resp = await http.get(
            f"{_base()}/v1/api/social/next-batch",
            headers=_headers(),
            params={"n": n, "client": client_id},
        )
        resp.raise_for_status()
        body = resp.json()
    items_raw = body.get("items") or []
    out: list[HandoffItem] = []
    for it in items_raw:
        try:
            out.append(HandoffItem(
                handoff_id=int(it["handoffId"]),
                source_kind=str(it["sourceKind"]),
                source_id=int(it["sourceId"]),
                expires_at=str(it.get("expiresAt") or ""),
                attempts=int(it.get("attempts") or 1),
                priority_hint=float(it.get("priorityHint") or 0.0),
                preview=dict(it.get("preview") or {}),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("[handoff] bad batch item %r: %s", it, exc)
    return out


async def report_result(
    handoff_id: int,
    *,
    result: str,
    reason: str = "",
    social_post_id: Optional[int] = None,
    social_links: Optional[dict[str, str]] = None,
    social_extra_sources: Optional[list[dict]] = None,
    blog_html_path: str = "",
) -> dict[str, Any]:
    """Close a hand-off claim.

    Allowed `result` values:
      published         — terminal SUCCESS. Backend stamps
                          posted_to_social_at + applies any writeback
                          payload (social_links, social_extra_sources).
      failed_permanent  — never retry (e.g. structural defect, dupe).
      failed_transient  — retry on next rebuild; backend auto-promotes
                          to failed_permanent after SocialMaxAttempts.
      skipped           — bot decided to drop (counts toward attempts).

    Returning the lease handle on every code path matters: an unreturned
    handle is the failure mode that turned the legacy /mark-posted into
    an infinite loop on transient errors.
    """
    if not is_configured():
        return {}
    if result not in ("published", "failed_permanent", "failed_transient", "skipped"):
        raise ValueError(f"unknown result {result!r}")

    payload: dict[str, Any] = {"handoffId": handoff_id, "result": result}
    if reason:
        payload["reason"] = reason[:300]
    if social_post_id:
        payload["socialPostId"] = social_post_id
    if social_links:
        payload["socialLinks"] = social_links
    if social_extra_sources:
        payload["socialExtraSources"] = social_extra_sources
    if blog_html_path:
        payload["blogHtmlPath"] = blog_html_path

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
        resp = await http.post(
            f"{_base()}/v1/api/social/report-result",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_city_event_payload(city_event_id: int) -> Optional[dict]:
    """Pull a single city_event by id for post composition.

    /next-batch returns only a tiny preview (title, city, hasPhoto, ...)
    so the bot can fast-veto. To actually compose a post we need the
    full payload — title, description, translations, venue, ticket url
    — which lives behind the public-event endpoint we already use for
    deep links (no auth required, but we send X-Sync-Key anyway for
    log-trace consistency).
    """
    if not is_configured():
        return None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
        resp = await http.get(
            f"{_base()}/v1/api/city-events/{city_event_id}/public",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
