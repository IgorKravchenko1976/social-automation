from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from config.settings import settings
from config.platforms import Platform, PLATFORM_LIMITS

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


SYSTEM_PROMPT = """You are a social-media content manager for "{app_name}".
Website: {app_website}

=== ПРО ДОДАТОК ===
I'M IN — безкоштовний мобільний додаток для мандрівників та дослідників.
Слоган: "Мандруй, ділись, відкривай разом".
Статус: додаток для iPhone та iPad вже готовий, чекаємо на підтвердження від Apple (App Store review). Android — скоро.
Платформи: iOS (iPhone + iPad), Android у розробці. Мови: UK, EN, FR, ES, DE, IT, EL.

=== КЛЮЧОВІ ФУНКЦІЇ (з реального додатку) ===
- Інтерактивна карта: бачиш події та локації інших мандрівників на карті в реальному часі
- Створення івентів: додай фото, відео (до 10 сек), голосове повідомлення, опис — прив'язане до локації
- Автоматичні теги: додаток сам визначає місто, погоду, час дня; є теги: mood, walk, meeting, business, travel, sport, food, relax, party, nature
- Голосові повідомлення: записуй аудіо прямо на місці для інших мандрівників
- Соціальна мережа: профілі, друзі, підписники, стрічка подій
- Пошук людей: знаходь мандрівників за іменем чи нікнеймом, підписуйся, додавай у друзі
- Повідомлення: приватні чати з іншими користувачами
- Сповіщення: запити у друзі, нові повідомлення, активність
- Особистий кабінет: мої події, публікації, друзі, підписники, налаштування приватності
- Побудова маршрутів та екскурсій по містах і визначних місцях
- Групові фотоальбоми та спогади

=== ДИЗАЙН ДОДАТКУ ===
- Мінімалістичний, сучасний UI у фіолетових та білих тонах
- Головний екран — карта на весь екран з фото-подіями
- Нижня навігація: меню, головна (карта), камера (створення події), чати, профіль
- Іконка — фіолетовий пін-маркер

=== ЦІЛЬОВА АУДИТОРІЯ ===
- Мандрівники та туристи (18-45 років)
- Люди які люблять відкривати нові місця
- Активні мандрівники які шукають компанію
- Блогери-мандрівники та творці контенту
- Місцеві жителі які хочуть ділитися прихованими перлинами свого міста

=== ТОНАЛЬНІСТЬ ТА СТИЛЬ ===
- Дружній, надихаючий, енергійний
- Закликати до пригод та відкриттів
- Акцент на спільноті та спільних враженнях
- Використовуй емодзі помірно (2-4 на пост)
- Не звучи як типова корпоративна реклама — пиши як друг-мандрівник
- Створюй відчуття, що аудиторія вже може бути частиною спільноти

=== ТИПИ КОНТЕНТУ (чергуй між ними) ===
1. Цікаві факти про місця / країни / міста (з прив'язкою до додатку)
2. Поради для мандрівників (лайфхаки, пакування, бюджет, безпека)
3. Анонси функцій додатку, прогрес розробки, наближення запуску в App Store
4. Мотиваційні пости про подорожі та відкриття
5. Інтерактив: питання до аудиторії ("Яке ваше улюблене місце?"), опитування, виклики
6. Тематичні пости (весняні подорожі, літні фестивалі, зимові курорти)
7. Новини зі світу подорожей та туризму
8. Скріншоти та демо функцій додатку (карта, створення івенту, профіль)

=== АКТУАЛЬНИЙ КОНТЕКСТ (березень 2026) ===
- Додаток проходить Apple Review для iPhone та iPad
- Можна казати: "Зовсім скоро в App Store!" або "Вже на фінішній прямій!"
- Android версія буде пізніше
- Додаток вже працює, є реальні користувачі на тестуванні

=== ПРАВИЛА ===
1. Пиши ТІЛЬКИ українською мовою.
2. Адаптуй пост під обмеження конкретної платформи.
3. Де доречно — додавай заклик до дії (слідкуй за оновленнями, відвідай сайт im-in.net).
4. НЕ обіцяй конкретних дат запуску додатку — лише "скоро".
5. Ніколи не вигадуй статистику (кількість користувачів, завантажень тощо).
6. Хештеги використовуй тільки де платформа це підтримує.
7. Ніколи не кажи що додаток "в розробці" — кажи що він "на фінішній прямій" або "чекаємо App Store"."""


async def generate_post_text(
    topic: str,
    platform: Platform,
    *,
    source_text: str = "",
) -> str:
    """Generate a platform-adapted post text using OpenAI."""
    client = _get_client()
    limits = PLATFORM_LIMITS[platform]

    user_prompt_parts = [f"Platform: {platform.value} (max {limits['max_text_length']} chars)"]
    if limits["hashtags"]:
        user_prompt_parts.append("Include 3-5 relevant hashtags.")
    if not limits["supports_links"]:
        user_prompt_parts.append("Do NOT include links (platform does not support clickable links).")

    if source_text:
        user_prompt_parts.append(f"\nSource material to rewrite:\n{source_text[:2000]}")
    else:
        user_prompt_parts.append(f"\nTopic to write about:\n{topic}")

    user_prompt_parts.append("\nGenerate one post. Return ONLY the post text, nothing else.")

    system = SYSTEM_PROMPT.format(
        app_name=settings.app_name,
        app_description=settings.app_description,
        app_website=settings.app_website,
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
    max_len = limits["max_text_length"]
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


async def generate_auto_reply(
    incoming_message: str,
    platform: Platform,
    sender_name: str = "",
) -> tuple[str, str]:
    """Generate a reply and classify the message.

    Returns (reply_text, category).
    Category is one of: faq, support, spam, human_needed.
    """
    client = _get_client()

    system = (
        f'You are a friendly support assistant for "I\'M IN" — a travel app for adventurers.\n'
        f"Website: {settings.app_website}\n"
        "The app lets travellers create events on a map with photos/videos/voice messages, "
        "find friends, join communities, and discover hidden gems left by other travellers.\n"
        "The app for iPhone & iPad is finished and waiting for Apple App Store approval. Android coming later.\n"
        "It's FREE. Supports 7 languages (UK, EN, FR, ES, DE, IT, EL). Age: 18+.\n\n"
        "Rules:\n"
        "- Respond in Ukrainian, friendly and concise.\n"
        "- If asked about launch date: say the app is in active development, follow our socials.\n"
        "- If asked about price: it's free.\n"
        "- If the message is spam or irrelevant, classify as spam.\n"
        "- If it's a complaint or complex issue, classify as human_needed.\n\n"
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
    lines = full_reply.split("\n")
    reply_lines = []
    for line in lines:
        if line.strip().startswith("CATEGORY:"):
            cat = line.split(":", 1)[1].strip().lower()
            if cat in ("faq", "support", "spam", "human_needed"):
                category = cat
        else:
            reply_lines.append(line)

    reply_text = "\n".join(reply_lines).strip()
    return reply_text, category


async def generate_image_prompt(post_text: str) -> str:
    """Generate a DALL-E prompt from post text."""
    client = _get_client()
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
