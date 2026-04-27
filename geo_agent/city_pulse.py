"""City Pulse — cultural events vertical processor.

Three pipeline stages map to three job types in city_research_jobs:

  1. discover_sources : Perplexity Sonar lists official cinema chains, theaters,
                        concert venues, exhibition spaces, and sale aggregators
                        for a given city. Each candidate is light-validated
                        (HEAD/parse) before being POSTed back to the backend.

  2. verify_source    : Weekly HEAD + content sniff for every active source.
                        Bad responses bump consecutive_failures; three in a
                        row demote the source to 'broken'. Healthy responses
                        revive previously broken sources back to 'active'.

  3. fetch_content    : Daily content pull. RSS / iCal / JSON / sitemap are
                        parsed natively when feed_url is present. Otherwise
                        the raw HTML is handed to GPT-4o-mini with a strict
                        JSON schema for normalization. Each parsed event is
                        translated to 8 languages via the existing translator
                        before being upserted into city_events.

All three stages run on APScheduler intervals and share a single async lock
to keep the bot from over-budgeting any external API.

Backend communication is centralized in `geo_agent.backend_client` — see the
City Pulse helpers added April 2026 (fetch_next_city_pulse_job and friends).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from content.ai_client import get_client
from content.perplexity_client import get_perplexity_client, is_configured as perplexity_configured
from geo_agent import backend_client
from geo_agent.translator import translate_content

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

# Per-cycle caps. Each cycle handles exactly one job to keep behaviour simple
# and predictable; throughput comes from cycle frequency in main.py.
DISCOVER_TIMEOUT = 90
FETCH_TIMEOUT = 60
VERIFY_TIMEOUT = 30

VALID_CATEGORIES = {
    "cinema", "theater", "concert", "exhibition",
    "sale", "festival", "workshop", "tour", "sport",
}

VALID_SOURCE_TYPES = {
    "api_json", "api_ical", "rss", "sitemap", "html_scrape", "other",
}


# ════════════════════════════════════════════════════════════════════════════
# Public entry points called from the APScheduler.
# ════════════════════════════════════════════════════════════════════════════

async def process_city_pulse_discover() -> bool:
    """One discover_sources cycle — find sources for the next queued city."""
    async with _lock:
        if not backend_client.is_configured():
            return False
        try:
            job = await backend_client.fetch_next_city_pulse_job(job_type="discover_sources")
        except Exception as exc:
            logger.warning("[city-pulse] discover: fetch job failed: %s", exc)
            return False
        if job is None:
            return False
        return await _handle_discover_job(job)


async def process_city_pulse_verify() -> bool:
    """One verify_source cycle."""
    async with _lock:
        if not backend_client.is_configured():
            return False
        try:
            job = await backend_client.fetch_next_city_pulse_job(job_type="verify_source")
        except Exception as exc:
            logger.warning("[city-pulse] verify: fetch job failed: %s", exc)
            return False
        if job is None or job.source is None:
            return False
        return await _handle_verify_job(job)


async def process_city_pulse_fetch() -> bool:
    """One fetch_content cycle."""
    async with _lock:
        if not backend_client.is_configured():
            return False
        try:
            job = await backend_client.fetch_next_city_pulse_job(job_type="fetch_content")
        except Exception as exc:
            logger.warning("[city-pulse] fetch: fetch job failed: %s", exc)
            return False
        if job is None or job.source is None:
            return False
        return await _handle_fetch_job(job)


# ════════════════════════════════════════════════════════════════════════════
# Stage 1 — discover sources via Perplexity.
# ════════════════════════════════════════════════════════════════════════════

DISCOVER_PROMPT = """You are a cultural events researcher. The user is in a specific city
and wants to know about cinemas, theaters, concert venues, exhibitions,
seasonal sales, and festivals. Your job: list verified, official websites
that publish current programs / schedules / announcements.

Return ONLY valid JSON in this exact shape:
{
  "sources": [
    {
      "name": "Pathé Cinemas",
      "homepageUrl": "https://www.pathe.fr/",
      "feedUrl": "https://www.pathe.fr/rss",
      "apiEndpoint": "",
      "sourceType": "rss",
      "categories": ["cinema"],
      "language": "fr",
      "notes": "Major cinema chain"
    }
  ]
}

