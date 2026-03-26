"""Editorial fact-checking for AI-generated social media posts.

Verifies factual claims (dates, events, locations, numbers) before publishing.
Uses a separate AI call with analytical prompting to catch hallucinated facts.
If a post fails verification, provides correction hints for regeneration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

from config.settings import settings, get_now_local

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


@dataclass
class FactCheckResult:
    passed: bool
    claims: list[dict] = field(default_factory=list)
    summary: str = ""
    suggestion: str = ""


FACT_CHECK_SYSTEM_PROMPT = """\
Ти — суворий редактор-фактчекер туристичної сторінки в соцмережах.
Твоє завдання: перевірити ВСІ фактичні твердження в пості ПЕРЕД публікацією.

СЬОГОДНІ: {today}

=== ЩО ПЕРЕВІРЯТИ ===
1. ДАТИ: Чи вказана дата реальна для цієї події? Чи не в минулому?
2. ПОДІЇ: Чи реально існує ця подія/змагання/фестиваль? Чи відбувається вона в зазначений час?
3. ЛОКАЦІЇ: Чи правильне місце проведення для згаданої події?
4. ЧИСЛА: Чи реалістичні ціни, відстані, місткість, роки?
5. ЧАСОВА ВІДПОВІДНІСТЬ: Чи описані як поточні/майбутні події дійсно такі відносно сьогоднішньої дати?

=== ВІДОМІ СПОРТИВНІ КАЛЕНДАРІ (обов'язково перевіряй!) ===
Формула 1:
- Сезон починається у березні (Австралія/Бахрейн), закінчується у грудні (Абу-Дабі)
- Гран-прі Барселони/Іспанії — ЧЕРВЕНЬ (не березень, не квітень!)
- Гран-прі Монако — ЧЕРВЕНЬ
- Тестові заїзди F1 — ЛЮТИЙ, перед початком сезону. НЕ в будь-якому іншому місяці!
- НІКОЛИ не кажи що F1 тести або гонка в місяці який не відповідає реальному календарю

Теніс:
- Australian Open — СІЧЕНЬ (Мельбурн)
- Roland Garros — ТРАВЕНЬ-ЧЕРВЕНЬ (Париж)
- Wimbledon — ЛИПЕНЬ (Лондон)
- US Open — СЕРПЕНЬ-ВЕРЕСЕНЬ (Нью-Йорк)

Футбол:
- Ліга Чемпіонів фінал — ТРАВЕНЬ-ЧЕРВЕНЬ
- Чемпіонат Європи — ЛІТО (кожні 4 роки)
- Чемпіонат Світу — кожні 4 роки (перевір рік і місто)

Олімпійські ігри: перевір рік та місто проведення.
Лижний сезон: ГРУДЕНЬ-КВІТЕНЬ (Альпи, Карпати), деякі високогірні — з листопада.

=== ТИПОВІ ПОМИЛКИ AI ЯКІ ТРЕБА ЛОВИТИ ===
- Розміщення спортивної події в НЕПРАВИЛЬНИЙ місяць
- "Тестові заїзди F1" у будь-якому місяці крім лютого
- Фестиваль який "відбудеться незабаром" але насправді вже пройшов або буде через 6+ місяців
- Вигадані назви змагань або фестивалів
- Конкретна дата події яку AI вигадав (не існує в реальності)
- Стадіон/арена в неправильному місті
- "Щорічний фестиваль X" який насправді не існує

=== ФОРМАТ ВІДПОВІДІ ===
Поверни ТІЛЬКИ валідний JSON (без markdown, без пояснень):
{{
  "verdict": "PASS" або "FAIL",
  "claims": [
    {{"claim": "опис твердження з поста", "status": "ok" або "suspicious" або "wrong", "reason": "чому"}}
  ],
  "summary": "коротке пояснення рішення",
  "suggestion": "якщо FAIL — конкретна порада як виправити пост"
}}

=== ПРАВИЛА ВЕРДИКТУ ===
- Якщо пост НЕ містить конкретних дат, подій з датами, або числових фактів — verdict = "PASS"
- Загальні описи місць (без прив'язки до конкретних подій) — verdict = "PASS"
- Якщо БУДЬ-ЯКЕ твердження має статус "wrong" — verdict ОБОВ'ЯЗКОВО = "FAIL"
- Якщо є "suspicious" твердження (не можеш перевірити, але виглядає сумнівно) — verdict = "FAIL"
- Будь СУВОРИМ: краще відхилити нормальний пост, ніж опублікувати з неправильними фактами
- Загальновідомі факти (столиця країни, відома пам'ятка, історична дата) — "ok"
"""

MAX_FACT_CHECK_RETRIES = 2


async def fact_check_post(post_text: str, content_type: str = "") -> FactCheckResult:
    """Verify factual claims in a post before publishing.

    Returns FactCheckResult with passed=True if the post is factually sound.
    On API errors, returns passed=True to avoid blocking publishing.
    """
    client = _get_client()
    today_str = get_now_local().strftime("%d %B %Y (%A)")
    system = FACT_CHECK_SYSTEM_PROMPT.format(today=today_str)

    user_msg = (
        f"Тип контенту: {content_type}\n\n"
        f"=== ПОСТ ДЛЯ ПЕРЕВІРКИ ===\n{post_text}\n=== КІНЕЦЬ ПОСТА ==="
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=800,
            temperature=0.1,
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)

        verdict = data.get("verdict", "FAIL").upper()
        result = FactCheckResult(
            passed=(verdict == "PASS"),
            claims=data.get("claims", []),
            summary=data.get("summary", ""),
            suggestion=data.get("suggestion", ""),
        )

        if result.passed:
            logger.info("FACT-CHECK PASS [%s]: %s", content_type, result.summary[:120])
        else:
            wrong = [c for c in result.claims if c.get("status") in ("wrong", "suspicious")]
            logger.warning(
                "FACT-CHECK FAIL [%s]: %s | Issues: %s",
                content_type,
                result.summary[:100],
                "; ".join(c.get("claim", "")[:80] for c in wrong),
            )

        return result

    except json.JSONDecodeError:
        logger.warning("Fact-check returned non-JSON — treating as PASS")
        return FactCheckResult(passed=True, summary="Could not parse fact-check response")
    except Exception:
        logger.warning("Fact-check API error — treating as PASS", exc_info=True)
        return FactCheckResult(passed=True, summary="Fact-check error — skipped")
