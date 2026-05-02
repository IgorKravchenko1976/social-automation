"""Wikidata-based region structure crawler + AI enrichment.

Two-phase operation:
  Phase A — Structure Seeding: Crawl Wikidata for admin subdivisions of a country.
  Phase B — Region Research: Enrich individual regions with Wikipedia + AI descriptions.

Uses SPARQL queries to Wikidata and Wikipedia REST API for descriptions.
GPT-4o-mini generates traveler-focused descriptions in Ukrainian.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from content.ai_client import get_client

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIPEDIA_SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH_API = "https://{lang}.wikipedia.org/w/api.php"
HTTP_HEADERS = {"User-Agent": "ImInApp/1.0 (region research bot; igork2011@gmail.com)"}
HTTP_TIMEOUT = 30

# Wikidata property IDs
P_INSTANCE_OF = "P31"
P_COUNTRY = "P17"
P_COORD = "P625"
P_POPULATION = "P1082"
P_AREA = "P2046"
P_ADMIN_PARENT = "P131"
P_TIMEZONE = "P421"
P_IMAGE = "P18"

# Country Wikidata IDs for priority countries
COUNTRY_QID = {
    "UA": "Q212", "PL": "Q36", "DE": "Q183", "FR": "Q142",
    "ES": "Q29", "IT": "Q38", "GR": "Q41", "CY": "Q229",
    "TR": "Q43", "GB": "Q145", "US": "Q30", "PT": "Q45",
    "CZ": "Q213", "AT": "Q40", "CH": "Q39", "NL": "Q55",
    "HR": "Q224", "BG": "Q219", "HU": "Q28", "RO": "Q218",
    "JP": "Q17", "TH": "Q869", "EG": "Q79", "AE": "Q878",
    "IN": "Q668", "BR": "Q155", "MX": "Q96", "AU": "Q408",
    "CA": "Q16", "IL": "Q801",
}

# Admin subdivision types by country
# Maps country QID → list of Wikidata types for first-level admin divisions
ADMIN_TYPES = {
    "Q212": ["Q3348196"],  # UA: oblast of Ukraine
    "Q36": ["Q150093"],     # PL: voivodeship
    "Q183": ["Q1221156"],   # DE: Bundesland
    "Q142": ["Q36784"],     # FR: region of France
    "Q29": ["Q10742"],      # ES: autonomous community
    "Q38": ["Q16110"],      # IT: region of Italy
    "Q145": ["Q180673"],    # GB: country of the UK + Q48091 (county)
    "Q30": ["Q35657"],      # US: state
}


async def _sparql_query(query: str) -> list[dict]:
    """Execute a SPARQL query against Wikidata."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
            headers=HTTP_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("results", {}).get("bindings", [])


def _extract_qid(uri: str) -> str:
    """Extract Wikidata QID from URI like http://www.wikidata.org/entity/Q212."""
    if not uri:
        return ""
    match = re.search(r"(Q\d+)$", uri)
    return match.group(1) if match else ""


def _extract_coord(point_str: str) -> tuple[float, float]:
    """Parse 'Point(lon lat)' from Wikidata into (lat, lon)."""
    if not point_str:
        return 0.0, 0.0
    match = re.match(r"Point\(([-\d.]+)\s+([-\d.]+)\)", point_str)
    if match:
        lon, lat = float(match.group(1)), float(match.group(2))
        return lat, lon
    return 0.0, 0.0


def _safe_float(val: str) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: str) -> int:
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


