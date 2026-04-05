"""AI-powered geo-location researcher with multi-source verification.

Data-first approach:
  Step 1: Gather REAL data from multiple sources (Nominatim, Overpass, Wikipedia)
  Step 2: Build verified geo-chain with concrete place names
  Step 3: AI writes research ONLY about verified places (never invents)
  Step 4: If no concrete place found — SKIP (better no data than wrong data)

Levels:
  - location (~200m): specific POI found via Overpass/Nominatim
  - district (~2km): neighborhood from Nominatim address
  - city: city from Nominatim address
  - country: country-level overview
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from content.ai_client import get_client

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 8_000
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WIKIPEDIA_API = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
HTTP_HEADERS = {"User-Agent": "ImInApp/1.0 (research bot; igork2011@gmail.com)"}

POI_SEARCH_RADIUS = 200  # meters

OVERPASS_QUERY_TEMPLATE = """
[out:json][timeout:10];
(
  node(around:{radius},{lat},{lng})["tourism"];
  node(around:{radius},{lat},{lng})["historic"];
  node(around:{radius},{lat},{lng})["amenity"~"^(place_of_worship|theatre|cinema|library|museum|arts_centre|fountain|marketplace|community_centre)$"];
  node(around:{radius},{lat},{lng})["leisure"~"^(park|garden|nature_reserve|beach_resort|marina|playground)$"];
  node(around:{radius},{lat},{lng})["natural"~"^(beach|peak|cave_entrance|cliff|spring|waterfall)$"];
  node(around:{radius},{lat},{lng})["man_made"~"^(lighthouse|tower|windmill|watermill|pier|bridge)$"];
  way(around:{radius},{lat},{lng})["tourism"];
  way(around:{radius},{lat},{lng})["historic"];
  way(around:{radius},{lat},{lng})["leisure"~"^(park|garden|nature_reserve|beach_resort|marina)$"];
  way(around:{radius},{lat},{lng})["natural"~"^(beach|peak|cave_entrance|cliff|spring|waterfall)$"];
  way(around:{radius},{lat},{lng})["man_made"~"^(lighthouse|tower|windmill|watermill|pier|bridge)$"];
  relation(around:{radius},{lat},{lng})["tourism"];
  relation(around:{radius},{lat},{lng})["historic"];
  relation(around:{radius},{lat},{lng})["leisure"~"^(park|garden|nature_reserve)$"];
);
out center tags 20;
"""

# ── AI prompts (AI only writes about VERIFIED places) ──

PROMPT_LOCATION = """Ти — місцевий гід-експерт.
Тобі дають ПЕРЕВІРЕНІ ДАНІ про конкретне місце з реальних джерел (карти, Wikipedia).
Напиши дослідження на основі ТІЛЬКИ цих даних. НЕ вигадуй нічого зайвого.

Поверни JSON:
{
  "location_name": "ТОЧНА назва місця (з наданих даних)",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Опис цього місця на основі наданих даних. 1-2 абзаци.",
  "history": [{"period": "рік/період", "description": "що відбувалось тут"}],
  "places": [{"name": "Назва", "type": "museum/monument/restaurant/hotel/park/church/shop", "description": "опис", "url": "посилання або null"}],
  "news": [{"title": "Заголовок", "description": "Опис", "source": "джерело"}]
}

КРИТИЧНО:
1. location_name — ТОЧНА назва з наданих даних! НЕ змінюй!
2. Пиши ТІЛЬКИ про факти з наданих джерел. НЕ ВИГАДУЙ додаткову інформацію.
3. Якщо дані з Wikipedia — використай їх для history та summary.
4. places — тільки ті POI що надані в даних, НЕ вигадуй додаткових.
5. Поверни ТІЛЬКИ JSON."""

PROMPT_DISTRICT = """Ти — краєзнавець-дослідник.
Тобі дають ПЕРЕВІРЕНУ назву району та місто. Напиши дослідження.

