"""AI-powered geo-location researcher with hierarchical levels.

Two-step verification:
  Step 1: AI identifies the exact geo-chain (location → district → city → country)
  Step 2: AI generates content ONLY about the verified place

Levels:
  - location (~200m): specific POI, building, monument
  - district (~2km): neighborhood character, local attractions
  - city (~10km): city overview, transport, major landmarks
  - country: tourist overview, visa, currency, regions
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from content.ai_client import get_client

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 8_000

# ── Step 1: Geo-chain identification ──

GEO_IDENTIFY_PROMPT = """Ти — географ-верифікатор. Твоє єдине завдання: точно визначити що знаходиться за координатами.

Тобі дають GPS координати. Визнач ТОЧНИЙ ланцюжок геолокації:

Поверни JSON:
{
  "location": "Що саме тут: назва вулиці, будівлі, парку, пляжу (в радіусі 200м)",
  "district": "Назва району/кварталу міста (в радіусі 2 км)",
  "city": "Назва міста або населеного пункту",
  "region": "Область/провінція/округ",
  "country": "Назва країни",
  "country_code": "ISO alpha-2 код",
  "confidence": "high/medium/low"
}

ПРАВИЛА:
1. Використовуй ТІЛЬКИ загальновідомі географічні факти.
2. Якщо не впевнений що саме тут — пиши "невідомо" і confidence: "low".
3. НЕ ВИГАДУЙ назви! Краще написати "житловий квартал" ніж вигадати назву.
4. district — це район МІСТА з поля city. Не плутай міста між собою!
5. city — місто де знаходиться ЦЯ ТОЧКА, а не найближче відоме місто.
6. Поверни ТІЛЬКИ JSON."""


# ── Step 2: Level-specific content generation ──

PROMPT_LOCATION = """Ти — місцевий гід-експерт.
Тобі дають ПЕРЕВІРЕНУ інформацію про конкретне місце. Напиши дослідження.

Поверни JSON:
{
  "location_name": "ТОЧНА назва місця (з верифікації)",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Що саме знаходиться в цій точці. 1-2 абзаци.",
  "history": [{"period": "рік/період", "description": "що відбувалось саме тут"}],
  "places": [{"name": "Назва", "type": "museum/monument/restaurant/hotel/park/church/shop", "description": "опис", "url": "посилання або null"}],
  "news": [{"title": "Заголовок", "description": "Опис", "source": "джерело"}]
}

КРИТИЧНО:
1. Пиши ТІЛЬКИ про вказане місце (±200м)!
2. location_name — назва з верифікації, НЕ змінюй!
3. Місця (places) — лише ті що РЕАЛЬНО в радіусі 200м.
4. НЕ пиши про все місто. Тільки ця конкретна точка.
5. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_DISTRICT = """Ти — краєзнавець-дослідник.
Тобі дають ПЕРЕВІРЕНУ назву району та місто. Напиши дослідження ЦЬОГО РАЙОНУ.

Поверни JSON:
{
  "location_name": "Назва району (з верифікації)",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Характеристика ЦЬОГО району: атмосфера, тип забудови, чим відомий. 2-3 абзаци.",
  "history": [{"period": "рік/період", "description": "історія ЦЬОГО району"}],
  "places": [{"name": "Назва", "type": "тип", "description": "опис", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Опис", "source": "джерело"}]
}

КРИТИЧНО:
1. Пиши ТІЛЬКИ про вказаний район! НЕ про інше місто!
2. location_name — район з верифікації + місто. Наприклад: "Старе місто, Ларнака".
3. Місця — головні атракції ЦЬОГО РАЙОНУ (5-10 шт), НЕ всього міста.
4. Якщо район — це центр Ларнаки, пиши про центр Ларнаки, а не про Лімасол!
5. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_CITY = """Ти — туристичний експерт.
Тобі дають ПЕРЕВІРЕНУ назву міста. Напиши повний огляд ЦЬОГО МІСТА для туриста.

