"""Multi-language translation for blog posts via OpenAI."""
from __future__ import annotations

import json
import logging

from content.ai_client import get_client

logger = logging.getLogger(__name__)

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
    target_langs = [lang for lang in BLOG_LANGUAGES if lang != source_lang]
    if not target_langs:
        return {}

    lang_list = ", ".join(f"{code} ({LANG_NAMES[code]})" for code in target_langs)
    client = get_client()

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
                        'Format: {"en": {"title": "...", "content": "..."}, '
                        '"fr": {"title": "...", "content": "..."}, ...}\n'
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
