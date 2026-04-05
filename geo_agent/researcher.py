"""AI-powered geo-location researcher with hierarchical levels.

Generates research at 4 levels of detail:
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

# ── Level-specific system prompts ──

PROMPT_LOCATION = """Ти — місцевий гід-експерт з глибоким знанням конкретних місць.
Тобі дають ТОЧНІ координати (±200 метрів). Досліди ЦЮ КОНКРЕТНУ точку.

Поверни JSON:
{
  "location_name": "Точна назва місця/будівлі/парку/вулиці (±200м від координат)",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Що саме знаходиться в цій точці: яка будівля, парк, площа, вулиця. 1-2 абзаци.",
  "history": [{"period": "рік/період", "description": "що відбувалось саме тут"}],
  "places": [{"name": "Назва", "type": "museum/monument/restaurant/hotel/park/church/shop", "description": "опис", "url": "посилання або null"}],
  "news": [{"title": "Заголовок", "description": "Опис", "source": "джерело"}]
}

КРИТИЧНО:
1. Пиши ТІЛЬКИ про те що в радіусі 200 метрів від координат!
2. Якщо тут музей — опиши музей, якщо парк — парк, якщо житловий квартал — опиши квартал.
3. НЕ пиши про все місто! Тільки ця конкретна точка.
4. location_name — назва конкретного місця (вулиця, будівля, парк), НЕ назва міста.
5. country_code ОБОВ'ЯЗКОВО точний.
6. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_DISTRICT = """Ти — краєзнавець-дослідник міських районів.
Тобі дають координати — досліди РАЙОН/КВАРТАЛ навколо цієї точки (радіус ~2 км).

Поверни JSON:
{
  "location_name": "Назва району/кварталу",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Характеристика району: атмосфера, тип забудови, хто тут живе, чим відомий. 2-3 абзаци.",
  "history": [{"period": "рік/період", "description": "історія району"}],
  "places": [{"name": "Назва", "type": "тип", "description": "опис", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Опис", "source": "джерело"}]
}

ПРАВИЛА:
1. Фокус на РАЙОНІ (~2 км радіус), не на всьому місті.
2. Опиши характер району: туристичний, житловий, діловий, богемний.
3. Місця — головні атракції РАЙОНУ (5-10 шт), не всього міста.
4. Історія — як формувався цей район.
5. country_code ОБОВ'ЯЗКОВО. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_CITY = """Ти — туристичний експерт з міст світу.
Тобі дають координати — визнач місто та зроби повний огляд для туриста.

