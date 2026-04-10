"""POI research processor — generates deep research content for enriched POI points.

Flow: fetch enriched+posted POI → AI generates research → translate → create event → link back.
"""
from __future__ import annotations

import asyncio
import json
import logging

from content.ai_client import get_client
from content.media import get_image_for_post, download_image_from_url, cleanup_media_file
from geo_agent import backend_client
from geo_agent.translator import translate_content

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

SYSTEM_PROMPT_POI_RESEARCH = """Ти — досвідчений дослідник-мандрівник, який створює глибокі дослідження про конкретні місця.

=== ЗАВДАННЯ ===
Тобі надані РЕАЛЬНІ, ПЕРЕВІРЕНІ дані про конкретне місце з бази даних.
Створи ГЛИБОКЕ ДОСЛІДЖЕННЯ цього місця, використовуючи ТІЛЬКИ надані факти.
Доповни своїми загальновідомими знаннями про регіон/місто/країну (якщо впевнений).

=== СТРУКТУРА ДОСЛІДЖЕННЯ ===
Створи JSON з такими полями:
{
  "summary": "Короткий опис місця (2-3 речення)",
  "location_name": "Назва місця",
  "country_code": "UA",
  "history": [
    {"period": "рік або період", "description": "що сталось"}
  ],
  "detailed_history": [
    {"period": "I ст. н.е.", "description": "..."},
    {"period": "V-IX ст.", "description": "..."},
    {"period": "X-XIII ст.", "description": "..."},
    {"period": "XIV-XVI ст.", "description": "..."},
    {"period": "XVII-XVIII ст.", "description": "..."},
    {"period": "XIX ст.", "description": "..."},
    {"period": "XX ст.", "description": "..."},
    {"period": "XXI ст.", "description": "..."}
  ],
  "places": [
    {"name": "назва поруч", "type": "тип (city/museum/park/church/...)", "description": "що цікавого"}
  ],
  "regions": [
    {"name": "Назва регіону/міста", "type": "region/city", "description": "коротко про регіон"}
  ],
  "news": [
    {"title": "Заголовок", "description": "Про що", "source": "джерело"}
  ],
  "practical_info": {
    "best_time": "найкращий час для відвідування",
    "how_to_get": "як дістатися",
    "budget": "приблизний бюджет",
    "tips": ["порада 1", "порада 2"]
  },
  "cultural_context": "Культурний та історичний контекст місця в регіоні",
  "nearby_attractions": ["Цікаве місце 1", "Цікаве місце 2"]
}

=== ПРАВИЛА ===
1. ТІЛЬКИ українською мовою.
2. НЕ вигадуй фактів — якщо не знаєш, не пиши.
3. Історія: тільки те що ТОЧНО відомо. Краще менше але правдиво.
4. detailed_history: детальна хронологія з нашої ери. Якщо це місто/країна — від античності до сучасності. Пропускай періоди яких не знаєш.
5. regions: якщо це країна — перерахуй основні регіони/області. Якщо місто — основні райони. Якщо регіон — основні міста.
6. news: якщо знаєш актуальні туристичні новини (фестивалі, відкриття, події) — додай. Якщо не знаєш — пусте [].
7. places.type: обов'язково вкажи тип (city, region, island, museum, park, church, castle, beach тощо).
8. Практична інфо: на основі наданих даних + загальновідомі факти.
9. Поверни ТІЛЬКИ валідний JSON, нічого більше.

=== ЗАБОРОНЕНО ===
Росія, Білорусь, окуповані території, небезпечні зони.
Якщо місце в забороненій зоні — поверни {"_rejected": true, "_reject_reason": "blocked territory"}."""


def _format_poi_for_research(poi: backend_client.POIResearchTask) -> str:
    """Format all POI data for the AI researcher."""
    lines = [
        "=== ДАНІ ПРО МІСЦЕ ===",
        f"Назва: {poi.name}",
        f"Тип: {poi.point_type.replace('_', ' ').title()}",
        f"Місто: {poi.city}" if poi.city else "",
        f"Країна: {poi.country_code.upper()}" if poi.country_code else "",
        f"Координати: {poi.latitude:.6f}, {poi.longitude:.6f}",
    ]

    if poi.address:
        lines.append(f"Адреса: {poi.address}")
    if poi.phone:
        lines.append(f"Телефон: {poi.phone}")
    if poi.opening_hours:
        lines.append(f"Години роботи: {poi.opening_hours}")
    if poi.cuisine:
        lines.append(f"Кухня: {poi.cuisine}")
    if poi.website:
        lines.append(f"Вебсайт: {poi.website}")
    if poi.operator_name:
        lines.append(f"Оператор: {poi.operator_name}")
    if poi.founded_year and poi.founded_year > 0:
        lines.append(f"Рік заснування: {poi.founded_year}")
    if poi.rating and poi.rating > 0:
        lines.append(f"Рейтинг: {poi.rating:.1f}")
    if poi.description:
        desc = poi.description[:1000]
        lines.append(f"\nОпис (Wikipedia): {desc}")
    if poi.wikipedia_url:
        lines.append(f"Wikipedia: {poi.wikipedia_url}")

    lines.append("=== КІНЕЦЬ ДАНИХ ===")
    return "\n".join(l for l in lines if l)