async def fetch_country_regions(country_code: str) -> list[dict]:
    """Phase A: Fetch first-level admin divisions for a country from Wikidata.

    Returns list of dicts with wikidataId, name, lat, lon, population, areaKm2, etc.
    """
    country_qid = COUNTRY_QID.get(country_code)
    if not country_qid:
        country_qid = await _lookup_country_qid(country_code)
        if not country_qid:
            logger.warning("[region-researcher] No Wikidata QID for %s", country_code)
            return []

    admin_types = ADMIN_TYPES.get(country_qid)

    if admin_types:
        type_filter = " ".join(f"wd:{t}" for t in admin_types)
        query = f"""
        SELECT DISTINCT ?region ?regionLabel ?coord ?population ?area ?image WHERE {{
          VALUES ?type {{ {type_filter} }}
          ?region wdt:{P_INSTANCE_OF}/wdt:P279* ?type.
          ?region wdt:{P_COUNTRY} wd:{country_qid}.
          OPTIONAL {{ ?region wdt:{P_COORD} ?coord }}
          OPTIONAL {{ ?region wdt:{P_POPULATION} ?population }}
          OPTIONAL {{ ?region wdt:{P_AREA} ?area }}
          OPTIONAL {{ ?region wdt:{P_IMAGE} ?image }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "uk,en" }}
        }}
        """
    else:
        # Generic: first-level admin divisions
        query = f"""
        SELECT DISTINCT ?region ?regionLabel ?coord ?population ?area ?image WHERE {{
          ?region wdt:{P_ADMIN_PARENT} wd:{country_qid}.
          ?region wdt:{P_INSTANCE_OF}/wdt:P279* wd:Q10864048.
          OPTIONAL {{ ?region wdt:{P_COORD} ?coord }}
          OPTIONAL {{ ?region wdt:{P_POPULATION} ?population }}
          OPTIONAL {{ ?region wdt:{P_AREA} ?area }}
          OPTIONAL {{ ?region wdt:{P_IMAGE} ?image }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "uk,en" }}
        }}
        """

    try:
        results = await _sparql_query(query)
    except Exception as exc:
        logger.error("[region-researcher] SPARQL failed for %s: %s", country_code, exc)
        return []

    regions = []
    seen_qids = set()
    for row in results:
        qid = _extract_qid(row.get("region", {}).get("value", ""))
        if not qid or qid in seen_qids:
            continue
        seen_qids.add(qid)

        name = row.get("regionLabel", {}).get("value", "")
        if not name or name == qid:
            continue

        lat, lon = _extract_coord(row.get("coord", {}).get("value", ""))
        population = _safe_int(row.get("population", {}).get("value", ""))
        area = _safe_float(row.get("area", {}).get("value", ""))
        image = row.get("image", {}).get("value", "")

        regions.append({
            "wikidataId": qid,
            "level": "region",
            "name": name,
            "latitude": lat,
            "longitude": lon,
            "population": population,
            "areaKm2": area,
            "imageUrl": image,
        })

    logger.info("[region-researcher] %s: found %d regions", country_code, len(regions))
    return regions


async def fetch_region_cities(
    region_wikidata_id: str,
    country_code: str,
    min_population: int = 5000,
) -> list[dict]:
    """Fetch cities/towns within a region from Wikidata.

    Returns cities with population >= min_population.
    """
    query = f"""
    SELECT DISTINCT ?city ?cityLabel ?coord ?population ?image WHERE {{
      ?city wdt:{P_INSTANCE_OF}/wdt:P279* wd:Q515.
      ?city wdt:{P_ADMIN_PARENT}+ wd:{region_wikidata_id}.
      OPTIONAL {{ ?city wdt:{P_COORD} ?coord }}
      OPTIONAL {{ ?city wdt:{P_POPULATION} ?population }}
      OPTIONAL {{ ?city wdt:{P_IMAGE} ?image }}
      FILTER(BOUND(?population) && ?population >= {min_population})
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "uk,en" }}
    }}
    ORDER BY DESC(?population)
    LIMIT 100
    """

    try:
        results = await _sparql_query(query)
    except Exception as exc:
        logger.error("[region-researcher] City SPARQL failed for %s: %s", region_wikidata_id, exc)
        return []

    cities = []
    seen_qids = set()
    for row in results:
        qid = _extract_qid(row.get("city", {}).get("value", ""))
        if not qid or qid in seen_qids:
            continue
        seen_qids.add(qid)

        name = row.get("cityLabel", {}).get("value", "")
        if not name or name == qid:
            continue

        lat, lon = _extract_coord(row.get("coord", {}).get("value", ""))
        population = _safe_int(row.get("population", {}).get("value", ""))
        image = row.get("image", {}).get("value", "")

        cities.append({
            "wikidataId": qid,
            "parentWikidataId": region_wikidata_id,
            "level": "city",
            "name": name,
            "latitude": lat,
            "longitude": lon,
            "population": population,
            "imageUrl": image,
        })

    logger.info(
        "[region-researcher] %s: found %d cities (pop>=%d)",
        region_wikidata_id, len(cities), min_population,
    )
    return cities