Поверни JSON:
{
  "location_name": "Назва міста",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Повний огляд міста для туриста: що це за місто, населення, клімат, найкращий час для відвідування, транспорт, кухня. 3-4 абзаци.",
  "history": [{"period": "рік/період", "description": "ключові моменти історії міста"}],
  "places": [{"name": "Назва", "type": "тип", "description": "чому варто відвідати", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Останні новини міста", "source": "джерело"}]
}

ПРАВИЛА:
1. Огляд ВСЬОГО МІСТА — головні визначні місця, райони, транспорт.
2. Місця — ТОП-10 must-visit місць МІСТА.
3. Включи практичну інформацію: як дістатися, де їсти, де зупинитися.
4. Історія — ключові віхи міста (5-8 подій).
5. country_code ОБОВ'ЯЗКОВО. Тільки факти. Поверни ТІЛЬКИ JSON."""

PROMPT_COUNTRY = """Ти — експерт з міжнародного туризму.
Тобі дають код країни — зроби повний туристичний огляд.

Поверни JSON:
{
  "location_name": "Назва країни",
  "country_code": "ISO alpha-2 код країни",
  "summary": "Повний туристичний огляд: що за країна, клімат, найкращий сезон, візовий режим, валюта, мова, безпека, кухня, менталітет. 4-5 абзаців.",
  "history": [{"period": "рік/період", "description": "ключові моменти історії країни"}],
  "places": [{"name": "Регіон/місто", "type": "region/city/island/park", "description": "чому варто відвідати", "url": "або null"}],
  "news": [{"title": "Заголовок", "description": "Актуальне для туристів", "source": "джерело"}]
}

ПРАВИЛА:
1. Фокус на ТУРИСТИЧНІЙ інформації для мандрівника.
2. Місця — ТОП-10 регіонів/міст КРАЇНИ які варто відвідати.
3. Практична інформація: візи, валюта, мова, безпека, транспорт між містами.
4. Що їсти, що привезти, культурні особливості.
5. country_code ОБОВ'ЯЗКОВО. Тільки факти. Поверни ТІЛЬКИ JSON."""

LEVEL_PROMPTS = {
    "location": PROMPT_LOCATION,
    "district": PROMPT_DISTRICT,
    "city": PROMPT_CITY,
    "country": PROMPT_COUNTRY,
}


EDITORIAL_CHECK_PROMPT = """Ти — редактор-верифікатор географічного контенту.
Тобі дають:
1. Координати точки (latitude, longitude)
2. Очікувану країну (expected_country_code)
3. Згенерований текст дослідження (JSON)

Твоє завдання: перевірити чи контент відповідає РЕАЛЬНІЙ локації.

Поверни JSON:
{
  "passed": true/false,
  "detected_country": "ISO код країни, яку описує контент",
  "reason": "коротке пояснення чому passed або не passed"
}

ПРАВИЛА ВІДБРАКОВКИ (passed=false):
- Контент описує ІНШУ країну ніж expected_country_code
- Згадані місця/міста знаходяться в іншій країні
- Координати явно не відповідають описаній місцевості
- Якщо expected_country_code порожній — перевіряй тільки що контент відповідає координатам

Поверни ТІЛЬКИ JSON."""


async def research_location(
    latitude: float,
    longitude: float,
    name: Optional[str] = None,
    language: str = "uk",
    expected_country: str = "",
    level: str = "location",
) -> dict | None:
    """Research a geographic location at a given detail level.

    Levels: location (~200m), district (~2km), city (~10km), country.
    Returns parsed dict or None. Sets result["_rejected"] if editorial check fails.
    """
    client = get_client()
    system_prompt = LEVEL_PROMPTS.get(level, PROMPT_LOCATION)

    if level == "country":
        location_desc = f"Країна: {expected_country}"
    else:
        location_desc = f"Координати: {latitude}, {longitude}"
        if expected_country:
            location_desc += f"\nКраїна (country_code): {expected_country}"
        if name:
            location_desc += f"\nНазва/підказка: {name}"

    user_prompt = (
        f"{location_desc}\n"
        f"Мова відповіді: {language}\n\n"
        "Поверни структуровану інформацію у форматі JSON."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=3000,
            temperature=0.4,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)

        if not result.get("summary"):
            logger.info("AI returned empty summary for %s, %s level=%s", latitude, longitude, level)
            return None

        result["summary"] = result["summary"][:MAX_SUMMARY_CHARS]
        for key in ("history", "places", "news"):
            if not isinstance(result.get(key), list):
                result[key] = []

        if level != "country":
            check = await _editorial_check(client, latitude, longitude, expected_country, result)
            if check is not None and not check.get("passed", True):
                result["_rejected"] = True
                result["_reject_reason"] = (
                    f"Editorial: expected {expected_country}, "
                    f"detected {check.get('detected_country', '?')}: "
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


async def _editorial_check(
    client,
    latitude: float,
    longitude: float,
    expected_country: str,
    research: dict,
) -> Optional[dict]:
    """Run editorial AI verification on generated research. Returns check result or None on error."""
    if not expected_country:
        return None

    try:
        summary_preview = research.get("summary", "")[:500]
        location_name = research.get("location_name", "")
        ai_country = research.get("country_code", "")

        if ai_country and ai_country.upper() != expected_country.upper():
            return {
                "passed": False,
                "detected_country": ai_country,
                "reason": f"AI returned country_code={ai_country}, expected {expected_country}",
            }

        check_prompt = (
            f"Координати: {latitude}, {longitude}\n"
            f"Очікувана країна: {expected_country}\n"
            f"location_name: {location_name}\n"
            f"summary: {summary_preview}\n\n"
            "Перевір: чи цей контент описує місце в очікуваній країні?"
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

        raw = response.choices[0].message.content.strip()
        return json.loads(raw)

    except Exception:
        logger.warning("[researcher] Editorial check failed, allowing content through")
        return None