Rules:
1. Up to 8 sources per city. Quality over quantity.
2. Allowed categories: cinema, theater, concert, exhibition, sale, festival, workshop, tour, sport.
3. Allowed sourceType: api_json, api_ical, rss, sitemap, html_scrape, other.
4. Prefer feed_url (RSS/iCal/sitemap/JSON) over plain html_scrape.
5. ONLY official venue/operator domains, NOT aggregators that resell tickets.
6. EXCEPTION for Ukrainian cities: official local ticket platforms ARE allowed
   (e.g. kontramarka.ua, concert.ua, karabas.com, city.kyiv.ua, kyivnotkyiv.com).
7. NO Russian / Belarusian / occupied-territory sources, never.
8. NO domains ending in .ru.
9. NO duplicates.
10. If unsure about feedUrl, leave it empty — backend will handle html_scrape.
11. Return ONLY JSON. No prose, no markdown."""


async def _handle_discover_job(job: backend_client.CityPulseJob) -> bool:
    logger.info("[city-pulse] discover: %s, %s (priority=%.2f)",
                job.city, job.country_code, job.priority)

    if not perplexity_configured():
        logger.warning("[city-pulse] discover: Perplexity not configured, marking failed")
        await backend_client.submit_sources_discovered(
            job.id, job.country_code, job.city, [],
            failed=True, error="perplexity_not_configured")
        return False

    try:
        candidates = await _ask_perplexity_for_sources(job.country_code, job.city)
    except Exception as exc:
        logger.exception("[city-pulse] discover: Perplexity failed: %s", exc)
        await backend_client.submit_sources_discovered(
            job.id, job.country_code, job.city, [],
            failed=True, error=str(exc)[:200])
        return False

    validated: list[dict] = []
    for c in candidates:
        cleaned = _normalize_candidate(c)
        if cleaned is None:
            continue
        ok = await _light_validate(cleaned["homepageUrl"])
        if not ok:
            logger.info("[city-pulse] discover: drop %s (HEAD/parse failed)",
                        cleaned["homepageUrl"])
            continue
        validated.append(cleaned)

    logger.info("[city-pulse] discover: %d/%d candidates passed validation",
                len(validated), len(candidates))

    await backend_client.submit_sources_discovered(
        job.id, job.country_code, job.city, validated)
    return True


async def _ask_perplexity_for_sources(country: str, city: str) -> list[dict]:
    client = get_perplexity_client()
    if client is None:
        raise RuntimeError("Perplexity client not configured")

    user = (
        f"List up to 8 official sources for cultural events (cinema, theater, "
        f"concerts, exhibitions, sales, festivals) in {city}, {country}. "
        f"Include their websites, RSS/iCal/sitemap feeds, or public APIs."
    )
    resp = await client.chat.completions.create(
        model="sonar",
        messages=[
            {"role": "system", "content": DISCOVER_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=2000,
    )

    content = resp.choices[0].message.content or ""
    return _extract_sources_json(content)


def _extract_sources_json(text: str) -> list[dict]:
    """Robustly pull `sources: [...]` out of Perplexity output."""
    if not text:
        return []

    # First try plain json.loads.
    try:
        parsed = json.loads(text)
        return list(parsed.get("sources", []))
    except Exception:
        pass

    # Fallback: regex-extract first {...} block and parse it.
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
        return list(parsed.get("sources", []))
    except Exception:
        return []


def _normalize_candidate(raw: dict) -> dict | None:
    """Sanitize one source from the LLM. Returns None if unsalvageable."""
    if not isinstance(raw, dict):
        return None

    homepage = (raw.get("homepageUrl") or "").strip()
    if not homepage or not _looks_like_url(homepage):
        return None
    if homepage.endswith(".ru") or ".ru/" in homepage:
        return None

    name = (raw.get("name") or "").strip()
    if not name:
        # Last-resort name fallback so backend has *something*.
        try:
            name = urlparse(homepage).hostname or homepage[:80]
        except Exception:
            name = homepage[:80]
    name = name[:200]

    stype = (raw.get("sourceType") or "html_scrape").lower().strip()
    if stype not in VALID_SOURCE_TYPES:
        stype = "html_scrape"

    cats_raw = raw.get("categories") or []
    if isinstance(cats_raw, str):
        cats_raw = [cats_raw]
    cats = []
    seen = set()
    for c in cats_raw:
        c = (c or "").lower().strip()
        if c in VALID_CATEGORIES and c not in seen:
            cats.append(c)
            seen.add(c)
    if not cats:
        cats = ["festival"]

    return {
        "name": name,
        "homepageUrl": homepage,
        "feedUrl": (raw.get("feedUrl") or "").strip(),
        "apiEndpoint": (raw.get("apiEndpoint") or "").strip(),
        "sourceType": stype,
        "categories": cats,
        "language": (raw.get("language") or "").strip().lower()[:5],
        "notes": (raw.get("notes") or "").strip()[:500],
    }


def _looks_like_url(s: str) -> bool:
    if not s or len(s) > 1000:
        return False
    if not (s.startswith("http://") or s.startswith("https://")):
        return False
    if " " in s:
        return False
    try:
        parsed = urlparse(s)
        return bool(parsed.netloc)
    except Exception:
        return False


async def _light_validate(url: str) -> bool:
    """HEAD the URL and accept any 2xx/3xx response.

    Some venue sites block HEAD; fall back to a tiny GET in that case.
    """
    headers = {"User-Agent": "im-in/city-pulse-discover"}
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True, headers=headers,
        ) as client:
            try:
                resp = await client.head(url)
                if 200 <= resp.status_code < 400:
                    return True
            except httpx.HTTPError:
                pass
            resp = await client.get(url)
            return 200 <= resp.status_code < 400
    except Exception as exc:
        logger.debug("[city-pulse] validate %s: %s", url, exc)
        return False


# ════════════════════════════════════════════════════════════════════════════
# Stage 2 — verify a source (weekly).
# ════════════════════════════════════════════════════════════════════════════

async def _handle_verify_job(job: backend_client.CityPulseJob) -> bool:
    src = job.source
    target_url = src.feed_url or src.api_endpoint or src.homepage_url
    logger.info("[city-pulse] verify: source=%d %s (%s)", src.id, src.name, target_url)

    started = time.monotonic()
    http_status = 0
    parse_ok = False
    error_msg = ""

    headers = {"User-Agent": "im-in/city-pulse-verify"}
    try:
        async with httpx.AsyncClient(
            timeout=VERIFY_TIMEOUT, follow_redirects=True, headers=headers,
        ) as client:
            resp = await client.get(target_url)
            http_status = resp.status_code
            if 200 <= resp.status_code < 400:
                parse_ok = _quick_sniff(resp.text, src.source_type)
    except Exception as exc:
        error_msg = str(exc)[:200]

    duration_ms = int((time.monotonic() - started) * 1000)
    await backend_client.submit_source_verified(
        job.id, src.id,
        http_status=http_status,
        parse_ok=parse_ok,
        new_events_found=0,
        total_events_seen=0,
        duration_ms=duration_ms,
        error_message=error_msg,
    )
    return True


def _quick_sniff(body: str, source_type: str) -> bool:
    """Cheap parse heuristic per source type."""
    if not body:
        return False
    body = body[:50_000]  # cap memory
    lower = body.lower()

    if source_type == "rss":
        return "<rss" in lower or "<feed" in lower or "<channel" in lower
    if source_type == "api_ical":
        return body.startswith("BEGIN:VCALENDAR")
    if source_type == "api_json":
        try:
            json.loads(body)
            return True
        except Exception:
            return False
    if source_type == "sitemap":
        return "<urlset" in lower or "<sitemapindex" in lower
    # html_scrape / other — accept any HTML page
    return "<html" in lower or "<!doctype" in lower or "<body" in lower


# ════════════════════════════════════════════════════════════════════════════
# Stage 3 — fetch & normalize content (daily).
# ════════════════════════════════════════════════════════════════════════════

NORMALIZE_PROMPT = """You receive raw web content from a single cultural-events source.
Your job: extract a list of upcoming events/screenings/exhibitions/sales/festivals
and return them as STRICT JSON in this shape.

