from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from config.settings import settings
from config.platforms import Platform, PLATFORM_LIMITS
from content.product_knowledge import PRODUCT_KNOWLEDGE

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


SYSTEM_PROMPT = """You are a social-media content manager for "{app_name}".
Website: {app_website}

{product_knowledge}

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
1. Презентація конкретної функції додатку (карта, створення події, голосові, авто-режим, 3D, чат, приватність тощо)
2. Цікаві факти про місця / країни / міста (з прив'язкою до додатку)
3. Поради для мандрівників (лайфхаки, пакування, бюджет, безпека)
4. Мотиваційні пости про подорожі та відкриття
5. Інтерактив: питання до аудиторії, опитування, виклики
6. Тематичні пости (весняні подорожі, літні фестивалі, зимові курорти)
7. Новини зі світу подорожей та туризму
8. Сценарії використання: покрокові історії як мандрівник використовує додаток (наприклад: приїхав у нове місто → відкрив карту → побачив події поруч → познайомився з людьми)

=== ПРАВИЛА ===
1. Пиши ТІЛЬКИ українською мовою.
2. Адаптуй пост під обмеження конкретної платформи.
3. Де доречно — додавай заклик до дії (слідкуй за оновленнями, відвідай сайт im-in.net).
4. НЕ обіцяй конкретних дат запуску додатку — лише "скоро".
5. Ніколи не вигадуй статистику (кількість користувачів, завантажень тощо).
6. Хештеги використовуй тільки де платформа це підтримує.
7. Ніколи не кажи що додаток "в розробці" — кажи що він "на фінішній прямій" або "чекаємо App Store".
8. При описі функцій — використовуй РЕАЛЬНІ деталі з документації (конкретні числа, параметри, можливості)."""


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
        app_website=settings.app_website,
        product_knowledge=PRODUCT_KNOWLEDGE,
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
        f"Website: {settings.app_website}\n\n"
        f"{PRODUCT_KNOWLEDGE}\n\n"
        "=== ПРАВИЛА ВІДПОВІДІ ===\n"
        "- ВИЗНАЧИ мову повідомлення користувача і ВІДПОВІДАЙ ТІЄЮ Ж МОВОЮ. "
        "Наприклад: якщо пишуть англійською — відповідай англійською, "
        "французькою — французькою, українською — українською, і так далі.\n"
        "- Відповідай дружньо та лаконічно.\n"
        "- Використовуй КОНКРЕТНІ деталі з документації вище для відповідей.\n"
        "- На питання про функції — описуй реальні можливості додатку з деталями.\n"
        "- На питання про реєстрацію — поясни два кроки: email + код підтвердження, потім пароль.\n"
        "- На питання про біометрію — так, підтримуємо Face ID, відбиток пальця, скан райдужної.\n"
        "- На питання про карту — розкажи про 2D/3D, маркери подій, пошук адрес.\n"
        "- На питання про приватність — поясни три режими: приватний, для друзів, публічний.\n"
        "- На питання про ціну — безкоштовно.\n"
        "- На питання про дату запуску — скажи що додаток на фінішній прямій, слідкуй за оновленнями.\n"
        "- На питання про Android — скажи що Android версія буде трохи пізніше.\n"
        "- На питання про видалення акаунту — поясни процес через Кабінет.\n"
        "- Якщо повідомлення — спам або нерелевантне, класифікуй як spam.\n"
        "- Якщо скарга або складне питання, класифікуй як human_needed.\n"
        "- Не вигадуй інформацію якої немає в документації.\n\n"
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
