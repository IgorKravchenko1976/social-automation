"""ElevenLabs TTS client — voice narration for City Pulse events.

We hit the REST API directly (no SDK dep) because the only call we need is
text-to-speech. The response is binary MP3 which we forward to imin-backend.

Pricing reference (Apr 2026):
  - Multilingual v2:  ~ 1 credit per character
  - Starter plan:     30k credits / month
  - Creator plan:     100k credits / month

Average City Pulse description is ~250 chars + ~50 chars intro/outro, so
each generation costs ~300 credits → ~330 events/month on Starter, ~1000
on Creator. Source-language only at fetch time keeps us inside Starter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"

# Conservative timeout. ElevenLabs streams audio; even minute-long clips
# usually finish in < 15s. 60s gives generous headroom.
REQUEST_TIMEOUT = 60

# Cap to keep individual narrations under ~1 MB. ElevenLabs charges per
# character so capping protects against runaway prompts.
MAX_INPUT_CHARS = 1500


def is_configured() -> bool:
    return bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id)


@dataclass
class TTSResult:
    audio_bytes: bytes
    content_type: str  # "audio/mpeg"
    chars_used: int
    voice_id: str


# Voice settings tuned for cultural-events narration:
#   stability=0.55  — relaxed enough to sound natural, stable enough to
#                     keep names/places intact across multiple events
#   similarity_boost=0.75 — keeps the chosen voice's character
#   style=0.30      — subtle expressiveness, not theatrical
#   use_speaker_boost=True — slightly louder, more confident delivery
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.55,
    "similarity_boost": 0.75,
    "style": 0.30,
    "use_speaker_boost": True,
}


async def synthesize_narration(
    text: str,
    *,
    voice_id: Optional[str] = None,
    model_id: Optional[str] = None,
    voice_settings: Optional[dict] = None,
) -> Optional[TTSResult]:
    """Generate MP3 narration for a single chunk of text.

    Returns None if not configured or on a non-retryable error. Callers
    should handle None as "skip this event for now". 5xx errors raise so
    the caller's retry/circuit-breaker logic kicks in.
    """
    if not is_configured():
        logger.debug("[elevenlabs] Not configured, skipping TTS")
        return None

    text = (text or "").strip()
    if len(text) < 10:
        logger.debug("[elevenlabs] Text too short (%d chars), skipping", len(text))
        return None
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
        logger.warning("[elevenlabs] Truncated input to %d chars", MAX_INPUT_CHARS)

    voice = voice_id or settings.elevenlabs_voice_id
    model = model_id or settings.elevenlabs_model_id

    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": voice_settings or DEFAULT_VOICE_SETTINGS,
    }
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    url = f"{API_BASE}/text-to-speech/{voice}?output_format=mp3_44100_128"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code == 401:
        logger.error("[elevenlabs] Auth failed (401) — check ELEVENLABS_API_KEY")
        return None
    if resp.status_code == 422:
        logger.warning("[elevenlabs] Validation error: %s", resp.text[:300])
        return None
    if resp.status_code == 429:
        logger.warning("[elevenlabs] Rate limited (429), backing off")
        return None
    if resp.status_code >= 500:
        # Surface 5xx so the caller can retry / archive the cycle
        resp.raise_for_status()
    if resp.status_code != 200:
        logger.warning("[elevenlabs] HTTP %d: %s", resp.status_code, resp.text[:300])
        return None

    audio = resp.content
    if not audio:
        logger.warning("[elevenlabs] Empty response body")
        return None

    return TTSResult(
        audio_bytes=audio,
        content_type="audio/mpeg",
        chars_used=len(text),
        voice_id=voice,
    )


def build_narration_text(
    title: str,
    description: str,
    *,
    venue_name: str = "",
    starts_at_human: str = "",
    category_label: str = "",
    city: str = "",
    intro_template: Optional[str] = None,
    outro: Optional[str] = None,
) -> str:
    """Assemble a clean, listenable narration script.

    Output shape (Ukrainian default):
        {city}. {category_label}. {title}.
        {when_clause}, {venue_clause}.
        {description}
        {outro}

    Empty fields are skipped silently. Total length capped at MAX_INPUT_CHARS.
    """
    parts: list[str] = []

    intro_bits: list[str] = []
    if city:
        intro_bits.append(city.strip())
    if category_label:
        intro_bits.append(category_label.strip())
    if intro_bits:
        parts.append(". ".join(intro_bits) + ".")

    if title:
        parts.append(title.strip().rstrip(".") + ".")

    when_venue: list[str] = []
    if starts_at_human:
        when_venue.append(starts_at_human.strip())
    if venue_name:
        when_venue.append(venue_name.strip())
    if when_venue:
        parts.append(", ".join(when_venue).rstrip(".") + ".")

    description = (description or "").strip()
    if description:
        parts.append(description)

    if outro:
        parts.append(outro.strip())

    text = " ".join(p for p in parts if p)
    return text[:MAX_INPUT_CHARS]


# Localized intro/outro phrasing per language. Keep it short — every char
# is billed. UA uses lowercased "im-in" since punctuation reads weird.
LOCALIZED_OUTRO = {
    "uk": "Деталі — у застосунку I'M IN.",
    "en": "More info in the I'M IN app.",
    "ru": "Подробности — в приложении I'M IN.",
    "de": "Mehr Infos in der I'M IN App.",
    "fr": "Plus d'infos dans l'application I'M IN.",
    "es": "Más información en la app I'M IN.",
    "it": "Maggiori informazioni nell'app I'M IN.",
    "el": "Περισσότερα στην εφαρμογή I'M IN.",
}

LOCALIZED_CATEGORIES = {
    "cinema":     {"uk": "Кіно", "en": "Cinema", "ru": "Кино", "de": "Kino", "fr": "Cinéma", "es": "Cine", "it": "Cinema", "el": "Σινεμά"},
    "theater":    {"uk": "Театр", "en": "Theater", "ru": "Театр", "de": "Theater", "fr": "Théâtre", "es": "Teatro", "it": "Teatro", "el": "Θέατρο"},
    "concert":    {"uk": "Концерт", "en": "Concert", "ru": "Концерт", "de": "Konzert", "fr": "Concert", "es": "Concierto", "it": "Concerto", "el": "Συναυλία"},
    "exhibition": {"uk": "Виставка", "en": "Exhibition", "ru": "Выставка", "de": "Ausstellung", "fr": "Exposition", "es": "Exposición", "it": "Mostra", "el": "Έκθεση"},
    "sale":       {"uk": "Акція", "en": "Sale", "ru": "Акция", "de": "Sale", "fr": "Soldes", "es": "Rebajas", "it": "Saldi", "el": "Έκπτωση"},
    "festival":   {"uk": "Фестиваль", "en": "Festival", "ru": "Фестиваль", "de": "Festival", "fr": "Festival", "es": "Festival", "it": "Festival", "el": "Φεστιβάλ"},
    "workshop":   {"uk": "Подія", "en": "Event", "ru": "Событие", "de": "Event", "fr": "Événement", "es": "Evento", "it": "Evento", "el": "Εκδήλωση"},
    "tour":       {"uk": "Екскурсія", "en": "Tour", "ru": "Экскурсия", "de": "Tour", "fr": "Visite", "es": "Tour", "it": "Tour", "el": "Ξενάγηση"},
}


def localized_category(category: str, lang: str) -> str:
    bag = LOCALIZED_CATEGORIES.get(category, {})
    return bag.get(lang, bag.get("en", ""))


def localized_outro(lang: str) -> str:
    return LOCALIZED_OUTRO.get(lang, LOCALIZED_OUTRO["en"])