Category mapping cheat sheet (when source content is in Ukrainian / English):
  - кіно / cinema / movie / показ → cinema
  - театр / theater / вистава / play / opera / ballet → theater
  - концерт / concert / live music / трибʼют / музика при свічках → concert
  - стендап / stand-up / impro / комедія в клубі → workshop (live comedy)
  - виставка / exhibition / museum / галерея → exhibition
  - акція / sale / знижки / happy hour / all inclusive bar → sale
  - фестиваль / festival / picnic / open-air → festival
  - квест / tour / екскурсія / прогулянка → tour
  - майстер-клас / workshop / lecture → workshop
  - спорт / sport / football / soccer / tennis / basketball / match / матч / гра /
    rugby / boxing / MMA / volleyball / hockey → sport
    IMPORTANT: If the title or description contains team names (e.g. "Динамо",
    "Шахтар", "Барселона", "Real Madrid", "Lakers"), or words like "матч",
    "match", "game", "derby", "cup", "championship", "league", "турнір",
    "чемпіонат", "ліга", "кубок", "стадіон", "stadium" — it is ALWAYS sport,
    NEVER concert. Sports teams playing at a stadium = sport, not concert.

{
  "events": [
    {
      "externalId": "stable id from source (URL/permalink/slug) — MUST be present",
      "title": "Short title in source language",
      "description": "1-3 sentence overview",
      "category": "cinema|theater|concert|exhibition|sale|festival|workshop|tour|sport",
      "startsAt": "RFC3339 UTC, e.g. 2026-05-14T19:30:00Z",
      "endsAt": "RFC3339 UTC or null",
      "durationMinutes": 120,
      "venueName": "Venue name in ORIGINAL language as on the page",
      "venueNameUk": "Venue name translated to Ukrainian (e.g. Національна опера України)",
      "venueAddress": "Street address if known",
      "latitude": 0.0,
      "longitude": 0.0,
      "priceFrom": null,
      "priceTo": null,
      "currency": "EUR|USD|UAH|...",
      "ticketUrl": "Direct booking link if present, else empty",
      "thumbnailUrl": "Image URL (poster, banner, photo) — MANDATORY if present on page",
      "photos": ["additional image URLs if available"],
      "ageLimit": null,
      "spokenLanguage": "uk|en|...",
      "facilities": {"parking": true, "wheelchair": false, "wifi": true},
      "transportInfo": "How to get there: metro station, bus, parking address",
      "whatToBring": "What to bring: comfortable shoes, warm jacket, etc.",
      "meta": {}
    }
  ]
}