async def _lookup_country_qid(country_code: str) -> Optional[str]:
    """Look up Wikidata QID for a country by ISO 3166-1 alpha-2 code."""
    query = f"""
    SELECT ?country WHERE {{
      ?country wdt:P297 "{country_code}".
    }}
    LIMIT 1
    """
    try:
        results = await _sparql_query(query)
        if results:
            return _extract_qid(results[0].get("country", {}).get("value", ""))
    except Exception:
        pass
    return None


# ── Phase B: Region Research (AI enrichment) ──

REGION_RESEARCH_PROMPT = """Ти — експерт з географії, історії та подорожей.
Ти отримуєш дані про адміністративну одиницю (континент / країну /
регіон / область / місто / район).
Напиши ЯКІСНИЙ, ДЕТАЛЬНИЙ опис для мандрівника українською мовою.

Поверни JSON:
{
  "summary": "2-3 речення (~150-250 символів) — ключова характеристика для мандрівника, чому варто туди їхати",
  "description": "Розгорнутий опис ~500-700 символів. Структуруй на 2-3 теми:\\n• Історія: ключові періоди, події, важливі дати (НЕ просто 'багата історія', а конкретні факти: коли заснований, хто завоював, що визначального).\\n• Культура / архітектура / кухня — те, що відрізняє це місце.\\n• Природа / клімат / географія — для розуміння де це і коли їхати.",
  "highlights": ["Головна пам'ятка / місце 1", "Цікаве 2", "Цікаве 3", "Цікаве 4 (опц.)", "Цікаве 5 (опц.)"]
}

КРИТИЧНО — три пріоритети у такому порядку:
1. ТОЧНІСТЬ. Використовуй лише надані дані (Wikipedia + populace + area
   + контекст) + те, що ти точно знаєш. ЗАБОРОНЕНО вигадувати конкретні
   дати, цифри, прізвища. Краще пропустити деталь, ніж вигадати неправду.
2. СВІЖІСТЬ. Якщо у контексті згадуються факти ХХ ст., згадай їх,
   але обов'язково додай хоча б один сучасний факт (статус, статистика
   або сучасне значення для мандрівника). Уникай "за радянських часів"
   без сучасного контексту.
3. РЕЛЕВАНТНІСТЬ ДЛЯ МАНДРІВНИКА. Не енциклопедична довідка. Що людина
   може ПОБАЧИТИ / СПРОБУВАТИ / ВІДЧУТИ.

Інші правила:
4. Reject ВИКЛЮЧНО за полем 'Назва' / 'Країна' у контексті:
   • якщо 'Країна: RU' або 'Країна: BY' → поверни {"_rejected": true};
   • якщо 'Назва' є точно "Россия", "Russia", "Беларусь", "Belarus"
     (тобто сама ця одиниця і є Росією / Білоруссю) → reject;
   • згадка Росії, Білорусі чи окупованих територій у Wiki-summary
     (як сусіда / частини більшого регіону / історичного факту) —
     НЕ привід для reject. Просто не пиши про них у власному описі.
5. Якщо рівень "continent" — описуй континент у цілому (географія,
   культурні регіони, що очікувати від подорожі). Континент НЕ можна
   reject'ити — Європа, Азія тощо завжди валідні.
6. Description не може бути коротшим за 350 символів. Якщо даних мало —
   зосередься на географії, кліматі, типовій кухні / культурі регіону
   (на рівні країни), і чесно скажи що це 'тиха провінція' замість
   придумування пам'яток.
7. Повертай ТІЛЬКИ JSON, без коментарів."""


