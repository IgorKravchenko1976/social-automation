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
Тобі дають координати місця (широта, довгота) та, можливо, назву.
Твоє завдання — зібрати та структурувати всю доступну інформацію про цю місцевість.

ОБОВ'ЯЗКОВО поверни відповідь як JSON об'єкт з такою структурою:
{
  "location_name": "Назва найближчого населеного пункту або відомої місцевості (місто, село, район, парк, тощо)",
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

ПРАВИЛА:
1. Відповідай мовою, вказаною в запиті (language параметр)
2. Історія — хронологічно, найважливіші події по роках/періодах
3. Місця — реальні заклади (музеї, театри, готелі, ресторани, парки, пам'ятки), з URL якщо знаєш
4. Новини — останні відомі події про цю місцевість
5. Якщо про місце НІЧОГО не відомо — поверни {"summary": "", "history": [], "places": [], "news": []}
6. Сумарний текст НЕ БІЛЬШЕ 8000 символів (приблизно 2 друковані сторінки)
7. Тільки факти, без вигадок — якщо не впевнений, краще не згадуй
8. Без політики, без згадок про війни (окрім історичних фактів)
9. Поверни ТІЛЬКИ JSON, без додаткового тексту чи markdown"""


async def research_location(
    latitude: float,
    longitude: float,
    name: Optional[str] = None,
    language: str = "uk",
) -> dict | None:
    """Research a geographic location and return structured info.

    Returns parsed dict with keys: summary, history, places, news.
    Returns None if the AI found nothing meaningful.
    """
    client = get_client()

    location_desc = f"Координати: {latitude}, {longitude}"
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

        return result

    except json.JSONDecodeError:
        logger.exception("Failed to parse AI JSON response for %s, %s", latitude, longitude)
        return None
    except Exception:
        logger.exception("AI research failed for %s, %s", latitude, longitude)
        raise
