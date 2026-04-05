"""AI-powered geo-location researcher.

Takes coordinates and returns structured information about the area:
history, notable places, current news.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from content.ai_client import get_client

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 8_000  # ~2 printed pages

SYSTEM_PROMPT = """Ти — професійний географічний дослідник та краєзнавець.
Тобі дають координати місця (широта, довгота), країну (country_code) та, можливо, назву.
Твоє завдання — зібрати та структурувати всю доступну інформацію про цю місцевість.

ОБОВ'ЯЗКОВО поверни відповідь як JSON об'єкт з такою структурою:
{
  "location_name": "Назва найближчого населеного пункту або відомої місцевості (місто, село, район, парк, тощо)",
  "country_code": "ISO 3166-1 alpha-2 код країни, де НАСПРАВДІ знаходиться ця точка (наприклад CY, UA, IL, US)",
  "summary": "Загальний опис місця — 2-3 абзаци, що це за район/місто/село, чим відоме, загальна характеристика",
  "history": [
    {"period": "рік або період (наприклад 1240, 1800-1850, XIV ст.)", "description": "що відбувалось"}
  ],
  "places": [
    {"name": "Назва закладу", "type": "тип (museum/theater/hotel/restaurant/park/monument/church)", "description": "короткий опис", "url": "посилання якщо відоме, інакше null"}
  ],
  "news": [
    {"title": "Заголовок новини", "description": "Короткий опис що трапилось", "source": "джерело якщо відоме"}
  ]
}

КРИТИЧНО ВАЖЛИВІ ПРАВИЛА ТОЧНОСТІ:
1. НАЙВАЖЛИВІШЕ: координати визначають ТОЧНЕ місце на землі. НІКОЛИ не плутай країни та міста!
   Якщо координати вказують на Кіпр — пиши про Кіпр, НЕ про Ізраїль чи іншу країну.
2. ТОЧНІСТЬ НАЗВИ: location_name повинна відповідати САМЕ цим координатам (±1 км).
   НЕ пиши назву великого міста якщо координати вказують на передмістя або село поруч!
   Приклад: 50.37, 30.44 — це околиці Києва (Дарницький район), а НЕ Бровари (50.51, 30.79).
   Якщо точка в селі — пиши назву села, якщо в районі міста — пиши район.
3. Поле country_code ОБОВ'ЯЗКОВЕ — вкажи ISO код країни де реально знаходиться ця точка.
4. Перевір себе: чи всі згадані місця/новини/історія стосуються САМЕ цієї координати, а не сусіднього міста?
5. Якщо не впевнений де саме знаходиться точка — краще поверни порожній summary.

ЗАГАЛЬНІ ПРАВИЛА:
1. Відповідай мовою, вказаною в запиті (language параметр)
2. Історія — хронологічно, найважливіші події по роках/періодах
3. Місця — реальні заклади (музеї, театри, готелі, ресторани, парки, пам'ятки), з URL якщо знаєш
4. Новини — останні відомі події про цю місцевість
5. Якщо про місце НІЧОГО не відомо — поверни {"summary": "", "country_code": "", "history": [], "places": [], "news": []}
6. Сумарний текст НЕ БІЛЬШЕ 8000 символів
7. Тільки факти, без вигадок — якщо не впевнений, краще не згадуй
8. Без політики, без згадок про війни (окрім історичних фактів)
9. Поверни ТІЛЬКИ JSON, без додаткового тексту чи markdown"""


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
) -> dict | None:
    """Research a geographic location and return structured info.

    Returns parsed dict with keys: summary, history, places, news, country_code.
    Returns None if the AI found nothing meaningful.
    Sets result["_rejected"] = True and result["_reject_reason"] if editorial check fails.
    """
    client = get_client()

    location_desc = f"Координати: {latitude}, {longitude}"
    if expected_country:
        location_desc += f"\nКраїна (country_code): {expected_country}"
    if name:
        location_desc += f"\nНазва/підказка: {name}"

    user_prompt = (
        f"{location_desc}\n"
        f"Мова відповіді: {language}\n\n"
        "Дослідж цю місцевість та поверни структуровану інформацію у форматі JSON."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=3000,
            temperature=0.4,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)

        if not result.get("summary"):
            logger.info("AI returned empty summary for %s, %s", latitude, longitude)
            return None

        result["summary"] = result["summary"][:MAX_SUMMARY_CHARS]

        for key in ("history", "places", "news"):
            if not isinstance(result.get(key), list):
                result[key] = []

        # --- Editorial AI verification ---
        check = await _editorial_check(client, latitude, longitude, expected_country, result)
        if check is not None and not check.get("passed", True):
            result["_rejected"] = True
            result["_reject_reason"] = (
                f"Editorial: expected {expected_country}, "
                f"detected {check.get('detected_country', '?')}: "
                f"{check.get('reason', 'no reason')}"
            )
            logger.warning(
                "[researcher] REJECTED %s, %s: %s",
                latitude, longitude, result["_reject_reason"],
            )
            return result

        return result

    except json.JSONDecodeError:
        logger.exception("Failed to parse AI JSON response for %s, %s", latitude, longitude)
        return None
    except Exception:
        logger.exception("AI research failed for %s, %s", latitude, longitude)
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
