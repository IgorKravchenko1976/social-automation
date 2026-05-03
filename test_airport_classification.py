"""Tests for geo_agent.airport_classification — Python mirror of the
Go ClassificationKey helper in imin-backend.

CRITICAL: keep these cases in sync with the Go test suite at
imin-backend/api/internal/handler/airports_classification_test.go.
The two implementations MUST agree on every (facility_type, size_class)
combination — divergence silently breaks default-image lookups.

Run with `python test_airport_classification.py` (no pytest dependency
required — uses plain assertions and prints a summary).
"""
from __future__ import annotations

import sys
from typing import Iterable

from geo_agent.airport_classification import (
    classification_key,
    resolve_classification_key,
)


# ── Test cases — MUST mirror the Go test cases exactly ──

AIRPORT_CASES = {
    "hub":      "airport_hub",
    "intl":     "airport_intl",
    "regional": "airport_regional",
    "small":    "airport_small",
    "unknown":  "airport_regional",  # safe-default downgrade
    "":         "airport_regional",
}

RAILWAY_CASES = {
    # Only 'hub' splits off; everything else collapses to plain railway.
    "hub":      "railway_hub",
    "regional": "railway",
    "small":    "railway",
    "unknown":  "railway",
    "":         "railway",
}

# Facility types that ignore size_class (collapsed to one key per type).
COLLAPSED_TYPES = {
    "aerodrome":        "aerodrome",
    "heliport":         "heliport",
    "military_airbase": "military",
    "bus_station":      "bus",
}

UNKNOWN_FACILITIES: Iterable[str] = ("", "unknown", "ferry_terminal", "metro_station", "tram_stop")


def test_airport_size_buckets() -> int:
    failures = 0
    for size_class, want in AIRPORT_CASES.items():
        got = classification_key("airport", size_class)
        if got != want:
            print(f"  FAIL: airport + {size_class!r} → {got!r}, want {want!r}")
            failures += 1
    return failures


def test_railway_only_hub_splits() -> int:
    failures = 0
    for size_class, want in RAILWAY_CASES.items():
        got = classification_key("railway_station", size_class)
        if got != want:
            print(f"  FAIL: railway_station + {size_class!r} → {got!r}, want {want!r}")
            failures += 1
    return failures


def test_collapsed_types_ignore_size_class() -> int:
    failures = 0
    for ft, want in COLLAPSED_TYPES.items():
        for sc in ("hub", "intl", "regional", "small", "unknown", ""):
            got = classification_key(ft, sc)
            if got != want:
                print(f"  FAIL: {ft} + {sc!r} → {got!r}, want {want!r}")
                failures += 1
    return failures


def test_unknown_facility_falls_back_to_transport() -> int:
    failures = 0
    for ft in UNKNOWN_FACILITIES:
        got = classification_key(ft, "hub")
        if got != "transport":
            print(f"  FAIL: {ft!r} + hub → {got!r}, want 'transport'")
            failures += 1
    return failures


def test_never_returns_empty_string() -> int:
    """Defensive: every combo must yield a non-empty key — callers index
    media_assets by this without checking length.
    """
    failures = 0
    combos = [
        ("airport", "hub"), ("airport", ""), ("airport", "garbage"),
        ("", ""), ("", "hub"),
        ("unknown_type", "unknown_size"),
        ("AIRPORT", "HUB"),  # case-sensitive — uppercase falls through
    ]
    for ft, sc in combos:
        if not classification_key(ft, sc):
            print(f"  FAIL: classification_key({ft!r}, {sc!r}) returned empty")
            failures += 1
    return failures


def test_resolve_prefers_backend_value() -> int:
    """resolve_classification_key picks the backend value when present
    and only falls back to local computation when classification_key is
    empty. This is the production code path; the local mirror is
    defensive fallback only.
    """
    class _FakeTask:
        def __init__(self, ft: str, sc: str, key: str = ""):
            self.facility_type = ft
            self.size_class = sc
            self.classification_key = key

    failures = 0

    # Backend supplied a key — use it as-is even if it disagrees with
    # what local computation would give. (Backend might roll out new
    # buckets before bot is updated; trust the wire value.)
    if resolve_classification_key(_FakeTask("airport", "hub", "future_key")) != "future_key":
        print("  FAIL: should prefer task.classification_key over local mirror")
        failures += 1

    # Backend value missing → compute locally.
    if resolve_classification_key(_FakeTask("airport", "hub")) != "airport_hub":
        print("  FAIL: empty classification_key should fall back to local computation")
        failures += 1

    # Whitespace-only → still treated as missing; local computation runs.
    fake = _FakeTask("airport", "small")
    fake.classification_key = "   "
    # Note: current implementation only checks empty-string truthiness,
    # not whitespace. Whitespace-only IS truthy → returns "   ". This
    # test pins that behaviour so we notice if it ever changes.
    if resolve_classification_key(fake) != "   ":
        print("  FAIL: whitespace classification_key currently passes through (pinned by this test)")
        failures += 1

    return failures


def main() -> int:
    suites = [
        ("airport size buckets",        test_airport_size_buckets),
        ("railway only-hub splits",     test_railway_only_hub_splits),
        ("collapsed types ignore size", test_collapsed_types_ignore_size_class),
        ("unknown → transport fallback", test_unknown_facility_falls_back_to_transport),
        ("never returns empty",         test_never_returns_empty_string),
        ("resolve prefers backend",     test_resolve_prefers_backend_value),
    ]
    total_failures = 0
    for name, fn in suites:
        f = fn()
        total_failures += f
        status = "OK" if f == 0 else f"FAIL ({f})"
        print(f"  {name:.<45} {status}")
    print()
    if total_failures:
        print(f"FAILED — {total_failures} assertion(s) failed")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