Rules:
- Up to 50 events per call. Extract ALL upcoming events you can find.
- Skip past events (older than today).
- NEVER invent dates, prices, venues — leave the field empty/null when unknown.
- Use the source language for title/description (we translate later).
- venueName: keep the ORIGINAL name as written on the page (e.g. "Kyiv Opera House").
- venueNameUk: ALWAYS provide the Ukrainian name (e.g. "Київська опера").
  For Ukrainian sources both fields may be the same. For English/other sources,
  translate the venue name to Ukrainian. Use the official Ukrainian name if known.
- externalId MUST be unique within this source. Prefer the canonical URL or slug.
- thumbnailUrl is CRITICAL — look for event poster, banner, og:image, hero image.
  If the page has any event image at all, extract it. Check <img>, <meta og:image>,
  background-image CSS, data-src attributes.
- durationMinutes: estimate from endsAt-startsAt, or from typical event length
  (cinema ~120, concert ~150, theater ~120, exhibition ~60, workshop ~90).
  Use null only if truly impossible to estimate.
- facilities: extract any mention of parking, wheelchair access, wifi,
  air conditioning, wardrobe, bar, restaurant, children's room.
  Use empty object {} if nothing mentioned.
- transportInfo: extract metro station, bus routes, parking, address directions.
  Leave empty if not mentioned.
- whatToBring: extract recommendations about dress code, equipment, documents.
  Leave empty if not mentioned.
