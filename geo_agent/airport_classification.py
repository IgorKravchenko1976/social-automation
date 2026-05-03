"""Python mirror of imin-backend handler.ClassificationKey.

The backend's /next-airport response includes `classificationKey` directly
(authoritative). This module exists for two reasons:

  1. Offline tests / unit tests that don't hit the live backend.
  2. Fallback when the backend response is missing classificationKey
     (older deploy that hasn't shipped the Phase 1 backend yet — extremely
     unlikely after rollout, but cheap to be defensive).

CRITICAL: keep this in sync with imin-backend/api/internal/handler/
airports_classification.go::ClassificationKey. Whenever you change a case
there, update both this function AND tests/test_airport_classification.py.

The contract is documented in detail in the Go file; see the table at
the top of airports_classification.go for the full mapping.
"""
from __future__ import annotations


def classification_key(facility_type: str, size_class: str) -> str:
    """Compute (facility_type, size_class) → enum-like key.

    Mirrors handler.ClassificationKey in Go. Always returns a non-empty
    string; defaults to 'transport' for unknown facility types so callers
    can rely on a media template / DALL-E prompt existing for any input.
    """
    if facility_type == "airport":
        if size_class == "hub":
            return "airport_hub"
        if size_class == "intl":
            return "airport_intl"
        if size_class == "regional":
            return "airport_regional"
        if size_class == "small":
            return "airport_small"
        # 'unknown' or empty → safe default: regional. Avoids promoting
        # a random small airfield into glossy hub template imagery;
        # downgrade rather than upgrade on uncertainty.
        return "airport_regional"

    if facility_type == "aerodrome":
        return "aerodrome"
    if facility_type == "heliport":
        return "heliport"
    if facility_type == "military_airbase":
        return "military"

    if facility_type == "railway_station":
        if size_class == "hub":
            return "railway_hub"
        return "railway"

    if facility_type == "bus_station":
        return "bus"

    return "transport"


def resolve_classification_key(task) -> str:
    """Pick the classification key for an AirportTask: prefer the
    backend-supplied value, fall back to local computation.

    `task` is duck-typed: needs `.classification_key`, `.facility_type`,
    `.size_class` attributes. Used by airport_processor when selecting
    DALL-E prompts and (future) default-image lookups.
    """
    key = getattr(task, "classification_key", "") or ""
    if key:
        return key
    return classification_key(
        getattr(task, "facility_type", "") or "",
        getattr(task, "size_class", "") or "",
    )
