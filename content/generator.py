from __future__ import annotations

import json
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
Створи короткий пост про КОНКРЕТНУ ФУНКЦІЮ додатку I'M IN,
прив'язавши її до РЕАЛЬНОЇ ПОДОРОЖНЬОЇ СИТУАЦІЇ з теми поста.

Наприклад:
- Тема "Тенісний турнір у Парижі" → покажи як на карті I'M IN знайти події поруч з Ролан Гаррос
- Тема "Львівські кав'ярні" → покажи як зберігати улюблені місця та ділитись з друзями
- Тема "Серфінг на Балі" → покажи як створити подію з фото/відео прямо на пляжі

=== СТИЛЬ ===
- Дружній, надихаючий, як друг-мандрівник
- Короткий (3-5 речень), яскравий
- Спочатку — подорожня ситуація (1 речення), потім — як I'M IN допомагає (2-3 речення)
- 2-4 емодзі
- Конкретні деталі з документації (числа, параметри)
- Заклик: відвідай www.im-in.net

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. НЕ обіцяй конкретних дат запуску — лише "скоро" або "на фінішній прямій".
3. Ніколи не вигадуй статистику.
4. Хештеги тільки де платформа підтримує.
5. Виведи ТІЛЬКИ текст посту. Жодних пояснень, вибачень, коментарів."""


SYSTEM_PROMPT_TOURISM_NEWS = """You are a social-media content manager for a travel app "{app_name}".
Website: {app_website}

=== ЗАВДАННЯ ===
Перепиши туристичну новину як КОРОТКИЙ АНОНС для соціальних мереж.

=== КРИТИЧНО: ДАТА ТА СВІЖІСТЬ ===
КОЖЕН пост ОБОВ'ЯЗКОВО починається з ДАТИ новини у форматі:
"📅 [число] [місяць] [рік]" — це дата ОРИГІНАЛЬНОЇ публікації новини.
Якщо дата не зрозуміла з оригіналу — пиши "📅 березень 2026" або місяць/рік.
НІКОЛИ не публікуй старі новини як нові. Якщо новина старша за 2 дні — ВІДМОВСЯ.
НІКОЛИ не пиши дати з минулих років (2023, 2024, 2025) — тільки актуальні.
Зараз 2026 рік. Якщо в оригіналі вказана стара дата — це стара новина, ІГНОРУЙ її.

=== ГОЛОВНЕ ПРАВИЛО: ДЖЕРЕЛО ТА ФАКТИ ===
КОЖНА новина ОБОВ'ЯЗКОВО повинна містити:
1. КОНКРЕТНІ факти: ХТО прийняв рішення, ЩО саме змінилось, КОЛИ, ДЛЯ КОГО.
   НЕ пиши абстрактно "країна послаблює правила" — пиши конкретно ЩО саме змінилось.
   Приклад ПОГАНОГО посту: "Італія послаблює правила в'їзду для іноземців"
   Приклад ХОРОШОГО посту: "📅 22 березня 2026 — За даними CNN Travel, Італія скасовує вимогу
   заповнювати форму PLF для туристів з країн, що не входять до ЄС."
2. ДЖЕРЕЛО в кінці: "📰 Джерело: [назва медіа]" + посилання (якщо є в оригіналі).
3. Якщо з оригіналу не зрозуміло ХТО/ЩО/КОЛИ — НЕ додавай від себе, а напиши
   тільки те, що точно відомо. Краще менше інформації, але правдивої.
4. НІКОЛИ не роби заяви від імені сторінки як факт — завжди "за даними [джерело]".

=== ПРІОРИТЕТ — УКРАЇНА ===
Якщо є можливість — ЗАВЖДИ обирай або згадуй Україну:
- Туристичні місця України: Карпати, Одеса, Львів, Київ, Закарпаття, Кам'янець-Подільський,
  Чернівці, Буковель, Шацькі озера, Асканія-Нова, Хортиця, Тунель кохання тощо
- Гастротуризм: українська кухня, фестивалі вина, сиру, меду, кави у Львові
- Активний відпочинок в Україні: рафтинг на Черемоші, трекінг у Карпатах, велотури
- Культурні події: фестивалі, ярмарки, концерти, виставки в українських містах
- Сезонні можливості: лижний сезон Буковель/Драгобрат, пляжний сезон Одеса/Затока
- НЕ згадуй війну, окупацію, фронт, обстріли — ТІЛЬКИ позитивний туристичний контент про Україну