Поверни JSON:
{
  "location_name": "Назва міста (з верифікації)",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Повний огляд ЦЬОГО КОНКРЕТНОГО МІСТА: населення, клімат, транспорт, кухня. 3-4 абзаци.",
  "history": [{"period": "рік/період", "description": "ключові моменти історії ЦЬОГО міста"}],
  "places": [{"name": "Назва", "type": "тип", "description": "чому варто відвідати", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Новини ЦЬОГО міста", "source": "джерело"}]
}

КРИТИЧНО:
1. Пиши ТІЛЬКИ про вказане місто! Якщо місто Ларнака — пиши про Ларнаку, НЕ про Лімасол чи Фамагусту!
2. location_name — назва міста з верифікації, НЕ змінюй на інше місто!
3. Місця — ТОП-10 must-visit місць ЦЬОГО МІСТА, не іншого.
4. Історія — ключові віхи ЦЬОГО міста.
5. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_COUNTRY = """Ти — експерт з міжнародного туризму.
Тобі дають код країни — зроби повний туристичний огляд.

Поверни JSON:
{
  "location_name": "Назва країни",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Повний туристичний огляд: клімат, сезон, візи, валюта, мова, безпека, кухня. 4-5 абзаців.",
  "history": [{"period": "рік/період", "description": "ключові моменти історії"}],
  "places": [{"name": "Регіон/місто", "type": "region/city/island/park", "description": "чому варто відвідати", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Актуальне для туристів", "source": "джерело"}]
}

ПРАВИЛА:
1. Фокус на ТУРИСТИЧНІЙ інформації.
2. Місця — ТОП-10 регіонів/міст КРАЇНИ.
3. Практична інформація: візи, валюта, мова, безпека, транспорт.
4. Тільки факти. Поверни ТІЛЬКИ JSON."""

LEVEL_PROMPTS = {
    "location": PROMPT_LOCATION,
    "district": PROMPT_DISTRICT,
    "city": PROMPT_CITY,
    "country": PROMPT_COUNTRY,
}


# ── Editorial verification (Step 3) ──

EDITORIAL_CHECK_PROMPT = """Ти — редактор-верифікатор. Перевір чи контент відповідає ПРАВИЛЬНОМУ місцю.

Тобі дають:
1. Координати (latitude, longitude)
2. Верифіковану гео-інформацію (geo_chain)
3. Рівень дослідження (level)
4. Згенерований контент

Перевір:
- Чи location_name відповідає гео-ланцюжку? (район=район, місто=місто)
- Чи контент описує ПРАВИЛЬНЕ місце, а не сусіднє місто/район?
- Чи country_code відповідає очікуваному?

Поверни JSON:
{
  "passed": true/false,
  "detected_city": "яке місто насправді описується в контенті",
  "expected_city": "яке місто ПОВИННО описуватися",
  "reason": "коротке пояснення"
}

ПРАВИЛА ВІДБРАКОВКИ (passed=false):
- Контент про ІНШЕ місто ніж в geo_chain
- Район описує інше місто
- location_name не відповідає рівню (наприклад, для district рівня вказано назву країни)
- Поверни ТІЛЬКИ JSON."""


async def _identify_geo_chain(client, latitude: float, longitude: float, expected_country: str) -> dict | None:
    """Step 1: Identify the exact geo-chain for coordinates."""
    try:
        user_prompt = f"Координати: {latitude}, {longitude}"
        if expected_country:
            user_prompt += f"\nВідома країна: {expected_country}"

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": GEO_IDENTIFY_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=500,
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        chain = json.loads(raw)
        logger.info(
            "[researcher] Geo-chain: %s → %s → %s → %s (%s) confidence=%s",
            chain.get("location", "?"), chain.get("district", "?"),
            chain.get("city", "?"), chain.get("country", "?"),
            chain.get("country_code", "?"), chain.get("confidence", "?"),
        )
        return chain

    except Exception:
        logger.exception("[researcher] Geo-chain identification failed for %s, %s", latitude, longitude)
        return None


async def research_location(
    latitude: float,
    longitude: float,
    name: Optional[str] = None,
    language: str = "uk",
    expected_country: str = "",
    level: str = "location",
) -> dict | None:
    """Research a geographic location at a given detail level.

    Two-step process:
      1. Identify exact geo-chain (location → district → city → country)
      2. Generate content only about the verified place
    """
    client = get_client()

    if level == "country":
        return await _research_country(client, expected_country, language)

    geo_chain = await _identify_geo_chain(client, latitude, longitude, expected_country)
    if geo_chain is None:
        logger.warning("[researcher] Cannot identify geo-chain, skipping %s, %s level=%s", latitude, longitude, level)
        return None

    if geo_chain.get("confidence") == "low":
        logger.warning("[researcher] Low confidence for %s, %s — skipping level=%s", latitude, longitude, level)
        return None

    chain_country = (geo_chain.get("country_code") or "").upper()
    if expected_country and chain_country and chain_country != expected_country.upper():
        logger.warning("[researcher] Geo-chain country %s != expected %s, skipping", chain_country, expected_country)
        return None

    system_prompt = LEVEL_PROMPTS.get(level, PROMPT_LOCATION)

    verified_context = _build_verified_context(geo_chain, level, latitude, longitude, language)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": verified_context},
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

        check = await _editorial_check_v2(client, latitude, longitude, geo_chain, level, result)
        if check is not None and not check.get("passed", True):
            result["_rejected"] = True
            result["_reject_reason"] = (
                f"Editorial: expected city={check.get('expected_city', '?')}, "
                f"detected city={check.get('detected_city', '?')}: "
                f"{check.get('reason', 'no reason')}"
            )
            logger.warning("[researcher] REJECTED %s, %s level=%s: %s",
                           latitude, longitude, level, result["_reject_reason"])
            return result

        return result

    except json.JSONDecodeError:
        logger.exception("Failed to parse AI JSON for %s, %s level=%s", latitude, longitude, level)
        return None
    except Exception:
        logger.exception("AI research failed for %s, %s level=%s", latitude, longitude, level)
        raise


def _build_verified_context(geo_chain: dict, level: str, lat: float, lng: float, language: str) -> str:
    """Build the user prompt with verified geo-chain context."""
    parts = [
        f"Координати: {lat}, {lng}",
        f"Мова відповіді: {language}",
        "",
        "ВЕРИФІКОВАНИЙ ГЕО-ЛАНЦЮЖОК (не змінюй!):",
        f"  Локація: {geo_chain.get('location', 'невідомо')}",
        f"  Район: {geo_chain.get('district', 'невідомо')}",
        f"  Місто: {geo_chain.get('city', 'невідомо')}",
        f"  Область: {geo_chain.get('region', 'невідомо')}",
        f"  Країна: {geo_chain.get('country', 'невідомо')} ({geo_chain.get('country_code', '?')})",
        "",
    ]

    if level == "location":
        parts.append(f"ЗАВДАННЯ: Напиши дослідження про '{geo_chain.get('location', '')}' в місті {geo_chain.get('city', '')}.")
    elif level == "district":
        parts.append(f"ЗАВДАННЯ: Напиши дослідження про район '{geo_chain.get('district', '')}' міста {geo_chain.get('city', '')}.")
        parts.append(f"УВАГА: Район належить місту {geo_chain.get('city', '')}! НЕ пиши про інші міста!")
    elif level == "city":
        parts.append(f"ЗАВДАННЯ: Напиши огляд міста '{geo_chain.get('city', '')}'.")
        parts.append(f"УВАГА: Пиши ТІЛЬКИ про місто {geo_chain.get('city', '')}! НЕ плутай з іншими містами!")

    parts.append("\nПоверни структуровану інформацію у форматі JSON.")
    return "\n".join(parts)


async def _research_country(client, country_code: str, language: str) -> dict | None:
    """Generate country-level research (no geo-chain needed)."""
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


async def _editorial_check_v2(
    client,
    latitude: float,
    longitude: float,
    geo_chain: dict,
    level: str,
    research: dict,
) -> Optional[dict]:
    """Enhanced editorial check: verify geo-chain consistency."""
    try:
        ai_country = (research.get("country_code") or "").upper()
        chain_country = (geo_chain.get("country_code") or "").upper()
        if ai_country and chain_country and ai_country != chain_country:
            return {
                "passed": False,
                "detected_city": research.get("location_name", "?"),
                "expected_city": geo_chain.get("city", "?"),
                "reason": f"country mismatch: content={ai_country}, chain={chain_country}",
            }

        location_name = (research.get("location_name") or "").lower()
        chain_city = (geo_chain.get("city") or "").lower()

        if level == "city" and chain_city and location_name:
            if chain_city not in location_name and location_name not in chain_city:
                return {
                    "passed": False,
                    "detected_city": research.get("location_name", "?"),
                    "expected_city": geo_chain.get("city", "?"),
                    "reason": f"city level describes '{research.get('location_name')}' instead of '{geo_chain.get('city')}'",
                }

        check_prompt = (
            f"Координати: {latitude}, {longitude}\n"
            f"Рівень: {level}\n"
            f"Гео-ланцюжок: місто={geo_chain.get('city')}, район={geo_chain.get('district')}, країна={geo_chain.get('country_code')}\n"
            f"location_name в контенті: {research.get('location_name', '')}\n"
            f"summary (перші 300 симв): {(research.get('summary') or '')[:300]}\n\n"
            "Перевір: чи контент описує правильне місце з гео-ланцюжка?"
        )

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": EDITORIAL_CHECK_PROMPT},
                {"role": "user", "content": check_prompt},
            ],
            max_tokens=300,
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        return json.loads(response.choices[0].message.content.strip())

    except Exception:
        logger.warning("[researcher] Editorial check v2 failed, allowing content through")
        return None
