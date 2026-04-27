"""AI content generation: posts, replies, topics, and image prompts.

Sub-modules (split for clarity):
- content.prompts      — system prompt templates and text cleanup
- content.ai_client    — shared OpenAI client singleton
- content.geo          — coordinate extraction and map links
- content.translator   — multi-language translation
"""
from __future__ import annotations

import logging

from config.settings import settings
from config.platforms import Platform, PLATFORM_LIMITS
from content.product_knowledge import PRODUCT_KNOWLEDGE
from content.tourism_topics import contains_blocked_territory

from content.ai_client import get_client
from content.prompts import (
    BlockedTerritoryError,
    CONTENT_TYPE_PROMPTS,
    SYSTEM_PROMPT_FEATURE,
    clean_ai_meta,
)
from content.geo import extract_location_coordinates, build_map_link  # noqa: F401 — re-export
from content.translator import (  # noqa: F401 — re-export
    translate_post,
    BLOG_LANGUAGES,
    LANG_NAMES,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BlockedTerritoryError",
    "generate_post_text",
    "generate_auto_reply",
    "generate_unique_topic",
    "generate_image_prompt",
    "extract_location_coordinates",
    "build_map_link",
    "translate_post",
    "BLOG_LANGUAGES",
    "LANG_NAMES",
]


# ---------------------------------------------------------------------------
# Post text generation
# ---------------------------------------------------------------------------

async def generate_post_text(
    topic: str,
    platform: Platform,
    *,
    source_text: str = "",
    content_type: str = "feature",
) -> str:
    """Generate a platform-adapted post text using OpenAI.

    content_type: feature | tourism_news | active_travel | leisure_travel | poi_spotlight
    """
    client = get_client()
    limits = PLATFORM_LIMITS[platform]

    user_prompt_parts = [f"Platform: {platform.value} (max {limits['max_text_length']} chars)"]
    if limits["hashtags"]:
        user_prompt_parts.append("Include 3-5 relevant hashtags.")
    if not limits["supports_links"]:
        user_prompt_parts.append("Do NOT include links (platform does not support clickable links).")

    if content_type == "city_pulse" and source_text:
        user_prompt_parts.append(f"\n=== ДАНІ ПРО ПОДІЮ ===\n{source_text[:3000]}")
        user_prompt_parts.append(
            "\n=== ІНСТРУКЦІЯ ==="
            "\nСтвори ПРИВАБЛИВИЙ пост про ЦЮ ПОДІЮ на основі наданих даних."
            "\nВикористовуй ВИКЛЮЧНО факти з даних вище — нічого не вигадуй."
            "\nДата, час, місце, ціна — ОБОВ'ЯЗКОВО якщо є в даних."
            "\nНЕ вигадуй додаткових деталей яких немає в даних."
        )
    elif content_type == "poi_spotlight" and source_text:
        user_prompt_parts.append(f"\n{source_text[:3000]}")
        user_prompt_parts.append(
            "\n=== ІНСТРУКЦІЯ ==="
            "\nСтвори пост КОНКРЕТНО ПРО ЦЮ ТОЧКУ (не про місто чи район!)."
            "\nВикористовуй ВИКЛЮЧНО дані вище. Якщо даних мало — пост буде коротким, це нормально."
            "\nНЕ ВИГАДУЙ: ціни, бюджети, погоду, транспорт, житло — якщо цього немає в даних."
            "\nНЕ УЗАГАЛЬНЮЙ: не пиши про місто/країну загалом, ТІЛЬКИ про цю конкретну точку."
            "\nКожне речення повинно стосуватися САМЕ цієї точки, а не навколишньої території."
        )
    elif source_text:
        user_prompt_parts.append(f"\nSource material to rewrite:\n{source_text[:2000]}")

    if topic:
        user_prompt_parts.append(f"\nTopic to write about:\n{topic}")

    user_prompt_parts.append("\nGenerate one post. Return ONLY the post text, nothing else.")

    prompt_template = CONTENT_TYPE_PROMPTS.get(content_type, SYSTEM_PROMPT_FEATURE)
    format_kwargs = {"app_name": settings.app_name, "app_website": settings.app_website}
    if "{product_knowledge}" in prompt_template:
        format_kwargs["product_knowledge"] = PRODUCT_KNOWLEDGE
    system = prompt_template.format(**format_kwargs)

    from config.settings import get_now_local
    today_str = get_now_local().strftime("%d.%m.%Y")
    system += (
        f"\n\nСЬОГОДНІШНЯ ДАТА: {today_str}. Публікуй тільки актуальну інформацію. "
        "Пост буде перевірений фактчекером — невірні дати та факти = відхилення."
    )

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user_prompt_parts)},
        ],
        max_tokens=1000,
        temperature=0.8,
    )

    text = response.choices[0].message.content.strip()
    text = clean_ai_meta(text)

    blocked = contains_blocked_territory(text)
    if blocked:
        logger.warning("TERRITORY BLOCK: generated text contains '%s'", blocked)
        raise BlockedTerritoryError(blocked)

    max_len = limits["max_text_length"]
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