=== ТЕМАТИКА (тільки про це!) ===
- Застереження для мандрівників (закриті перевали, стихійні лиха, небезпечні маршрути)
- Можливості для туристів (нові маршрути, знижки, безвіз, нові рейси)
- Аеропорти (нові термінали, затримки рейсів, страйки, нові авіалінії)
- Закриті/відкриті перевали та дороги
- Відкладені або скасовані рейси
- Пригоди та події на туристичних маршрутах
- Туристичні сезони (початок/кінець сезону, погода, найкращий час)
- Круїзи, потяги, автобуси — новини транспорту для туристів
- Візові зміни, нові правила в'їзду в країни
- Фестивалі, карнавали, культурні події для туристів
- Ціни на подорожі, тренди бюджетного туризму

=== ЗАБОРОНЕНО ===
- НІЯКОЇ ПОЛІТИКИ — ні війни, ні санкції, ні вибори, ні дипломатичні конфлікти
- Ніяких політичних лідерів, урядових рішень що не стосуються туризму
- ТАБУ на Росію: ніяких новин про Росію, російські міста, російські авіалінії,
  російські курорти, російських туристів. Повне ігнорування — не згадувати взагалі.
- Ніяких російських медіа як джерело.
- Якщо новина має політичний контекст — ігноруй її повністю або перепиши
  ТІЛЬКИ туристичну частину (наприклад: "рейси скасовані" — ОК, причину-політику не згадуй)

=== СТИЛЬ ===
- Анонс-формат: суть за 2-4 речення, не довга стаття
- Корисно для мандрівників: що це значить для них практично
- 2-3 емодзі
- ОБОВЯЗКОВО вкажи "📰 Джерело: [назва]" + посилання в кінці поста

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. Не додавай рекламу додатку — це чисто новинний пост.
3. Зберігай оригінальне посилання на джерело.
4. Хештеги тільки де платформа підтримує.
5. Не вигадуй фактів — перекажи тільки те що є в оригіналі.
6. Якщо вся новина — чисто політична без туристичного контексту, напиши натомість
   цікаву туристичну пораду або факт про красиве місце для подорожей.
7. КРИТИЧНО: НІКОЛИ не пиши "Вибачте", "Я не можу", "Ця новина", "На жаль" —
   ти пишеш ГОТОВИЙ ПОСТ для соцмереж, а не відповідь на запитання.
   Виведи ТІЛЬКИ текст посту, нічого більше. Жодних пояснень, коментарів, вибачень."""


SYSTEM_PROMPT_ACTIVE_TRAVEL = """You are a social-media content manager for a travel app "{app_name}".
Website: {app_website}

=== ЗАВДАННЯ ===
Створи короткий захоплюючий пост про СПОРТИВНЕ/АКТИВНЕ місце або подію для мандрівників.

=== ГОЛОВНИЙ ПРИНЦИП: МІСЦЕ + ПОДІЯ + КОРИСТЬ ===
КОЖЕН пост ОБОВ'ЯЗКОВО прив'язаний до КОНКРЕТНОГО МІСЦЯ (місто, країна, локація).
Пост повинен бути КОРИСНИМ для мандрівника який планує поїздку:
- ДЕ це відбувається (місто, країна, конкретна локація)
- ЩО цікавого (змагання, школа, турнір, маршрут, курорт)
- КОЛИ найкращий час / сезон / дати подій
- ПРАКТИЧНІ поради: ціни на готелі під час подій, як дістатись, де зупинитись,
  чи варто орендувати авто, час на дорогу, лайфхаки для економії

=== АУДИТОРІЯ ===
Мандрівники які: купують квитки на літак/потяг, бронюють готелі, орендують авто.
Їм цікаво: результати змагань + ДЕ це було, поради де краще зупинитись,
ціни (вони ростуть під час великих подій!), альтернативні варіанти проживання.

=== КРИТИЧНО: ДАТА ТА АКТУАЛЬНІСТЬ ===
Якщо пост про ПОДІЮ (змагання, турнір, фестиваль) — ОБОВ'ЯЗКОВО вкажи дату:
"📅 [дата або період]". Тільки АКТУАЛЬНІ події (найближчі тижні/місяці).
НІКОЛИ не пиши про минулі події як поточні. Зараз 2026 рік.