async def research_region(
    name: str,
    level: str,
    country_code: str,
    *,
    parent_name: str = "",
    latitude: float = 0,
    longitude: float = 0,
    population: int = 0,
    area_km2: float = 0,
    wikidata_id: str = "",
    wikipedia_url: str = "",
) -> Optional[dict]:
    """Research a region using Wikipedia + GPT-4o-mini.

    Returns dict with summary, description, highlights or None on failure.
    """
    # Fetch Wikipedia summary
    wiki_summary = ""
    wiki_image = ""
    wiki_url = wikipedia_url

    if wikidata_id:
        wiki_data = await _fetch_wikipedia_from_wikidata(wikidata_id)
        if wiki_data:
            wiki_summary = wiki_data.get("summary", "")
            wiki_image = wiki_data.get("image", "")
            if not wiki_url:
                wiki_url = wiki_data.get("url", "")

    if not wiki_summary:
        wiki_data = await _search_wikipedia(name, lang="uk")
        if wiki_data:
            wiki_summary = wiki_data.get("summary", "")
            wiki_image = wiki_image or wiki_data.get("image", "")
            wiki_url = wiki_url or wiki_data.get("url", "")

    if not wiki_summary:
        wiki_data = await _search_wikipedia(name, lang="en")
        if wiki_data:
            wiki_summary = wiki_data.get("summary", "")
            wiki_image = wiki_image or wiki_data.get("image", "")

    # Build context for AI
    context_parts = [
        f"Назва: {name}",
        f"Рівень: {level}",
        f"Країна: {country_code}",
    ]
    if parent_name:
        context_parts.append(f"Батьківський регіон: {parent_name}")
    if population > 0:
        context_parts.append(f"Населення: {population:,}")
    if area_km2 > 0:
        context_parts.append(f"Площа: {area_km2:,.1f} км²")
    if wiki_summary:
        context_parts.append(f"Вікіпедія: {wiki_summary[:4000]}")

    context = "\n".join(context_parts)

    try:
        client = get_client()
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": REGION_RESEARCH_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.5,
            max_tokens=2000,
        )

        content = response.choices[0].message.content
        result = json.loads(content)

        if result.get("_rejected"):
            logger.info("[region-researcher] Rejected: %s (%s)", name, country_code)
            return None

        result["wikipediaUrl"] = wiki_url
        result["imageUrl"] = wiki_image

        return result

    except Exception as exc:
        logger.error("[region-researcher] AI research failed for %s: %s", name, exc)
        return None


async def _fetch_wikipedia_from_wikidata(qid: str) -> Optional[dict]:
    """Fetch Wikipedia summary for a Wikidata entity."""
    # Get sitelinks from Wikidata
    query = f"""
    SELECT ?article WHERE {{
      OPTIONAL {{ ?article schema:about wd:{qid}; schema:isPartOf <https://uk.wikipedia.org/> }}
    }}
    LIMIT 1
    """
    try:
        results = await _sparql_query(query)
        if not results:
            return None

        article_url = results[0].get("article", {}).get("value", "")
        if not article_url:
            return None

        title = article_url.split("/wiki/")[-1] if "/wiki/" in article_url else ""
        if not title:
            return None

        return await _fetch_wiki_summary(title, "uk")
    except Exception:
        return None


async def _search_wikipedia(name: str, lang: str = "uk") -> Optional[dict]:
    """Search Wikipedia by name and return the summary."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                WIKIPEDIA_SEARCH_API.format(lang=lang),
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": name,
                    "srlimit": 1,
                    "format": "json",
                },
                headers=HTTP_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("query", {}).get("search", [])
        if not results:
            return None

        title = results[0]["title"]
        return await _fetch_wiki_summary(title, lang)
    except Exception:
        return None


async def _fetch_wiki_summary(title: str, lang: str) -> Optional[dict]:
    """Fetch summary from Wikipedia REST API."""
    try:
        url = WIKIPEDIA_SUMMARY.format(lang=lang, title=title)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=HTTP_HEADERS)
            if resp.status_code != 200:
                return None
            data = resp.json()

        return {
            "summary": data.get("extract", ""),
            "image": data.get("originalimage", {}).get("source", "")
                     or data.get("thumbnail", {}).get("source", ""),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
        }
    except Exception:
        return None
