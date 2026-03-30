"""Editorial fact-checking for AI-generated social media posts.

Verifies factual claims (dates, events, locations, numbers) before publishing.
Uses a separate AI call with analytical prompting to catch hallucinated facts.
If a post fails verification, provides correction hints for regeneration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from config.settings import settings, get_now_local
from content.ai_client import get_client
from content.tourism_topics import contains_blocked_territory

logger = logging.getLogger(__name__)


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
6. ЗАБОРОНЕНІ ТЕРИТОРІЇ ТА НЕБЕЗПЕЧНІ ЗОНИ:
   Пост НЕ ПОВИНЕН згадувати або РЕКОМЕНДУВАТИ відвідати:
   - Окуповані: Крим (Ялта, Севастополь та ін.), Донецьк, Луганськ, Маріуполь
   - Росія, Білорусь — повна заборона
   - Санкційні країни: Північна Корея, Іран, Сирія, Куба, Венесуела, М'янма
   - Зони тероризму/конфліктів: Афганістан, Сомалі, Ємен, Лівія, Ірак, Пд.Судан, ЦАР, Малі,
     Буркіна-Фасо, Нігер, Чад, Гаїті — НЕБЕЗПЕЧНО ДЛЯ ТУРИСТІВ
   - Місця активних природних катастроф (цунамі, землетруси, виверження, урагани)
   Якщо пост рекомендує БУДЬ-ЯКУ небезпечну територію — verdict = "FAIL" ОБОВ'ЯЗКОВО.
7. БЕЗПЕКА ТУРИСТІВ: Пост НЕ ПОВИНЕН рекомендувати місця де існує реальна загроза
   життю туристів (тероризм, збройні конфлікти, викрадення, природні катастрофи).
8. ІНФОРМАЦІЙНА ЦІННІСТЬ (КРИТИЧНО!):
   Пост ПОВИНЕН містити КОНКРЕТНУ, КОРИСНУ інформацію для мандрівника.
   ВІДХИЛЯЙ пости які:
   - Не називають КОНКРЕТНИХ місць (місто, країну) — пишуть "ці міста" або "ці місця" без назв
   - Містять лише загальні фрази типу "це створено для вас" без деталей
   - Не дають жодної практичної інформації (ні цін, ні транспорту, ні конкретних деталей)
   - Описують тему абстрактно без ЖОДНОГО конкретного прикладу
   Мінімум: пост ПОВИНЕН назвати хоча б ОДНЕ конкретне місце (місто + країна).
   Якщо пост порожній за змістом (лише емоції без фактів) — verdict = "FAIL" з
   suggestion "Додай конкретні деталі: назви міст, ціни, транспорт, практичні поради".

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
- Якщо пост містить конкретне місце (місто, країна) і корисну інформацію — verdict = "PASS"
- Загальні описи місць (з конкретною назвою міста/країни) — verdict = "PASS"
- Якщо БУДЬ-ЯКЕ твердження має статус "wrong" — verdict ОБОВ'ЯЗКОВО = "FAIL"
- Якщо є "suspicious" твердження (не можеш перевірити, але виглядає сумнівно) — verdict = "FAIL"
- Якщо пост НЕ називає жодного конкретного місця (міста/країни) — verdict = "FAIL"
- Якщо пост — це лише абстрактний заклик без конкретної інформації — verdict = "FAIL"
- Будь СУВОРИМ: краще відхилити нормальний пост, ніж опублікувати з неправильними фактами або без інформації
- Загальновідомі факти (столиця країни, відома пам'ятка, історична дата) — "ok"
"""

MAX_FACT_CHECK_RETRIES = 2

_VAGUE_PHRASES = [
    "ці міста", "ці місця", "ці напрямки", "ці локації", "ці країни",
    "these towns", "these cities", "these places", "these destinations",
    "створені для вас", "мають бути у вашому списку",
    "не пропустіть шанс", "варто відвідати",
]


def _check_information_density(text: str, content_type: str) -> FactCheckResult | None:
    """Quick programmatic check that the post contains substantive info.

    Returns a failing FactCheckResult if the post is too vague, or None if OK.
    """
    if content_type == "feature":
        return None

    text_lower = text.lower()

    vague_count = sum(1 for phrase in _VAGUE_PHRASES if phrase in text_lower)

    has_source_line = "джерело:" in text_lower or "📰" in text_lower
    text_for_check = text_lower
    if has_source_line:
        for line in text.split("\n"):
            if "джерело" in line.lower() or "📰" in line.lower():
                text_for_check = text_lower.replace(line.lower(), "")
                break

    words = text_for_check.split()
    capitalized = [w for w in words if w and w[0].isupper() and len(w) > 2
                   and not w.startswith("http") and w not in ("I'M", "IN", "Джерело:", "Не")]

    if vague_count >= 2 and len(capitalized) < 3:
        return FactCheckResult(
            passed=False,
            claims=[{
                "claim": "Пост не містить конкретної інформації",
                "status": "wrong",
                "reason": "Порожній пост без конкретних місць, деталей, цін чи практичних порад",
            }],
            summary="Пост порожній — немає конкретної інформації для мандрівника",
            suggestion="Додай конкретні деталі: назви міст та країн, ціни, як дістатися, де зупинитися, що спробувати",
        )

    return None


async def fact_check_post(post_text: str, content_type: str = "") -> FactCheckResult:
    """Verify factual claims in a post before publishing.

    Returns FactCheckResult with passed=True if the post is factually sound.
    On API errors, returns passed=True to avoid blocking publishing.
    """
    blocked = contains_blocked_territory(post_text)
    if blocked:
        logger.warning("TERRITORY BLOCK in fact-check: text contains '%s'", blocked)
        return FactCheckResult(
            passed=False,
            claims=[{"claim": f"Згадка забороненої території: {blocked}", "status": "wrong",
                     "reason": "Окупована/анексована територія або зона бойових дій"}],
            summary=f"Пост містить заборонену територію: {blocked}",
            suggestion="Повністю переписати пост без згадки окупованих територій, Росії або зон бойових дій",
        )

    density_fail = _check_information_density(post_text, content_type)
    if density_fail:
        logger.warning("QUALITY BLOCK: post lacks information density — %s", density_fail.summary)
        return density_fail

    client = get_client()
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