=== СТИЛЬ ===
- Короткий анонс: 3-5 речень
- Енергійний, надихаючий, КОРИСНИЙ
- 2-4 емодзі
- Ми ІНФОРМУЄМО — читач сам приймає рішення. Не нав'язуй, а подавай факти.
- В кінці: ненавʼязливо згадай www.im-in.net

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. Пиши лише загальновідомі факти. НЕ вигадуй конкретних цифр якщо не впевнений.
3. Хештеги тільки де платформа підтримує.
4. Виведи ТІЛЬКИ текст посту. Жодних пояснень, вибачень, коментарів."""


SYSTEM_PROMPT_LEISURE_TRAVEL = """You are a social-media content manager for a travel app "{app_name}".
Website: {app_website}

=== ЗАВДАННЯ ===
Створи короткий атмосферний пост про КОНКРЕТНЕ МІСЦЕ для подорожей.
Це може бути: вулиця, ресторан, музей, парк, район міста, пам'ятка, фестиваль, ринок,
архітектурний об'єкт, гастро-заклад, концертна площадка, тощо.

=== ГОЛОВНИЙ ПРИНЦИП: МІСЦЕ + АТМОСФЕРА + КОРИСТЬ ===
КОЖЕН пост ОБОВ'ЯЗКОВО прив'язаний до КОНКРЕТНОГО МІСЦЯ (місто, вулиця, локація).
- ДЕ це (місто, країна, район, вулиця)
- ЩО там цікавого (атмосфера, враження, історія, кухня, архітектура)
- Чому варто поїхати / відвідати
- Якщо доречно: практичні поради (коли найкраще відвідати, що спробувати)

=== АУДИТОРІЯ ===
Мандрівники які обирають куди поїхати, бронюють готелі, купують квитки.
Пост повинен надихати І бути корисним. Не просто "красиво" — а "красиво + як туди потрапити".

