"""Multi-language translator for research event content.

Uses GPT-4o-mini to translate title+description into all supported languages
in a single API call. Returns a dict suitable for JSONB storage.

Supported languages: uk, en, ru, de, fr, es, it, el
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from content.ai_client import get_client

logger = logging.getLogger(__name__)

TARGET_LANGUAGES = ["uk", "en", "ru", "de", "fr", "es", "it", "el"]

TRANSLATE_PROMPT = """You are a professional translator for a travel app.
Translate the following title and description into ALL of these languages: {languages}.
The source language is "{source_lang}".

RULES:
- Keep proper nouns (city names, airport names) in their locally accepted form.
- Do NOT transliterate IATA codes or technical terms.
- Keep emoji and formatting markers intact.
- Return ONLY valid JSON with this structure (no extra text):

{{
  "uk": {{"title": "...", "description": "..."}},
  "en": {{"title": "...", "description": "..."}},
  "ru": {{"title": "...", "description": "..."}},
  "de": {{"title": "...", "description": "..."}},
  "fr": {{"title": "...", "description": "..."}},
  "es": {{"title": "...", "description": "..."}},
  "it": {{"title": "...", "description": "..."}},
  "el": {{"title": "...", "description": "..."}}
}}
"""


async def translate_content(
    title: str,
    description: str,
    source_lang: str = "en",
) -> dict[str, dict[str, str]]:
    """Translate title+description to all 8 languages via GPT-4o-mini.

    Returns dict like {"uk": {"title": "...", "description": "..."}, ...}.
    The source language entry contains the original text.
    On failure, returns a dict with only the source language.
    """
    fallback = {source_lang: {"title": title, "description": description}}

    if not title and not description:
        return fallback

    remaining = [l for l in TARGET_LANGUAGES if l != source_lang]
    languages_str = ", ".join(remaining)

    user_msg = f"Title: {title}\n\nDescription: {description}"

    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=4000,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": TRANSLATE_PROMPT.format(
                        languages=languages_str,
                        source_lang=source_lang,
                    ),
                },
                {"role": "user", "content": user_msg},
            ],
        )

        raw = resp.choices[0].message.content or "{}"
        translations: dict = json.loads(raw)

        translations[source_lang] = {"title": title, "description": description}

        valid = {}
        for lang in TARGET_LANGUAGES:
            entry = translations.get(lang, {})
            if isinstance(entry, dict) and entry.get("title"):
                valid[lang] = {
                    "title": entry["title"],
                    "description": entry.get("description", ""),
                }

        if len(valid) < 2:
            logger.warning("[translator] Only %d languages returned, using fallback", len(valid))
            return fallback

        logger.info("[translator] Translated to %d languages", len(valid))
        return valid

    except Exception as exc:
        logger.warning("[translator] Translation failed: %s", exc)
        return fallback


TRANSLATE_RESEARCH_PROMPT = """You are a professional translator for a travel research app.
Translate the following research JSON into ALL of these languages: {languages}.
The source language is "{source_lang}".

The input is a JSON object with "summary" (text) and "content" (JSON string with structured research data).
You must translate both the summary and ALL text values inside the content JSON.

RULES:
- Translate EVERY text field (summary, location_name, descriptions, history periods, place names, news titles, etc.)
- Keep proper nouns (city names, geographic names) in their locally accepted form
- Do NOT translate IATA codes, URLs, country codes, or coordinate numbers
- Keep the JSON structure intact — same keys, same nesting
- Return ONLY valid JSON with this structure (no extra text):

{{
  "uk": {{"summary": "...", "content": "...translated JSON string..."}},
  "en": {{"summary": "...", "content": "...translated JSON string..."}},
  ...for all requested languages...
}}

CRITICAL: The "content" value must be a valid JSON STRING (escaped), not a raw object.
"""


async def translate_research_content(
    summary: str,
    content: str,
    source_lang: str = "uk",
) -> dict[str, dict[str, str]]:
    """Translate research summary+content JSON to all 8 languages.

    Returns dict like {"uk": {"summary": "...", "content": "..."}, ...}.
    On failure, returns a dict with only the source language.
    """
    fallback = {source_lang: {"summary": summary, "content": content}}

    if not summary and not content:
        return fallback

    remaining = [l for l in TARGET_LANGUAGES if l != source_lang]
    languages_str = ", ".join(remaining)

    user_msg = f"Summary: {summary}\n\nContent JSON:\n{content}"

    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=16000,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": TRANSLATE_RESEARCH_PROMPT.format(
                        languages=languages_str,
                        source_lang=source_lang,
                    ),
                },
                {"role": "user", "content": user_msg},
            ],
        )

        raw = resp.choices[0].message.content or "{}"
        translations: dict = json.loads(raw)

        translations[source_lang] = {"summary": summary, "content": content}

        valid = {}
        for lang in TARGET_LANGUAGES:
            entry = translations.get(lang, {})
            if isinstance(entry, dict) and entry.get("summary"):
                c = entry.get("content", "")
                if isinstance(c, dict):
                    c = json.dumps(c, ensure_ascii=False)
                valid[lang] = {
                    "summary": entry["summary"],
                    "content": c,
                }

        if len(valid) < 2:
            logger.warning("[translator] Research: only %d languages, using fallback", len(valid))
            return fallback

        logger.info("[translator] Research translated to %d languages", len(valid))
        return valid

    except Exception as exc:
        logger.warning("[translator] Research translation failed: %s", exc)
        return fallback


async def translate_name(
    name: str,
    source_lang: str = "en",
) -> dict[str, str]:
    """Translate a short name (airport/station) to all 8 languages.

    Returns dict like {"uk": "Аеропорт Гатвік", "en": "Gatwick Airport", ...}.
    """
    fallback = {source_lang: name}
    if not name:
        return fallback

    remaining = [l for l in TARGET_LANGUAGES if l != source_lang]

    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate the following transport facility name into these languages: "
                        f"{', '.join(remaining)}. "
                        f"Source language: {source_lang}. "
                        "Use locally accepted names. Return JSON: "
                        '{"uk": "...", "en": "...", "ru": "...", "de": "...", '
                        '"fr": "...", "es": "...", "it": "...", "el": "..."}'
                    ),
                },
                {"role": "user", "content": name},
            ],
        )

        raw = resp.choices[0].message.content or "{}"
        translations: dict = json.loads(raw)
        translations[source_lang] = name

        valid = {k: v for k, v in translations.items() if k in TARGET_LANGUAGES and isinstance(v, str) and v}
        if len(valid) < 2:
            return fallback

        logger.info("[translator] Name translated to %d languages", len(valid))
        return valid

    except Exception as exc:
        logger.warning("[translator] Name translation failed: %s", exc)
        return fallback
