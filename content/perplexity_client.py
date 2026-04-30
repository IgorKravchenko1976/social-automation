"""Perplexity Sonar API client — web search with citations.

Uses the OpenAI-compatible API with a different base URL and model name.
Returns structured research with real source URLs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from config.settings import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_perplexity_client() -> AsyncOpenAI | None:
    global _client
    if not settings.perplexity_api_key:
        return None
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.perplexity_api_key,
            base_url="https://api.perplexity.ai",
        )
    return _client


def is_configured() -> bool:
    return bool(settings.perplexity_api_key)


@dataclass
class PerplexityResult:
    content: str = ""
    citations: list[str] = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)


RESEARCH_SYSTEM_PROMPT = """You are a travel researcher. Given a SPECIFIC VENUE/ESTABLISHMENT and its location,
research it thoroughly using web search. Return ONLY valid JSON with this structure:
{
  "summary": "2-3 sentence overview of the place",
  "history": "Historical background (if known)",
  "cuisine_info": "About the cuisine/food (if restaurant/cafe)",
  "person_info": "About the person the place is named after (if applicable)",
  "cultural_context": "Cultural significance and local context",
  "practical_tips": "Tips for visitors",
  "nearby_attractions": "What else is interesting nearby",
  "fun_facts": "Interesting facts"
}

Rules:
1. ONLY use verified information from your search results
2. If you don't find information for a field, set it to empty string ""
3. Write in Ukrainian language
4. Never invent facts - accuracy is critical
5. Include specific details: dates, names, numbers when available

CRITICAL — NAME vs CONTENT:
The place NAME is just a brand/label for a venue (bar, restaurant, gym, shop, monument, etc.).
You MUST write about THE VENUE ITSELF — its atmosphere, menu, service, location, reviews.
NEVER write a Wikipedia article about the literal meaning of the name.
Example: "Бар «Лось»" is a BAR in Paris — write about the bar, NOT about the moose animal.
Example: "Gym Hercules" is a GYM — write about the gym, NOT about the Greek demigod.
If you cannot find information about the specific venue, return all fields as empty strings.

SOURCES POLICY:
- Never cite or rely on Russian or Belarusian sources (.ru, .by, vk.com, ok.ru, yandex, mail.ru, ria.ru, tass, rbc.ru, rt.com, kp.ru, lenta.ru, rg.ru, sputniknews, etc.).
- If a venue is located in Russia, Belarus, or in Russian-occupied Ukrainian territory (Crimea, occupied parts of Donetsk/Luhansk/Kherson/Zaporizhzhia oblasts) — return all fields as empty strings."""


async def research_place(
    name: str,
    city: str = "",
    country: str = "",
    point_type: str = "",
    extra_context: str = "",
) -> PerplexityResult | None:
    """Research a place using Perplexity Sonar with web search.

    Returns PerplexityResult with content and citations, or None on failure.
    """
    client = get_perplexity_client()
    if client is None:
        return None

    location_parts = [name]
    if city:
        location_parts.append(city)
    if country:
        location_parts.append(country)
    location_str = ", ".join(location_parts)

    type_label = point_type.replace("_", " ") if point_type else "place"
    user_prompt = f"Research this specific {type_label}: \"{name}\" located in {', '.join(location_parts[1:]) or 'unknown location'}"
    user_prompt += f"\n\nIMPORTANT: \"{name}\" is the NAME of a {type_label}. Research the venue itself, NOT the literal meaning of the word \"{name}\"."
    if extra_context:
        user_prompt += f"\nAdditional context: {extra_context}"
    user_prompt += "\n\nFind real facts about this specific venue/establishment from the internet. Return JSON."

    try:
        response = await client.chat.completions.create(
            model="sonar",
            messages=[
                {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.3,
        )

        content = response.choices[0].message.content.strip()
        citations = getattr(response, "citations", []) or []

        raw = {}
        try:
            raw = response.model_dump()
        except Exception:
            pass

        return PerplexityResult(
            content=content,
            citations=citations if isinstance(citations, list) else [],
            raw_response=raw,
        )

    except Exception as e:
        logger.warning("[perplexity] research failed for %s: %s", name, e)
        return None


def parse_research_json(result: PerplexityResult) -> dict | None:
    """Parse the JSON content from a Perplexity response."""
    content = result.content
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("[perplexity] failed to parse JSON response: %s", content[:200])
        return None