=== КРИТИЧНО: АКТУАЛЬНІСТЬ ===
НІКОЛИ не згадуй минулі події як актуальні. Зараз 2026 рік.
Якщо пост про фестиваль, виставку або подію — вкажи "📅 [дата/період]".
Загальні описи місць (без прив'язки до подій) — дата не обов'язкова.

=== СТИЛЬ ===
- Короткий: 3-5 речень
- Романтичний, мрійливий, створює бажання поїхати
- Описовий стиль: атмосфера, краса, враження — пиши як мандрівник який ділиться
- Загальновідомі факти ОК (столиця, пам'ятка, історична дата, відомий ресторан)
- 2-4 емодзі
- Ми ІНФОРМУЄМО — читач сам приймає рішення
- В кінці: ненавʼязливо згадай www.im-in.net

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. НЕ вигадуй конкретних цифр (ціни, відстані) якщо не впевнений.
   Загальновідомі історичні факти — ОК.
3. Хештеги тільки де платформа підтримує.
4. Виведи ТІЛЬКИ текст посту. Жодних пояснень, вибачень, коментарів."""


CONTENT_TYPE_PROMPTS = {
    "feature": SYSTEM_PROMPT_FEATURE,
    "tourism_news": SYSTEM_PROMPT_TOURISM_NEWS,
    "active_travel": SYSTEM_PROMPT_ACTIVE_TRAVEL,
    "leisure_travel": SYSTEM_PROMPT_LEISURE_TRAVEL,
}


import re

_META_PATTERNS = [
    r"(?i)^вибач(те|)[\s,.:!—–-].*?\n+",
    r"(?i)^на жаль[\s,.:!—–-].*?\n+",
    r"(?i)^я не (можу|зможу)[\s,.:!—–-].*?\n+",
    r"(?i)^ця новина[\s,.:!—–-].*?\n+",
    r"(?i)^цей запит[\s,.:!—–-].*?\n+",
    r"(?i)^і cannot[\s,.:!—–-].*?\n+",
    r"(?i)^sorry[\s,.:!—–-].*?\n+",
    r"(?i)^unfortunately[\s,.:!—–-].*?\n+",
    r"(?i)^натомість[\s,.:!—–-].*?\n+",
    r"(?i)^замість цього[\s,.:!—–-].*?\n+",
    r"(?i)^оскільки новина.*?\n+",
]


def _clean_ai_meta(text: str) -> str:
    """Strip AI apologies, refusals, and meta-commentary from generated posts."""
    cleaned = text
    for pattern in _META_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else text


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

    from config.settings import get_now_local
    today_str = get_now_local().strftime("%d.%m.%Y")
    system += f"\n\nСЬОГОДНІШНЯ ДАТА: {today_str}. Публікуй тільки актуальну інформацію."

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
    text = _clean_ai_meta(text)
    max_len = limits["max_text_length"]
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


async def generate_auto_reply(
    incoming_message: str,
    platform: Platform,
    sender_name: str = "",
    post_context: str = "",
    prior_replies: int = 0,
) -> tuple[str, str]:
    """Generate a reply and classify the message.

    Args:
        post_context: The text of the original post this comment is about.
            When available, the reply will be relevant to the post topic.

    Returns (reply_text, category).
    Category is one of: faq, support, spam, human_needed.
    """
    client = _get_client()

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
        "- На питання про деталі — розкрий тему поста глибше, додай конкретику\n"
        "- На запити 'розкажи більше' — дай додаткові деталі про місце/подію з поста\n"
        "- Якщо коментар стосується місця з поста — розкажи додатково про нього\n"
        "- Якщо доречно — згадай що в додатку I'M IN можна знайти це місце на карті\n\n"
        "=== КРИТИЧНО: ПОСИЛАННЯ НА ДЖЕРЕЛО ===\n"
        "Коли пост містить НОВИНУ (є 'Джерело:', назва медіа, або посилання) — "
        "у відповіді ЗАВЖДИ давай посилання на ОРИГІНАЛЬНЕ ДЖЕРЕЛО звідки новина:\n"
        "  Приклад: 'Джерело цієї новини — Укрінформ Туризм: [посилання з поста]'\n"
        "  НІКОЛИ не пиши 'Деталі на сайті www.im-in.net' для новин — на нашому сайті "
        "цієї новини немає! Давай посилання на те медіа, звідки ми взяли новину.\n"
        "Якщо в пості є рядок 'Джерело:' або '📰 Джерело:' — витягни назву та посилання звідти.\n\n"
        "Посилання www.im-in.net давай ТІЛЬКИ коли мова про:\n"
        "  - Функціонал додатку I'M IN\n"
        "  - Загальні питання про додаток, реєстрацію, можливості\n"
        "  - Зв'язок з командою\n\n"
        "=== ПРАВИЛА ВІДПОВІДІ ===\n"
        "- ВИЗНАЧИ мову повідомлення і ВІДПОВІДАЙ ТІЄЮ Ж МОВОЮ.\n"
        "- Відповідай дружньо та ЗМІСТОВНО. Не просто 'дякую' — дай конкретну інформацію.\n"
        "- Коротко (2-4 речення). Не пиши стіну тексту під коментарем.\n"
        f"- Це відповідь №{prior_replies + 1} цьому автору в цьому треді.\n"
        "- ВІТАННЯ ('Привіт', 'Добрий день', 'Вітаємо' тощо) — ТІЛЬКИ в ПЕРШІЙ відповіді автору (відповідь №1).\n"
        "  Якщо це НЕ перша відповідь — НЕ вітайся, одразу переходь до суті.\n"
        "- На привітання — привітайся тепло, запитай чим можемо допомогти.\n"
        "- На спам — класифікуй як spam, дай коротку ввічливу відповідь.\n"
        "- На скарги або складні питання — класифікуй як human_needed.\n"
        "- Про додаток I'M IN — розкажи як він допомагає мандрівникам "
        "(карта подій, фото/відео з геолокацією, спілкування). "
        "Тут давай посилання www.im-in.net.\n"
        "- Про ціну додатку — безкоштовний.\n"
        "- Про дату запуску — скоро, слідкуйте за оновленнями.\n"
        "- Не вигадуй фактів яких немає в пості або документації.\n\n"
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


async def generate_unique_topic(
    direction: str,
    content_type: str,
    recent_titles: list[str],
    *,
    travel_context: str = "",
) -> str:
    """Ask AI to generate a specific unique topic within a broad direction.

    The AI receives the direction category and a list of recent post titles
    (last 60 days) so it avoids repetition. Topics are always tied to a
    specific place/location and prioritize fresh events.

    For feature posts, travel_context provides a real travel topic from today's
    posts so the feature can be tied to a practical travel situation.
    """
    client = _get_client()

    recent_block = ""
    if recent_titles:
        titles_text = "\n".join(f"- {t}" for t in recent_titles[-80:])
        recent_block = (
            f"\n\nОСЬ ТЕМИ ПОСТІВ ЗА ОСТАННІ 60 ДНІВ (НЕ ПОВТОРЮЙ ЇХ!):\n{titles_text}"
        )

    context_block = ""
    if travel_context and content_type == "feature":
        context_block = (
            f"\n\nСЬОГОДНІШНЯ ПОДОРОЖНЯ ТЕМА (прив'яжи функцію до неї): {travel_context}"
        )

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
        "або тим що відбувається ЗАРАЗ (березень 2026). "
        "НІКОЛИ не пропонуй теми з минулих років. Якщо згадуєш подію — вона має бути актуальна. "
        "Тема повинна бути унікальною і НЕ повторювати жодну з наведених минулих тем. "
        "Напрямок — це ШИРОКЕ поле з десятками можливих тем (одне місто = ресторани, "
        "музеї, вулиці, архітектура, події, фестивалі, історія, кухня тощо). "
        "Поверни ТІЛЬКИ тему (1-2 речення), без пояснень, нумерації чи коментарів."
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
    logger.info("Generated unique topic [%s/%s]: %s", content_type, direction, topic[:80])
    return topic


async def extract_location_coordinates(topic: str) -> dict | None:
    """Extract the main location from a topic and return its coordinates.

    Returns {"lat": float, "lon": float, "name": str} or None.
    Tries to find the most specific location (restaurant > city > country).
    """
    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the main geographic location from the text and return its coordinates. "
                        "Find the MOST SPECIFIC place mentioned: a specific restaurant/hotel/stadium "
                        "is better than a city, a city is better than a country. "
                        "Return ONLY valid JSON: {\"lat\": 48.8566, \"lon\": 2.3522, \"name\": \"Paris, France\"}\n"
                        "If no location can be determined, return: {\"lat\": null, \"lon\": null, \"name\": null}\n"
                        "No explanations, no markdown, ONLY the JSON object."
                    ),
                },
                {"role": "user", "content": topic[:500]},
            ],
            max_tokens=80,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)
        if data.get("lat") is not None and data.get("lon") is not None:
            logger.info("Geo for topic: %s → %s (%.4f, %.4f)",
                        topic[:60], data["name"], data["lat"], data["lon"])
            return data
    except Exception:
        logger.warning("Failed to extract coordinates for: %s", topic[:80])
    return None


def build_map_link(lat: float, lon: float, name: str = "") -> str:
    """Build a Google Maps link for given coordinates."""
    label = name.replace(" ", "+") if name else ""
    return f"https://maps.google.com/?q={lat},{lon}&label={label}" if label else f"https://maps.google.com/?q={lat},{lon}"


BLOG_LANGUAGES = ["uk", "en", "fr", "es", "de", "it", "el"]
LANG_NAMES = {
    "uk": "Ukrainian", "en": "English", "fr": "French",
    "es": "Spanish", "de": "German", "it": "Italian", "el": "Greek",
}


async def translate_post(title: str, content: str, source_lang: str = "uk") -> dict:
    """Translate post title and content to all website languages in one API call.

    Returns dict like {"en": {"title": "...", "content": "..."}, "fr": {...}, ...}
    The source language is excluded from the result.
    """
    target_langs = [l for l in BLOG_LANGUAGES if l != source_lang]
    if not target_langs:
        return {}

    lang_list = ", ".join(f"{code} ({LANG_NAMES[code]})" for code in target_langs)
    client = _get_client()

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a professional translator for a travel blog. "
                        "Translate the given title and content into each requested language. "
                        "Keep the meaning, tone, and emoji intact. Do not add or remove information. "
                        "Return ONLY valid JSON with language codes as keys.\n"
                        "Format: {\"en\": {\"title\": \"...\", \"content\": \"...\"}, "
                        "\"fr\": {\"title\": \"...\", \"content\": \"...\"}, ...}\n"
                        "No markdown, no explanations, ONLY the JSON object."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Translate to: {lang_list}\n\n"
                        f"Title: {title}\n\n"
                        f"Content:\n{content[:3000]}"
                    ),
                },
            ],
            max_tokens=4000,
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.strip("`").removeprefix("json").strip()
        translations = json.loads(raw)
        valid = {}
        for lang in target_langs:
            if lang in translations and isinstance(translations[lang], dict):
                valid[lang] = {
                    "title": translations[lang].get("title", title),
                    "content": translations[lang].get("content", content),
                }
        logger.info("Translated post to %d languages: %s", len(valid), list(valid.keys()))
        return valid
    except Exception:
        logger.warning("Translation failed — post will stay in original language", exc_info=True)
        return {}


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