# ---------------------------------------------------------------------------
# Auto-reply generation
# ---------------------------------------------------------------------------

async def generate_auto_reply(
    incoming_message: str,
    platform: Platform,
    sender_name: str = "",
    post_context: str = "",
    prior_replies: int = 0,
) -> tuple[str, str]:
    """Generate a reply and classify the message.

    Returns (reply_text, category).
    Category is one of: faq, support, spam, human_needed.
    """
    client = get_client()

    post_block = ""
    if post_context:
        post_block = (
            f"\n=== ПОСТ ПІД ЯКИМ ЗАЛИШЕНО КОМЕНТАР ===\n"
            f"{post_context[:1500]}\n"
            f"=== КІНЕЦЬ ПОСТА ===\n"
        )

    system = (
        f'Ти — адміністратор сторінки "{settings.app_name}" про подорожі.\n'
        f"Сайт: {settings.app_website}\n\n"
        f"{PRODUCT_KNOWLEDGE}\n"
        f"{post_block}\n"
        "=== ХТО ТИ ===\n"
        "Ти відповідаєш від імені СТОРІНКИ (як адмін/редакція), а НЕ як бот або штучний інтелект.\n"
        "НІКОЛИ не кажи що ти бот, AI, чат-бот, штучний інтелект або автоматична відповідь.\n"
        "Говори від першої особи множини ('ми', 'наша команда') або нейтрально.\n\n"
        "=== ГОЛОВНЕ ПРАВИЛО: ВІДПОВІДЬ ПРО ПОСТ ===\n"
        "Якщо є контекст поста (вище) — ВІДПОВІДАЙ в контексті цього поста:\n"
        "- На питання про деталі — розкрий тему поста глибше, але ТІЛЬКИ тим що є в пості\n"
        "- На запити 'розкажи більше' — дай додаткові деталі з тексту поста\n"
        "- Якщо коментар стосується місця з поста — розкажи те що відомо з поста\n"
        "- Якщо доречно — згадай що в додатку I'M IN можна знайти це місце на карті\n\n"
        "=== ВАЖЛИВО: ПОСИЛАННЯ ===\n"
        "Веб-додаток app.im-in.net показує події з фото, описом та картою.\n"
        "Посилання на подію: https://app.im-in.net/e/{event_id}\n"
        "Маркетинговий сайт www.im-in.net — сторінка додатку (опис, App Store).\n"
        "На маркетинговому сайті НЕМАЄ:\n"
        "  - Деталей про події, фестивалі, змагання\n"
        "  - Розкладів, цін, програм заходів\n"
        "  - Новин або статей про подорожі\n"
        "  - Додаткової інформації про місця з постів\n"
        "Пости створюються з ЗОВНІШНІХ джерел — на сайті точно та сама інформація що і в пості.\n\n"
        "НІКОЛИ не кажи:\n"
        "  ❌ 'Дізнайтеся більше на www.im-in.net'\n"
        "  ❌ 'Деталі на нашому сайті www.im-in.net'\n"
        "Замість цього:\n"
        "  ✅ 'Дивіться в I'M IN: app.im-in.net' — веб-додаток з картою\n"
        "  ✅ Якщо пост має 'Джерело:' або '📰 Джерело:' — дай це посилання\n"
        "  ✅ Якщо подія загальновідома — порадь шукати на офіційному сайті події\n"
        "     (наприклад: 'Деталі на офіційному сайті Formula1.com')\n"
        "  ✅ Якщо не знаєш джерело — скажи 'рекомендуємо перевірити на офіційному сайті події'\n\n"
        "app.im-in.net — веб-додаток з картою подій та фото. Згадуй коли:\n"
        "  - Мова про конкретне місце з поста — 'дивіться на app.im-in.net'\n"
        "  - Сам додаток I'M IN (функції, завантаження, карта)\n"
        "  - Загальні питання про додаток\n"
        "  - 'Завантажте додаток I'M IN щоб знайти це місце на карті'\n\n"
        "=== ПРАВИЛА ВІДПОВІДІ ===\n"
        "- ВИЗНАЧИ мову повідомлення і ВІДПОВІДАЙ ТІЄЮ Ж МОВОЮ.\n"
        "- Відповідай дружньо та ЗМІСТОВНО. Не просто 'дякую' — дай конкретну інформацію.\n"
        "- Коротко (2-4 речення). Не пиши стіну тексту під коментарем.\n"
        f"- Це відповідь №{prior_replies + 1} цьому автору в цьому треді.\n"
        "- ВІТАННЯ ('Привіт', 'Добрий день', 'Вітаємо' тощо) — ТІЛЬКИ в ПЕРШІЙ відповіді автору (відповідь №1).\n"
        "  Якщо це НЕ перша відповідь — НЕ вітайся, одразу переходь до суті.\n"
        "- На привітання — привітайся тепло, запитай чим можемо допомогти.\n"
        "- На скарги або складні питання — класифікуй як human_needed.\n"
        "- Про додаток I'M IN — розкажи як він допомагає мандрівникам "
        "(карта подій, фото/відео з геолокацією, спілкування). "
        "Тут давай посилання www.im-in.net.\n"
        "- Про ціну додатку — безкоштовний.\n"
        "- Про дату запуску — скоро, слідкуйте за оновленнями.\n\n"
        "=== КЛАСИФІКАЦІЯ SPAM — ДУЖЕ ОБЕРЕЖНО ===\n"
        "spam — ТІЛЬКИ для ЯВНОГО спаму:\n"
        "  - Реклама чужих продуктів/послуг з посиланнями\n"
        "  - Випадкові символи, незрозумілий набір тексту\n"
        "  - Масова розсилка, нігерійські листи, фішинг\n"
        "НЕ SPAM (класифікуй як faq або support):\n"
        "  - Довгі коментарі з корисною інформацією (розклади, дати, факти)\n"
        "  - Коментарі з багатьма пунктами/списками (людина ділиться інформацією)\n"
        "  - Коментарі про спорт, подорожі, події — навіть якщо дуже довгі\n"
        "  - Питання, навіть незрозумілі або некоректні\n"
        "  - Емоції, реакції на пост (навіть просто емодзі або 'круто!')\n"
        "Якщо сумніваєшся — класифікуй як faq, НЕ як spam.\n\n"
        "=== КРИТИЧНО: ДАТИ ТА ФАКТИ — НЕ ВИГАДУВАТИ ===\n"
        "- НІКОЛИ не вигадуй дати, числа, факти яких немає в тексті поста.\n"
        "- Якщо в пості є дата (наприклад '📅 22 березня 2026' або 'Дата публікації: 22.03.2026') — "
        "використовуй САМЕ ЦЮ дату, не змінюй її.\n"
        "- Якщо в пості НЕМАЄ конкретної дати — НЕ ДОДАВАЙ дату від себе. "
        "Скажи 'деталі за посиланням у пості' замість вигаданої дати.\n"
        "- НІКОЛИ не 'осучаснюй' старі дати. Якщо в пості написано 2023 — це 2023, не пиши 2026.\n"
        "- Якщо людина запитує про дату а її немає в пості — чесно скажи "
        "'точну дату ми не знаємо, деталі в оригінальному джерелі'.\n\n"
        "After your reply, on a NEW line write exactly one of these categories:\n"
        "CATEGORY: faq | support | spam | human_needed"
    )

    user_content = f"From: {sender_name}\nMessage: {incoming_message}"
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        max_tokens=500,
        temperature=0.5,
    )

    full_reply = response.choices[0].message.content.strip()

    category = "support"
    reply_lines = []
    for line in full_reply.split("\n"):
        if line.strip().startswith("CATEGORY:"):
            cat = line.split(":", 1)[1].strip().lower()
            if cat in ("faq", "support", "spam", "human_needed"):
                category = cat
        else:
            reply_lines.append(line)

    reply_text = "\n".join(reply_lines).strip()
    return reply_text, category


