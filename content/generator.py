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


SYSTEM_PROMPT_FEATURE = """You are a social-media content manager for "{app_name}".
Website: {app_website}

{product_knowledge}

=== ЗАВДАННЯ ===
Створи короткий пост про КОНКРЕТНУ ФУНКЦІЮ додатку I'M IN.

=== СТИЛЬ ===
- Дружній, надихаючий, як друг-мандрівник
- Короткий (3-5 речень), яскравий
- 2-4 емодзі
- Конкретні деталі з документації (числа, параметри)
- Заклик: відвідай im-in.net

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. НЕ обіцяй конкретних дат запуску — лише "скоро" або "на фінішній прямій".
3. Ніколи не вигадуй статистику.
4. Хештеги тільки де платформа підтримує."""


SYSTEM_PROMPT_TOURISM_NEWS = """You are a social-media content manager for a travel app "{app_name}".
Website: {app_website}

=== ЗАВДАННЯ ===
Перепиши туристичну новину як КОРОТКИЙ АНОНС для соціальних мереж.

=== СТИЛЬ ===
- Анонс-формат: суть за 2-4 речення, не довга стаття
- Інформативно: що сталося, де, чому це важливо для туристів
- 2-3 емодзі
- ОБОВЯЗКОВО вкажи джерело та посилання в кінці поста

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. Не додавай рекламу додатку — це чисто новинний пост.
3. Зберігай оригінальне посилання на джерело.
4. Хештеги тільки де платформа підтримує.
5. Не вигадуй фактів — перекажи тільки те що є в оригіналі."""


SYSTEM_PROMPT_ACTIVE_TRAVEL = """You are a social-media content manager for a travel app "{app_name}".
Website: {app_website}

=== ЗАВДАННЯ ===
Створи короткий захоплюючий пост про СПОРТИВНЕ/АКТИВНЕ місце для подорожей.

=== СТИЛЬ ===
- Короткий анонс: 3-5 речень
- Енергійний, надихаючий для спортсменів та активних мандрівників
- 2-4 емодзі
- Факти: де знаходиться, чим відоме, коли кращий час для відвідування
- В кінці: ненавʼязливо згадай im-in.net

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. Не вигадуй факти — пиши тільки перевірену інформацію.
3. Хештеги тільки де платформа підтримує."""


SYSTEM_PROMPT_LEISURE_TRAVEL = """You are a social-media content manager for a travel app "{app_name}".
Website: {app_website}

=== ЗАВДАННЯ ===
Створи короткий атмосферний пост про КРАСИВЕ місце для подорожей (прогулянки, ресторани, музеї, природа, країни, міста).

=== СТИЛЬ ===
- Короткий: 3-5 речень
- Романтичний, мрійливий, створює бажання поїхати
- 2-4 емодзі
- Факти: де знаходиться, що подивитися, чим особливе
- В кінці: ненавʼязливо згадай im-in.net

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. Не вигадуй факти.
3. Хештеги тільки де платформа підтримує."""


CONTENT_TYPE_PROMPTS = {
    "feature": SYSTEM_PROMPT_FEATURE,
    "tourism_news": SYSTEM_PROMPT_TOURISM_NEWS,
    "active_travel": SYSTEM_PROMPT_ACTIVE_TRAVEL,
    "leisure_travel": SYSTEM_PROMPT_LEISURE_TRAVEL,
}


async def generate_post_text(
    topic: str,
    platform: Platform,
    *,
    source_text: str = "",
    content_type: str = "feature",
) -> str:
    """Generate a platform-adapted post text using OpenAI.

    content_type: feature | tourism_news | active_travel | leisure_travel
    """
    client = _get_client()
    limits = PLATFORM_LIMITS[platform]

    user_prompt_parts = [f"Platform: {platform.value} (max {limits['max_text_length']} chars)"]
    if limits["hashtags"]:
        user_prompt_parts.append("Include 3-5 relevant hashtags.")
    if not limits["supports_links"]:
        user_prompt_parts.append("Do NOT include links (platform does not support clickable links).")

    if source_text:
        user_prompt_parts.append(f"\nSource material to rewrite:\n{source_text[:2000]}")
    if topic:
        user_prompt_parts.append(f"\nTopic to write about:\n{topic}")

    user_prompt_parts.append("\nGenerate one post. Return ONLY the post text, nothing else.")

    prompt_template = CONTENT_TYPE_PROMPTS.get(content_type, SYSTEM_PROMPT_FEATURE)
    format_kwargs = {"app_name": settings.app_name, "app_website": settings.app_website}
    if "{product_knowledge}" in prompt_template:
        format_kwargs["product_knowledge"] = PRODUCT_KNOWLEDGE
    system = prompt_template.format(**format_kwargs)

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
