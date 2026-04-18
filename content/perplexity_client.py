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


RESEARCH_SYSTEM_PROMPT = """You are a travel researcher. Given a place name and location, 
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
5. Include specific details: dates, names, numbers when available"""


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

    user_prompt = f"Research this place: {location_str}"
    if point_type:
        user_prompt += f"\nType: {point_type.replace('_', ' ')}"
    if extra_context:
        user_prompt += f"\nAdditional context: {extra_context}"
    user_prompt += "\n\nFind real facts about this place from the internet. Return JSON."

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