# ---------------------------------------------------------------------------
# Topic generation
# ---------------------------------------------------------------------------

async def generate_unique_topic(
    direction: str,
    content_type: str,
    recent_titles: list[str],
    *,
    travel_context: str = "",
) -> str:
    """Ask AI to generate a specific unique topic within a broad direction.

    The AI receives the direction category and recent post titles (last 60 days)
    so it avoids repetition. Topics are always tied to a specific place/location.
    """
    client = get_client()

    recent_block = ""
    if recent_titles:
        titles_text = "\n".join(f"- {t}" for t in recent_titles[-80:])
        recent_block = f"\n\nОСЬ ТЕМИ ПОСТІВ ЗА ОСТАННІ 60 ДНІВ (НЕ ПОВТОРЮЙ ЇХ!):\n{titles_text}"

    context_block = ""
    if travel_context and content_type == "feature":
        context_block = f"\n\nСЬОГОДНІШНЯ ПОДОРОЖНЯ ТЕМА (прив'яжи функцію до неї): {travel_context}"

    type_hints = {
        "active_travel": (
            "спортивну подію, змагання, маршрут або активність ДЛЯ МАНДРІВНИКІВ. "
            "ОБОВ'ЯЗКОВО прив'яжи до конкретного МІСЦЯ (місто, країна). "
            "Пріоритет: свіжі події (турніри, змагання, результати, відкриття сезону). "
            "Також цікаво: школи/академії спорту, ціни на готелі під час подій, "
            "поради як зекономити, транспорт до локації, альтернативне проживання."
        ),
        "leisure_travel": (
            "конкретну локацію, місце, вулицю, ресторан, музей, парк, фестиваль, "
            "архітектурний об'єкт або гастро-заклад ДЛЯ МАНДРІВНИКІВ. "
            "ОБОВ'ЯЗКОВО вкажи МІСТО та КРАЇНУ. "
            "Напрямок широкий: наприклад 'Львів' — це десятки тем (кав'ярні, музеї, "
            "вулиці, архітектура, фестивалі, концерти, парки, історія). "
            "Пріоритет: свіжі події, фестивалі, сезонні рекомендації."
        ),
        "feature": (
            "конкретну функцію або можливість мобільного додатку I'M IN для мандрівників. "
            "Покажи як ця функція допомагає мандрівнику в РЕАЛЬНІЙ подорожній ситуації. "
            "Прив'яжи функцію до сьогоднішньої подорожньої теми (якщо вказана)."
        ),
    }
    hint = type_hints.get(content_type, "цікаву тему для мандрівників прив'язану до конкретного місця")

    from config.settings import get_now_local
    today_str = get_now_local().strftime("%d %B %Y")

    system_msg = (
        "Ти генеруєш ОДНУ конкретну тему для поста в соціальних мережах про подорожі. "
        f"СЬОГОДНІ: {today_str}. "
        "ГОЛОВНЕ ПРАВИЛО: кожна тема ПРИВ'ЯЗАНА до КОНКРЕТНОГО МІСЦЯ (місто, локація, країна). "
        "Тема повинна бути АКТУАЛЬНОЮ — пов'язаною з поточним сезоном, свіжими подіями, "
        "або тим що відбувається ЗАРАЗ. "
        "НІКОЛИ не пропонуй теми з минулих років. Якщо згадуєш подію — вона має бути актуальна. "
        "Тема повинна бути унікальною і НЕ повторювати жодну з наведених минулих тем. "
        "Напрямок — це ШИРОКЕ поле з десятками можливих тем (одне місто = ресторани, "
        "музеї, вулиці, архітектура, події, фестивалі, історія, кухня тощо). "
        "\n\nЗАБОРОНЕНІ ТЕРИТОРІЇ ТА НЕБЕЗПЕЧНІ ЗОНИ (АБСОЛЮТНА ЗАБОРОНА!): "
        "НІКОЛИ не пропонуй теми про: "
        "1) ОКУПОВАНІ: Крим (Ялта, Севастополь та ін.), Донецьк, Луганськ, Маріуполь. "
        "2) РОСІЯ/БІЛОРУСЬ: будь-яке місто. "
        "3) САНКЦІЙНІ: Північна Корея, Іран, Сирія, Куба, Венесуела, М'янма. "
        "4) ТЕРОРИЗМ/КОНФЛІКТИ: Афганістан, Сомалі, Ємен, Лівія, Ірак, Пд.Судан, ЦАР, "
        "Малі, Буркіна-Фасо, Нігер, Чад, Гаїті — там вбивають туристів. "
        "5) ПРИРОДНІ КАТАСТРОФИ: не рекомендуй місця з активними стихійними лихами. "
        "Ці території небезпечні. Заміни на безпечне місце. "
        "\n\nКРИТИЧНО ЩОДО ДАТ ТА ПОДІЙ: "
        "Якщо згадуєш конкретну подію (змагання, фестиваль, турнір) — ти ПОВИНЕН бути "
        "100% впевнений що вона дійсно відбувається ЗАРАЗ або НЕЗАБАРОМ. "
        "НІКОЛИ не вигадуй дати подій. Приклади помилок: 'тестові заїзди F1 у березні' "
        "(вони тільки у лютому), 'Wimbledon у квітні' (він у липні). "
        "Якщо не впевнений у даті події — НЕ ВКАЗУЙ конкретну подію з датою. "
        "Замість цього пропонуй тему про МІСЦЕ (стадіон, трасу, парк) без прив'язки до "
        "конкретної дати. Твій пост БУДЕ перевірений фактчекером і відхилений якщо дати невірні. "
        "\nПоверни ТІЛЬКИ тему (1-2 речення), без пояснень, нумерації чи коментарів."
    )

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": (
                    f"Напрямок: {direction}\n"
                    f"Потрібно придумати: {hint}\n"
                    f"Мова: українська{context_block}{recent_block}\n\n"
                    "Згенеруй одну конкретну, цікаву тему у цьому напрямку, "
                    "яка відрізняється від усіх перерахованих вище. "
                    "Тема повинна бути прив'язана до конкретного місця!"
                ),
            },
        ],
        max_tokens=150,
        temperature=1.0,
    )

    topic = response.choices[0].message.content.strip()
    topic = topic.lstrip("- •123456789.").strip()

    blocked = contains_blocked_territory(topic)
    if blocked:
        logger.warning("TERRITORY BLOCK in topic: '%s' contains '%s'", topic[:80], blocked)
        raise BlockedTerritoryError(blocked)

    logger.info("Generated unique topic [%s/%s]: %s", content_type, direction, topic[:80])
    return topic


# ---------------------------------------------------------------------------
# Image prompt generation
# ---------------------------------------------------------------------------

async def generate_image_prompt(post_text: str) -> str:
    """Generate a DALL-E prompt from post text."""
    client = get_client()
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Generate a short DALL-E image prompt (max 200 chars) that would "
                    "make a good social media image for this post. "
                    "The image should be modern, clean, professional. "
                    "Return ONLY the prompt, nothing else."
                ),
            },
            {"role": "user", "content": post_text[:1000]},
        ],
        max_tokens=100,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()
