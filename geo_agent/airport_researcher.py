"""AI-powered transport hub researcher with Wikipedia verification.

Supports airports and railway stations. Each facility type gets its own
AI prompt and image query. Content is generated in Ukrainian.

Flow:
  1. Search Wikipedia for the facility article
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
WIKI_UK_SEARCH_API = "https://uk.wikipedia.org/w/api.php"
WIKI_UK_SUMMARY = "https://uk.wikipedia.org/api/rest_v1/page/summary/{title}"
HTTP_HEADERS = {"User-Agent": "ImInApp/1.0 (transport research bot; igork2011@gmail.com)"}


AIRPORT_PROMPT = """Ти — експерт з подорожей, який пише про аеропорти для мандрівників.
Ти отримуєш ПЕРЕВІРЕНІ дані про аеропорт з реальних джерел (Вікіпедія, карти).
Напиши опис для мандрівників, використовуючи ТІЛЬКИ надані дані.

Поверни JSON:
{
  "title": "Офіційна назва аеропорту (з наданих даних)",
  "description": "Опис для мандрівників: розташування, транспорт до центру міста, зручності, цікаві факти. 2-3 абзаци.",
  "city_info": "Коротка інформація про місто, яке обслуговує аеропорт.",
  "transport": "Як дістатися з аеропорту до центру міста (якщо відомо з даних).",
  "facts": ["Цікавий факт 1", "Цікавий факт 2"]
}

КРИТИЧНО:
1. Використовуй ТІЛЬКИ надані дані. НЕ вигадуй додаткову інформацію.
2. Пиши УКРАЇНСЬКОЮ мовою.
3. Фокусуйся на практичній інформації для мандрівників.
4. Повертай ТІЛЬКИ JSON."""


RAILWAY_PROMPT = """Ти — експерт з подорожей, який пише про залізничні вокзали для мандрівників.
Ти отримуєш ПЕРЕВІРЕНІ дані про вокзал з реальних джерел (Вікіпедія, карти).
Напиши опис для мандрівників, використовуючи ТІЛЬКИ надані дані.

Поверни JSON:
{
  "title": "Офіційна назва вокзалу (з наданих даних)",
  "description": "Опис для мандрівників: розташування, маршрути, зручності, архітектура, цікаві факти. 2-3 абзаци.",
  "city_info": "Коротка інформація про місто, де знаходиться вокзал.",
  "transport": "Основні напрямки поїздів та з'єднання з іншим транспортом.",
  "facts": ["Цікавий факт 1", "Цікавий факт 2"]
}

КРИТИЧНО:
1. Використовуй ТІЛЬКИ надані дані. НЕ вигадуй додаткову інформацію.
2. Пиши УКРАЇНСЬКОЮ мовою.
3. Фокусуйся на практичній інформації для мандрівників.
4. Повертай ТІЛЬКИ JSON."""


HELIPORT_PROMPT = """Ти — експерт з подорожей, який пише про вертолітні майданчики для мандрівників.
Ти отримуєш ПЕРЕВІРЕНІ дані з реальних джерел (Вікіпедія, карти).
Напиши опис для мандрівників, використовуючи ТІЛЬКИ надані дані.

Поверни JSON:
{
  "title": "Назва вертолітного майданчика (з наданих даних)",
  "description": "Опис: розташування, призначення, доступність, цікаві факти. 2-3 абзаци.",
  "city_info": "Коротка інформація про місто/район, де знаходиться.",
  "transport": "Як дістатися (якщо відомо з даних).",
  "facts": ["Цікавий факт 1", "Цікавий факт 2"]
}

КРИТИЧНО:
1. Використовуй ТІЛЬКИ надані дані. НЕ вигадуй.
2. Пиши УКРАЇНСЬКОЮ мовою.
3. Фокусуйся на практичній інформації для мандрівників.
4. Повертай ТІЛЬКИ JSON."""


MILITARY_PROMPT = """Ти — експерт з подорожей, який пише про військові аеробази як цікаві місця.
Ти отримуєш ПЕРЕВІРЕНІ дані з реальних джерел (Вікіпедія, карти).
Напиши опис для мандрівників — фокус на історичному та авіаційному значенні.