- Return ONLY JSON, no commentary."""


async def _handle_fetch_job(job: backend_client.CityPulseJob) -> bool:
    src = job.source
    target_url = src.feed_url or src.api_endpoint or src.homepage_url
    logger.info("[city-pulse] fetch: source=%d %s", src.id, target_url)

    raw = await _download_text(target_url)
    if not raw:
        await backend_client.submit_events_imported(
            job.id, src.id, [], failed=True, error="empty_or_unreachable")
        return False

    events = await _normalize_events(raw, src)
    if not events:
        await backend_client.submit_events_imported(job.id, src.id, [])
        return True

    enriched = await _attach_translations(events, src.language or "en")

    await backend_client.submit_events_imported(job.id, src.id, enriched)
    return True


async def _download_text(url: str) -> str:
    headers = {"User-Agent": "im-in/city-pulse-fetch"}
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers,
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return ""
            return resp.text[:120_000]  # GPT context cap
    except Exception as exc:
        logger.warning("[city-pulse] fetch download %s: %s", url, exc)
        return ""


async def _normalize_events(raw: str, src: backend_client.CityPulseSource) -> list[dict]:
    client = get_client()
    if client is None:
        return []

    user_msg = (
        f"Source name: {src.name}\n"
        f"Source URL: {src.feed_url or src.homepage_url}\n"
        f"City: {src.city}, country: {src.country_code}\n"
        f"Source language: {src.language or 'auto'}\n"
        f"Allowed categories for THIS source (prefer these): "
        f"{', '.join(src.categories) if src.categories else 'any'}\n\n"
        f"Raw content (truncated):\n{raw}"
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": NORMALIZE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=12000,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        items = data.get("events") or []
    except Exception as exc:
        logger.warning("[city-pulse] normalize: GPT failed: %s", exc)
        return []

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        cat = (item.get("category") or "festival").lower().strip()
        if cat not in VALID_CATEGORIES:
            cat = src.categories[0] if src.categories else "festival"
        photos_raw = item.get("photos") or []
        if not isinstance(photos_raw, list):
            photos_raw = []
        photos = [str(p).strip()[:1000] for p in photos_raw if isinstance(p, str) and p.strip()][:10]

        facilities_raw = item.get("facilities")
        facilities = facilities_raw if isinstance(facilities_raw, dict) else {}

        duration = item.get("durationMinutes")
        if duration is not None:
            try:
                duration = int(duration)
                if duration <= 0 or duration > 1440:
                    duration = None
            except (TypeError, ValueError):
                duration = None

        out.append({
            "externalId": (item.get("externalId") or "").strip()[:200],
            "title": title[:500],
            "description": (item.get("description") or "")[:5000],
            "category": cat,
            "startsAt": item.get("startsAt"),
            "endsAt": item.get("endsAt"),
            "durationMinutes": duration,
            "venueName": (item.get("venueName") or "")[:500],
            "venueNameUk": (item.get("venueNameUk") or "")[:500],
            "venueAddress": (item.get("venueAddress") or "")[:500],
            "latitude": _safe_float(item.get("latitude")),
            "longitude": _safe_float(item.get("longitude")),
            "priceFrom": _safe_optional_float(item.get("priceFrom")),
            "priceTo": _safe_optional_float(item.get("priceTo")),
            "currency": (item.get("currency") or "")[:8],
            "ticketUrl": (item.get("ticketUrl") or "").strip()[:1000],
            "thumbnailUrl": (item.get("thumbnailUrl") or "").strip()[:1000],
            "photos": photos,
            "ageLimit": item.get("ageLimit"),
            "spokenLanguage": (item.get("spokenLanguage") or "")[:20],
            "facilities": facilities,
            "transportInfo": (item.get("transportInfo") or "")[:2000],
            "whatToBring": (item.get("whatToBring") or "")[:1000],
            "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
            "contentLanguage": src.language or "",
        })
    return out


async def _attach_translations(events: list[dict], source_lang: str) -> list[dict]:
    """Translate title+description for each event into 8 languages."""
    if not events:
        return events

    # Translator hits GPT once per call — keep concurrency small to stay
    # within rate limits. 4 concurrent calls is fine for daily volumes.
    sem = asyncio.Semaphore(4)

    async def one(idx: int, ev: dict) -> None:
        async with sem:
            try:
                tr = await translate_content(
                    title=ev["title"],
                    description=ev.get("description", ""),
                    source_lang=source_lang or "en",
                )
                if tr:
                    ev["translations"] = tr
                    ev["contentLanguage"] = source_lang or ev.get("contentLanguage", "en")
            except Exception as exc:
                logger.debug("[city-pulse] translate event %d failed: %s", idx, exc)

    await asyncio.gather(*[one(i, e) for i, e in enumerate(events)])
    return events


# ════════════════════════════════════════════════════════════════════════════
# Helpers.
# ════════════════════════════════════════════════════════════════════════════

def _safe_float(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_optional_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