Поверни JSON:
{
  "location_name": "Назва району, Місто",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Характеристика цього району. 2-3 абзаци.",
  "history": [{"period": "рік/період", "description": "історія цього району"}],
  "places": [{"name": "Назва", "type": "тип", "description": "опис", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Опис", "source": "джерело"}]
}

КРИТИЧНО:
1. Пиши ТІЛЬКИ про вказаний район вказаного міста!
2. location_name — район + місто з верифікації.
3. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_CITY = """Ти — туристичний експерт.
Тобі дають ПЕРЕВІРЕНУ назву міста. Напиши огляд для туриста.

Поверни JSON:
{
  "location_name": "Назва міста",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Огляд міста: населення, клімат, транспорт, кухня. 3-4 абзаци.",
  "history": [{"period": "рік/період", "description": "ключові моменти історії"}],
  "places": [{"name": "Назва", "type": "тип", "description": "опис", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Новини міста", "source": "джерело"}]
}

КРИТИЧНО:
1. Пиши ТІЛЬКИ про вказане місто!
2. location_name — назва з верифікації, НЕ змінюй!
3. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_COUNTRY = """Ти — експерт з міжнародного туризму.
Тобі дають код країни — зроби повний туристичний огляд.

Поверни JSON:
{
  "location_name": "Назва країни",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Туристичний огляд: клімат, сезон, візи, валюта, мова, безпека, кухня. 4-5 абзаців.",
  "history": [{"period": "рік/період", "description": "ключові моменти історії"}],
  "places": [{"name": "Регіон/місто", "type": "region/city/island/park", "description": "опис", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Актуальне для туристів", "source": "джерело"}]
}

Тільки факти. Поверни ТІЛЬКИ JSON."""

LEVEL_PROMPTS = {
    "location": PROMPT_LOCATION,
    "district": PROMPT_DISTRICT,
    "city": PROMPT_CITY,
    "country": PROMPT_COUNTRY,
}


# ── Step 1a: Nominatim reverse geocoding ──

async def _nominatim_reverse(lat: float, lng: float) -> dict | None:
    """Get address data from OpenStreetMap Nominatim."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=HTTP_HEADERS) as http:
            resp = await http.get(NOMINATIM_URL, params={
                "lat": lat, "lon": lng,
                "format": "json", "addressdetails": 1,
                "zoom": 18, "accept-language": "uk,en",
                "extratags": 1,
            })
            resp.raise_for_status()
            data = resp.json()

        addr = data.get("address", {})
        extratags = data.get("extratags") or {}

        location_name = (
            addr.get("amenity") or addr.get("tourism")
            or addr.get("building") or addr.get("leisure")
            or addr.get("shop") or addr.get("road", "")
        )
        house = addr.get("house_number", "")
        if house and location_name:
            location_name = f"{location_name}, {house}"

        district = (
            addr.get("suburb") or addr.get("city_district")
            or addr.get("neighbourhood") or addr.get("quarter") or ""
        )
        city = (
            addr.get("city") or addr.get("town")
            or addr.get("village") or addr.get("municipality") or ""
        )
        region = (
            addr.get("state") or addr.get("county")
            or addr.get("state_district") or ""
        )
        country = addr.get("country", "")
        country_code = (addr.get("country_code") or "").upper()

        wiki_title = extratags.get("wikipedia", "")

        return {
            "location": location_name or "",
            "district": district or "",
            "city": city or "",
            "region": region or "",
            "country": country or "",
            "country_code": country_code or "",
            "display_name": data.get("display_name", ""),
            "nominatim_wiki": wiki_title,
            "osm_type": data.get("osm_type", ""),
            "osm_id": data.get("osm_id", ""),
            "category": data.get("category", ""),
            "type": data.get("type", ""),
        }

    except Exception:
        logger.exception("[researcher] Nominatim failed for %s, %s", lat, lng)
        return None


# ── Step 1b: Overpass POI search ──

async def _overpass_pois(lat: float, lng: float, radius: int = POI_SEARCH_RADIUS) -> list[dict]:
    """Find Points of Interest near coordinates via Overpass API."""
    query = OVERPASS_QUERY_TEMPLATE.format(radius=radius, lat=lat, lng=lng)
    try:
        async with httpx.AsyncClient(timeout=15, headers=HTTP_HEADERS) as http:
            resp = await http.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            data = resp.json()

        pois = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:uk") or tags.get("name:en")
            if not name:
                continue

            poi = {
                "name": name,
                "name_uk": tags.get("name:uk", ""),
                "name_en": tags.get("name:en", ""),
                "type": (
                    tags.get("tourism") or tags.get("historic")
                    or tags.get("amenity") or tags.get("leisure")
                    or tags.get("natural") or tags.get("man_made") or ""
                ),
                "wikipedia": tags.get("wikipedia", ""),
                "wikidata": tags.get("wikidata", ""),
                "website": tags.get("website", ""),
                "description": tags.get("description", ""),
            }
            pois.append(poi)

        pois.sort(key=lambda p: _poi_score(p), reverse=True)
        logger.info("[researcher] Overpass found %d POIs near %s, %s", len(pois), lat, lng)
        return pois[:15]

    except Exception:
        logger.warning("[researcher] Overpass failed for %s, %s — continuing without POIs", lat, lng)
        return []


TOURIST_POI_TYPES = {
    "museum", "gallery", "artwork", "attraction", "viewpoint", "theme_park",
    "monument", "memorial", "castle", "ruins", "archaeological_site", "fort",
    "palace", "city_gate", "church", "cathedral", "monastery", "mosque", "temple",
    "beach", "peak", "cave_entrance", "cliff", "spring", "waterfall",
    "lighthouse", "tower", "windmill", "pier", "bridge",
    "park", "garden", "nature_reserve", "beach_resort", "marina",
    "theatre", "arts_centre", "fountain",
    "hotel", "hostel", "camp_site", "guest_house", "chalet",
}


def _poi_score(poi: dict) -> int:
    """Score a POI by tourist relevance. Higher = more interesting."""
    score = 0
    if poi.get("wikipedia"):
        score += 10
    if poi.get("wikidata"):
        score += 3
    if poi.get("website"):
        score += 2
    if poi.get("type", "") in TOURIST_POI_TYPES:
        score += 5
    if poi.get("description"):
        score += 1
    return score


def _has_worthy_poi(pois: list[dict]) -> bool:
    """Check if there's at least one POI worth researching (tourist/historic/natural)."""
    for p in pois:
        if p.get("type", "") in TOURIST_POI_TYPES:
            return True
        if p.get("wikipedia"):
            return True
    return False


# ── Step 1c: Wikipedia summary ──

async def _wikipedia_summary(wiki_ref: str) -> dict | None:
    """Fetch Wikipedia summary. wiki_ref format: 'en:Article_Name' or 'uk:Назва'."""
    if not wiki_ref or ":" not in wiki_ref:
        return None

    lang, title = wiki_ref.split(":", 1)
    url = WIKIPEDIA_API.format(lang=lang, title=title.replace(" ", "_"))

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
        logger.warning("[researcher] Wikipedia fetch failed for %s", wiki_ref)
        return None


# ── Step 2: Build verified geo-chain ──

async def _build_geo_chain(lat: float, lng: float, expected_country: str) -> dict | None:
    """Gather data from all sources and build a verified geo-chain.

    Returns None if we cannot confidently identify the location.
    """
    nominatim = await _nominatim_reverse(lat, lng)
    if nominatim is None:
        return None

    city = nominatim.get("city", "")
    country_code = nominatim.get("country_code", "")

    if not city and not country_code:
        logger.warning("[researcher] Nominatim returned no city/country for %s, %s", lat, lng)
        return None

    if expected_country and country_code and country_code != expected_country.upper():
        logger.warning("[researcher] Country mismatch: nominatim=%s expected=%s", country_code, expected_country)
        return None

    pois = await _overpass_pois(lat, lng)

    best_poi = pois[0] if pois else None

    wiki_data = None
    wiki_ref = ""
    if best_poi and best_poi.get("wikipedia"):
        wiki_ref = best_poi["wikipedia"]
    elif nominatim.get("nominatim_wiki"):
        wiki_ref = nominatim["nominatim_wiki"]
    if wiki_ref:
        wiki_data = await _wikipedia_summary(wiki_ref)

    location_name = ""
    if best_poi:
        location_name = best_poi.get("name_uk") or best_poi.get("name") or ""
    if not location_name:
        location_name = nominatim.get("location", "")

    chain = {
        "location": location_name,
        "location_type": best_poi.get("type", "") if best_poi else nominatim.get("type", ""),
        "district": nominatim.get("district", ""),
        "city": city,
        "region": nominatim.get("region", ""),
        "country": nominatim.get("country", ""),
        "country_code": country_code,
        "display_name": nominatim.get("display_name", ""),
        "pois": pois,
        "best_poi": best_poi,
        "wikipedia": wiki_data,
        "has_concrete_location": _has_worthy_poi(pois),
        "confidence": "high" if _has_worthy_poi(pois) else ("medium" if city else "low"),
    }

    logger.info(
        "[researcher] Geo-chain: location='%s' (%s) → district='%s' → city='%s' → %s (%s) "
        "| POIs=%d | wiki=%s | confidence=%s",
        chain["location"], chain["location_type"],
        chain["district"], chain["city"],
        chain["country"], chain["country_code"],
        len(pois), "yes" if wiki_data else "no",
        chain["confidence"],
    )
    return chain


# ── Step 3: Content generation ──

def _build_ai_context(geo_chain: dict, level: str, lat: float, lng: float, language: str) -> str:
    """Build the AI prompt with all verified data."""
    parts = [
        f"Координати: {lat}, {lng}",
        f"Мова відповіді: {language}",
        "",
        "ВЕРИФІКОВАНІ ДАНІ З РЕАЛЬНИХ ДЖЕРЕЛ:",
        f"  Локація: {geo_chain.get('location', 'невідомо')} (тип: {geo_chain.get('location_type', '?')})",
        f"  Район: {geo_chain.get('district', 'невідомо')}",
        f"  Місто: {geo_chain.get('city', 'невідомо')}",
        f"  Область: {geo_chain.get('region', 'невідомо')}",
        f"  Країна: {geo_chain.get('country', 'невідомо')} ({geo_chain.get('country_code', '?')})",
    ]

    if level == "location":
        pois = geo_chain.get("pois", [])
        if pois:
            parts.append("")
            parts.append(f"ЗНАЙДЕНІ POI В РАДІУСІ {POI_SEARCH_RADIUS}М (з OpenStreetMap):")
            for p in pois[:10]:
                line = f"  • {p['name']} (тип: {p['type']})"
                if p.get("description"):
                    line += f" — {p['description']}"
                if p.get("website"):
                    line += f" | {p['website']}"
                parts.append(line)

        wiki = geo_chain.get("wikipedia")
        if wiki:
            parts.append("")
            parts.append("ДАНІ З WIKIPEDIA:")
            parts.append(f"  Назва: {wiki.get('title', '')}")
            parts.append(f"  Опис: {wiki.get('description', '')}")
            extract = wiki.get("extract", "")
            if extract:
                parts.append(f"  Текст: {extract[:1500]}")
            if wiki.get("url"):
                parts.append(f"  URL: {wiki['url']}")

        parts.append("")
        parts.append(f"ЗАВДАННЯ: Напиши дослідження про '{geo_chain.get('location', '')}' в місті {geo_chain.get('city', '')}.")
        parts.append("ВИКОРИСТОВУЙ ТІЛЬКИ НАДАНІ ДАНІ. НЕ ВИГАДУЙ.")

    elif level == "district":
        parts.append("")
        parts.append(f"ЗАВДАННЯ: Напиши про район '{geo_chain.get('district', '')}' міста {geo_chain.get('city', '')}.")

    elif level == "city":
        parts.append("")
        parts.append(f"ЗАВДАННЯ: Напиши огляд міста '{geo_chain.get('city', '')}'.")
        parts.append(f"УВАГА: Пиши ТІЛЬКИ про {geo_chain.get('city', '')}!")

    parts.append("\nПоверни JSON.")
    return "\n".join(parts)


# ── Main entry point ──

async def research_location(
    latitude: float,
    longitude: float,
    name: Optional[str] = None,
    language: str = "uk",
    expected_country: str = "",
    level: str = "location",
) -> dict | None:
    """Research a geographic location using multi-source verified data.

    For location level: requires a concrete named POI from Overpass/Nominatim.
    If no concrete place found — returns None (skip, don't invent).
    """
    client = get_client()

    if level == "country":
        return await _research_country(client, expected_country, language)

    geo_chain = await _build_geo_chain(latitude, longitude, expected_country)
    if geo_chain is None:
        logger.warning("[researcher] Cannot build geo-chain, skipping %s, %s level=%s", latitude, longitude, level)
        return None

    if level == "location" and not geo_chain.get("has_concrete_location"):
        logger.info(
            "[researcher] No concrete POI found for %s, %s — skipping location level "
            "(only road/address: '%s')", latitude, longitude, geo_chain.get("location", "?"),
        )
        return None

    if level == "district" and not geo_chain.get("district"):
        logger.info("[researcher] No district data for %s, %s — skipping", latitude, longitude)
        return None

    if level in ("city", "district") and not geo_chain.get("city"):
        logger.info("[researcher] No city data for %s, %s — skipping level=%s", latitude, longitude, level)
        return None

    system_prompt = LEVEL_PROMPTS.get(level, PROMPT_LOCATION)
    ai_context = _build_ai_context(geo_chain, level, latitude, longitude, language)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": ai_context},
            ],
            max_tokens=3000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)

        if not result.get("summary"):
            return None

        result["summary"] = result["summary"][:MAX_SUMMARY_CHARS]
        for key in ("history", "places", "news"):
            if not isinstance(result.get(key), list):
                result[key] = []

        if level == "city":
            chain_city = (geo_chain.get("city") or "").lower()
            result_name = (result.get("location_name") or "").lower()
            if chain_city and result_name and chain_city not in result_name and result_name not in chain_city:
                result["_rejected"] = True
                result["_reject_reason"] = f"AI wrote about '{result.get('location_name')}' instead of '{geo_chain.get('city')}'"
                logger.warning("[researcher] REJECTED: %s", result["_reject_reason"])
                return result

        result["_geo_chain"] = geo_chain
        return result

    except json.JSONDecodeError:
        logger.exception("Failed to parse AI JSON for %s, %s level=%s", latitude, longitude, level)
        return None
    except Exception:
        logger.exception("AI research failed for %s, %s level=%s", latitude, longitude, level)
        raise


async def _research_country(client, country_code: str, language: str) -> dict | None:
    """Generate country-level research."""
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": PROMPT_COUNTRY},
                {"role": "user", "content": f"Країна: {country_code}\nМова: {language}\n\nПоверни JSON."},
            ],
            max_tokens=3000,
            temperature=0.4,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)

        if not result.get("summary"):
            return None

        result["summary"] = result["summary"][:MAX_SUMMARY_CHARS]
        for key in ("history", "places", "news"):
            if not isinstance(result.get(key), list):
                result[key] = []

        return result

    except Exception:
        logger.exception("Country research failed for %s", country_code)
        return None