Поверни JSON:
{
  "title": "Назва авіабази (з наданих даних)",
  "description": "Опис: історія, значення, авіаційні факти, доступність для туристів. 2-3 абзаци.",
  "city_info": "Коротка інформація про місто/район.",
  "transport": "Загальна інформація про розташування.",
  "facts": ["Цікавий факт 1", "Цікавий факт 2"]
}

КРИТИЧНО:
1. Використовуй ТІЛЬКИ надані дані. НЕ вигадуй.
2. Пиши УКРАЇНСЬКОЮ мовою.
3. Фокусуйся на історичних/авіаційних фактах, уникай військових деталей.
4. Повертай ТІЛЬКИ JSON."""


BUS_PROMPT = """Ти — експерт з подорожей, який пише про автобусні станції для мандрівників.
Ти отримуєш ПЕРЕВІРЕНІ дані з реальних джерел (Вікіпедія, карти).
Напиши опис для мандрівників, використовуючи ТІЛЬКИ надані дані.

Поверни JSON:
{
  "title": "Назва автобусної станції (з наданих даних)",
  "description": "Опис: розташування, маршрути, зручності, практичні поради. 2-3 абзаци.",
  "city_info": "Коротка інформація про місто.",
  "transport": "Основні напрямки та з'єднання з іншим транспортом.",
  "facts": ["Цікавий факт 1", "Цікавий факт 2"]
}

