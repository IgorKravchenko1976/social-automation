"""AI-powered airport researcher with Wikipedia verification.

The airport is already a known, concrete POI — no need to discover it.
Flow:
  1. Search Wikipedia for the airport article
  2. Nominatim reverse geocoding for address confirmation
  3. AI generates traveler-focused description using verified data
  4. Returns title + description for event creation
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from content.ai_client import get_client

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
WIKIPEDIA_SEARCH = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH_API = "https://en.wikipedia.org/w/api.php"
HTTP_HEADERS = {"User-Agent": "ImInApp/1.0 (airport research bot; igork2011@gmail.com)"}


AIRPORT_PROMPT = """You are a travel expert writing about airports for travelers.
You receive VERIFIED data about an airport from real sources (Wikipedia, maps).
Write a traveler-focused description using ONLY the provided data.

Return JSON:
{
  "title": "Official airport name (from provided data)",
  "description": "Traveler-focused description: location, transport to city center, facilities, interesting facts. 2-3 paragraphs.",
  "city_info": "Brief info about the city this airport serves.",
  "transport": "How to get from airport to city center (if known from data).",
  "facts": ["Interesting fact 1", "Interesting fact 2"]
}

CRITICAL:
1. Use ONLY the data provided. DO NOT invent additional information.
2. Write in English.
3. Focus on practical info for travelers.
4. Return ONLY JSON."""


async def _wikipedia_airport(name: str, iata: str) -> dict | None:
    """Search Wikipedia for an airport article."""
    queries = [
        f"{name}",
        f"{iata} airport",
        f"{name} airport",
    ]

    for query in queries:
        try:
            async with httpx.AsyncClient(timeout=10, headers=HTTP_HEADERS) as http:
                resp = await http.get(WIKIPEDIA_SEARCH_API, params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "format": "json",
                    "srlimit": 3,
                })
                if resp.status_code != 200:
                    continue
                data = resp.json()

            results = data.get("query", {}).get("search", [])
            for result in results:
                title = result.get("title", "")
                lower_title = title.lower()
                if "airport" in lower_title or iata.lower() in lower_title:
                    summary = await _fetch_wiki_summary(title)
                    if summary:
                        return summary
                    break

        except Exception:
            logger.warning("[airport-researcher] Wikipedia search failed for: %s", query)
            continue

    return None


async def _fetch_wiki_summary(title: str) -> dict | None:
    """Fetch Wikipedia page summary."""
    url = WIKIPEDIA_SEARCH.format(title=title.replace(" ", "_"))
    try:
        async with httpx.AsyncClient(timeout=10, headers=HTTP_HEADERS) as http:
            resp = await http.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()

        return {
            "title": data.get("title", ""),
            "extract": data.get("extract", ""),
            "description": data.get("description", ""),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
        }
    except Exception:
        return None


async def _nominatim_airport(lat: float, lng: float) -> dict | None:
    """Reverse geocode airport location for address context."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=HTTP_HEADERS) as http:
            resp = await http.get(NOMINATIM_URL, params={
                "lat": lat, "lon": lng,
                "format": "json", "addressdetails": 1,
                "zoom": 14, "accept-language": "en",
            })
            resp.raise_for_status()
            data = resp.json()

        addr = data.get("address", {})
        return {
            "display_name": data.get("display_name", ""),
            "city": addr.get("city") or addr.get("town") or addr.get("village") or "",
            "state": addr.get("state") or "",
            "country": addr.get("country") or "",
            "country_code": (addr.get("country_code") or "").upper(),
        }
    except Exception:
        logger.warning("[airport-researcher] Nominatim failed for %.4f, %.4f", lat, lng)
        return None


async def research_airport(
    name: str,
    iata: str,
    city: str,
    country_code: str,
    lat: float,
    lng: float,
) -> Optional[dict]:
    """Research an airport using Wikipedia + Nominatim + AI.

    Returns dict with title, description, image_query or None on failure.
    """
    client = get_client()

    wiki = await _wikipedia_airport(name, iata)
    nominatim = await _nominatim_airport(lat, lng)

    context_parts = [
        f"Airport: {name}",
        f"IATA code: {iata}",
        f"City: {city}",
        f"Country: {country_code}",
        f"Coordinates: {lat}, {lng}",
    ]

    if nominatim:
        if nominatim.get("city"):
            context_parts.append(f"Nominatim city: {nominatim['city']}")
        if nominatim.get("state"):
            context_parts.append(f"State/Region: {nominatim['state']}")
        if nominatim.get("country"):
            context_parts.append(f"Country (verified): {nominatim['country']}")

    if wiki:
        context_parts.append("")
        context_parts.append("WIKIPEDIA DATA:")
        context_parts.append(f"  Title: {wiki.get('title', '')}")
        if wiki.get("description"):
            context_parts.append(f"  Description: {wiki['description']}")
        extract = wiki.get("extract", "")
        if extract:
            context_parts.append(f"  Article: {extract[:2000]}")
        if wiki.get("url"):
            context_parts.append(f"  URL: {wiki['url']}")
    else:
        context_parts.append("")
        context_parts.append("No Wikipedia article found. Use only the basic airport data above.")

    context_parts.append("")
    context_parts.append(f"Write a traveler description for {name} ({iata}).")
    context_parts.append("USE ONLY THE PROVIDED DATA. DO NOT INVENT.")
    context_parts.append("Return JSON.")

    ai_context = "\n".join(context_parts)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": AIRPORT_PROMPT},
                {"role": "user", "content": ai_context},
            ],
            max_tokens=2000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)

        if not result.get("title") or not result.get("description"):
            logger.warning("[airport-researcher] AI returned empty result for %s", iata)
            return None

        result["iata_code"] = iata
        result["airport_name"] = name
        result["city"] = city
        result["country_code"] = country_code
        result["wikipedia"] = wiki
        result["image_query"] = f"{name} airport terminal building"

        logger.info("[airport-researcher] Research completed for %s (%s)", name, iata)
        return result

    except json.JSONDecodeError:
        logger.exception("[airport-researcher] AI JSON parse failed for %s", iata)
        return None
    except Exception:
        logger.exception("[airport-researcher] AI research failed for %s", iata)
        return None