async def _generate_poi_research(poi: backend_client.POIResearchTask) -> dict | None:
    """Generate deep research about a POI using AI."""
    client = get_client()
    poi_text = _format_poi_for_research(poi)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_POI_RESEARCH},
                {"role": "user", "content": poi_text},
            ],
            max_tokens=3000,
            temperature=0.6,
        )
        raw = response.choices[0].message.content.strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)
        if result.get("_rejected"):
            logger.warning("[poi-researcher] AI rejected POI %d: %s", poi.point_id, result.get("_reject_reason"))
            return None

        result["location_name"] = result.get("location_name") or poi.name
        return result

    except (json.JSONDecodeError, Exception) as e:
        logger.error("[poi-researcher] AI research failed for POI %d: %s", poi.point_id, e)
        return None


async def process_poi_research() -> bool:
    """Process one POI for research. Called by scheduler.

    Returns True if a POI was processed, False otherwise.
    """
    async with _lock:
        return await _process_poi_research_inner()


async def _process_poi_research_inner() -> bool:
    if not backend_client.is_configured():
        return False

    try:
        poi = await backend_client.fetch_next_poi_for_research()
    except Exception as e:
        logger.warning("[poi-researcher] Failed to fetch next POI: %s", e)
        return False

    if poi is None:
        logger.debug("[poi-researcher] No POI available for research")
        return False

    logger.info("[poi-researcher] Researching POI %d: %s (%s, %s)",
                poi.point_id, poi.name, poi.city, poi.country_code)

    result = await _generate_poi_research(poi)
    if result is None:
        await backend_client.mark_poi_researched(poi.point_id, 0)
        return True

    content = json.dumps(result, ensure_ascii=False)
    summary = result.get("summary", "")
    location_name = result.get("location_name", poi.name)

    source_lang = "uk"
    try:
        translations = await translate_content(
            location_name[:200],
            summary[:2000],
            source_lang=source_lang,
        )
    except Exception as e:
        logger.warning("[poi-researcher] Translation failed for POI %d: %s", poi.point_id, e)
        translations = {source_lang: {"title": location_name, "description": summary}}

    title = location_name or poi.name
    parts = [summary] if summary else []

    history_list = result.get("history", [])
    if history_list and isinstance(history_list, list):
        history_lines = [f"• {h.get('period', '')}: {h.get('description', '')}" for h in history_list[:5]]
        parts.append("📜 Історія\n" + "\n".join(history_lines))

    places_list = result.get("places", [])
    if places_list and isinstance(places_list, list):
        place_lines = [f"• {p.get('name', '')} — {p.get('description', '')}" for p in places_list[:5]]
        parts.append("📍 Цікаві місця поруч\n" + "\n".join(place_lines))

    practical = result.get("practical_info", {})
    if practical:
        pi_lines = []
        if practical.get("best_time"):
            pi_lines.append(f"🕐 Найкращий час: {practical['best_time']}")
        if practical.get("how_to_get"):
            pi_lines.append(f"🚗 Як дістатися: {practical['how_to_get']}")
        if practical.get("budget"):
            pi_lines.append(f"💰 Бюджет: {practical['budget']}")
        tips = practical.get("tips", [])
        if tips:
            pi_lines.extend(f"💡 {t}" for t in tips[:3])
        if pi_lines:
            parts.append("ℹ️ Практична інформація\n" + "\n".join(pi_lines))

    cultural = result.get("cultural_context", "")
    if cultural:
        parts.append(f"🏛️ Культурний контекст\n{cultural}")

    description = "\n\n".join(parts) if parts else summary

    image_path = None
    if poi.image_url:
        image_path = await download_image_from_url(poi.image_url)
        if image_path:
            logger.info("[poi-researcher] Real photo for POI %d: %s", poi.point_id, poi.image_url[:80])
    if not image_path:
        image_query = f"{poi.name} {poi.city} travel" if poi.city else f"{poi.name} travel"
        image_path = await get_image_for_post(
            image_query, use_dalle=False, prefer_dalle=False,
        )
    if not image_path:
        dalle_prompt = (
            f"Photorealistic travel photography of {poi.name}, {poi.city} {poi.country_code}. "
            f"Beautiful scenery, professional travel magazine style."
        )
        image_path = await get_image_for_post(
            f"{poi.name} landmark", use_dalle=True, prefer_dalle=True, dalle_prompt=dalle_prompt,
        )

    research_code = f"poi_{poi.point_id}_{poi.name[:20].replace(' ', '_')}"

    try:
        resp = await backend_client.create_research_event(
            research_code=research_code,
            title=title[:200],
            description=description[:4000],
            latitude=poi.latitude,
            longitude=poi.longitude,
            photo_path=image_path,
            content_language=source_lang,
            translations=translations,
        )

        cleanup_media_file(image_path)

        event_id = resp.get("eventId", 0)
        if resp.get("ok") and event_id:
            await backend_client.mark_poi_researched(poi.point_id, event_id)
            logger.info("[poi-researcher] POI %d researched → event %d", poi.point_id, event_id)
        else:
            await backend_client.mark_poi_researched(poi.point_id, 0)
            logger.warning("[poi-researcher] Event creation response: %s", resp)

    except Exception as e:
        cleanup_media_file(image_path)
        await backend_client.mark_poi_researched(poi.point_id, 0)
        logger.error("[poi-researcher] Event creation failed for POI %d: %s", poi.point_id, e)

    return True