КРИТИЧНО:
1. Використовуй ТІЛЬКИ надані дані. НЕ вигадуй.
2. Пиши УКРАЇНСЬКОЮ мовою.
3. Фокусуйся на практичній інформації для мандрівників.
4. Повертай ТІЛЬКИ JSON."""


_FACILITY_PROMPTS = {
    "airport": AIRPORT_PROMPT,
    "aerodrome": AIRPORT_PROMPT,
    "railway": RAILWAY_PROMPT,
    "heliport": HELIPORT_PROMPT,
    "military": MILITARY_PROMPT,
    "bus": BUS_PROMPT,
}

_FACILITY_LABELS = {
    "airport": "Аеропорт",
    "aerodrome": "Аеродром",
    "railway": "Залізничний вокзал",
    "heliport": "Вертолітний майданчик",
    "military": "Військова авіабаза",
    "bus": "Автобусна станція",
}

_WIKI_KEYWORDS = {
    "airport": ["airport", "aerodrome", "aeroport"],
    "railway": ["railway", "station", "gare", "bahnhof", "stazione", "rail"],
    "heliport": ["heliport", "helipad", "helicopter"],
    "military": ["air base", "air force", "military", "airbase"],
    "bus": ["bus station", "bus terminal", "coach"],
}

_IMAGE_QUERIES = {
    "airport": "{name} airport terminal building",
    "aerodrome": "{name} aerodrome airfield",
    "railway": "{name} railway station building platform",
    "heliport": "{name} heliport helicopter landing",
    "military": "{name} air base aviation",
    "bus": "{name} bus station terminal",
}


async def _wikipedia_search(name: str, code: str, facility_type: str = "airport") -> dict | None:
    """Search Wikipedia for a facility article (tries EN then UK)."""
    keywords = _WIKI_KEYWORDS.get(facility_type, _WIKI_KEYWORDS["airport"])
    queries = [name, f"{code} {facility_type}", f"{name} {facility_type}"]

    for wiki_api, summary_tpl in [
        (WIKIPEDIA_SEARCH_API, WIKIPEDIA_SEARCH),
        (WIKI_UK_SEARCH_API, WIKI_UK_SUMMARY),
    ]:
        for query in queries:
            try:
                async with httpx.AsyncClient(timeout=10, headers=HTTP_HEADERS) as http:
                    resp = await http.get(wiki_api, params={
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
                    if any(kw in lower_title for kw in keywords) or code.lower() in lower_title:
                        summary = await _fetch_wiki_summary(title, summary_tpl)
                        if summary:
                            return summary
                        break

            except Exception:
                logger.warning("[transport-researcher] Wikipedia search failed for: %s", query)
                continue

    return None


async def _fetch_wiki_summary(title: str, url_template: str = WIKIPEDIA_SEARCH) -> dict | None:
    """Fetch Wikipedia page summary."""
    url = url_template.format(title=title.replace(" ", "_"))
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


async def _nominatim_location(lat: float, lng: float) -> dict | None:
    """Reverse geocode location for address context."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=HTTP_HEADERS) as http:
            resp = await http.get(NOMINATIM_URL, params={
                "lat": lat, "lon": lng,
                "format": "json", "addressdetails": 1,
                "zoom": 14, "accept-language": "uk",
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
        logger.warning("[transport-researcher] Nominatim failed for %.4f, %.4f", lat, lng)
        return None


async def research_airport(
    name: str,
    iata: str,
    city: str,
    country_code: str,
    lat: float,
    lng: float,
    facility_type: str = "airport",
) -> Optional[dict]:
    """Research a transport hub using Wikipedia + Nominatim + AI.

    Supports all facility types: airport, aerodrome, railway, heliport, military, bus.

    Returns dict with title, description, image_query or None on failure.
    """
    client = get_client()
    label = _FACILITY_LABELS.get(facility_type, "Аеропорт")

    wiki = await _wikipedia_search(name, iata, facility_type)
    nominatim = await _nominatim_location(lat, lng)

    context_parts = [
        f"{label}: {name}",
        f"IATA код: {iata}",
        f"Місто: {city}",
        f"Країна: {country_code}",
        f"Координати: {lat}, {lng}",
    ]

    if nominatim:
        if nominatim.get("city"):
            context_parts.append(f"Місто (з карти): {nominatim['city']}")
        if nominatim.get("state"):
            context_parts.append(f"Регіон: {nominatim['state']}")
        if nominatim.get("country"):
            context_parts.append(f"Країна (підтверджена): {nominatim['country']}")

    if wiki:
        context_parts.append("")
        context_parts.append("ДАНІ ВІКІПЕДІЇ:")
        context_parts.append(f"  Заголовок: {wiki.get('title', '')}")
        if wiki.get("description"):
            context_parts.append(f"  Опис: {wiki['description']}")
        extract = wiki.get("extract", "")
        if extract:
            context_parts.append(f"  Стаття: {extract[:2000]}")
        if wiki.get("url"):
            context_parts.append(f"  URL: {wiki['url']}")
    else:
        context_parts.append("")
        context_parts.append("Статтю у Вікіпедії не знайдено. Використовуй тільки базові дані вище.")

    context_parts.append("")
    context_parts.append(f"Напиши опис для мандрівників про {name} ({iata}). УКРАЇНСЬКОЮ мовою.")
    context_parts.append("ВИКОРИСТОВУЙ ТІЛЬКИ НАДАНІ ДАНІ. НЕ ВИГАДУЙ.")
    context_parts.append("Поверни JSON.")

    ai_context = "\n".join(context_parts)
    system_prompt = _FACILITY_PROMPTS.get(facility_type, AIRPORT_PROMPT)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": ai_context},
            ],
            max_tokens=2000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)

        if not result.get("title") or not result.get("description"):
            logger.warning("[transport-researcher] AI returned empty result for %s", iata)
            return None

        result["iata_code"] = iata
        result["facility_name"] = name
        result["facility_type"] = facility_type
        result["city"] = city
        result["country_code"] = country_code
        result["wikipedia"] = wiki

        img_tpl = _IMAGE_QUERIES.get(facility_type, "{name} airport terminal building")
        result["image_query"] = img_tpl.format(name=name)

        logger.info("[transport-researcher] Research completed for %s (%s) [%s]", name, iata, facility_type)
        return result

    except json.JSONDecodeError:
        logger.exception("[transport-researcher] AI JSON parse failed for %s", iata)
        return None
    except Exception:
        logger.exception("[transport-researcher] AI research failed for %s", iata)
        return None
