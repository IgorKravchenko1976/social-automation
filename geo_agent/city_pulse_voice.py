"""City Pulse — voice narration processor.

Runs as its own APScheduler interval (every ~3 min). Each cycle:
  1. Pulls up to N pending events for the source language ("uk" by default —
     covers Kyiv content first since it's the home market).
  2. Builds a narration script per event (intro + title + description + outro).
  3. Calls ElevenLabs Multilingual v2 → MP3 bytes.
  4. Uploads MP3 to imin-backend (which stores in B2 + updates city_events).
  5. On failure, marks the event audio_status='failed' so we don't loop on it.

Source language only is processed at fetch time — other languages are
generated on-demand when a user opens an event in their language and the
app polls /city-events/{id}/voice (which sets audio_status='pending').
The same processor picks them up next cycle.

Cost discipline: each cycle handles <= MAX_PER_CYCLE events to avoid
burning the entire ElevenLabs daily budget in one batch.
"""
from __future__ import annotations

import asyncio
import logging

from content.elevenlabs_client import (
    is_configured as elevenlabs_configured,
    synthesize_narration,
    build_narration_text,
    localized_category,
    localized_outro,
)
from geo_agent import backend_client

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

# Upper bound per cycle — keeps a single processor tick under ~60s and
# limits the per-cycle ElevenLabs spend to ~1500 chars * MAX_PER_CYCLE.
MAX_PER_CYCLE = 5

# Languages we'll process. Order matters — "uk" first because most events
# are Kyiv-based at this point. As more cities come online, English entries
# kick in via translations.
PRIORITY_LANGUAGES = ["uk", "en", "ru", "de", "fr", "es", "it", "el"]


async def process_city_pulse_voice() -> bool:
    """One voice-generation cycle. Returns True if anything was processed."""
    async with _lock:
        if not backend_client.is_configured():
            return False
        if not elevenlabs_configured():
            logger.debug("[city-pulse-voice] ElevenLabs not configured, skipping")
            return False

        for lang in PRIORITY_LANGUAGES:
            try:
                jobs = await backend_client.fetch_pending_voice_jobs(
                    lang=lang, limit=MAX_PER_CYCLE)
            except Exception as exc:
                logger.warning("[city-pulse-voice] fetch (%s) failed: %s", lang, exc)
                continue

            if not jobs:
                continue

            logger.info("[city-pulse-voice] processing %d %s jobs", len(jobs), lang)
            for job in jobs:
                ok = await _process_one(job, lang)
                if not ok:
                    # Slight delay between jobs to be polite to ElevenLabs
                    # rate limits even when individual calls succeed.
                    await asyncio.sleep(0.5)
            return True

    return False


async def _process_one(job: backend_client.CityPulseVoiceJob, lang: str) -> bool:
    """Synthesize + upload one event in one language."""
    title = job.title
    description = job.description

    # Use translation for non-source languages.
    if lang != job.content_language and isinstance(job.translations, dict):
        tr = job.translations.get(lang)
        if isinstance(tr, dict):
            title = tr.get("title", title) or title
            description = tr.get("description", description) or description

    if not title:
        logger.debug("[city-pulse-voice] skip event %d (no title)", job.id)
        return False

    script = build_narration_text(
        title=title,
        description=description,
        venue_name=job.venue_name,
        starts_at_human="",  # let the GPT description carry timing for now
        category_label=localized_category(job.category, lang),
        city=job.city,
        outro=localized_outro(lang),
    )

    if len(script) < 20:
        logger.debug("[city-pulse-voice] skip event %d (script too short)", job.id)
        try:
            await backend_client.submit_voice_failed(job.id, "script_too_short")
        except Exception:
            pass
        return False

    try:
        result = await synthesize_narration(script)
    except Exception as exc:
        logger.warning(
            "[city-pulse-voice] TTS event=%d lang=%s failed: %s", job.id, lang, exc)
        try:
            await backend_client.submit_voice_failed(job.id, str(exc)[:200])
        except Exception:
            pass
        return False

    if result is None:
        logger.info("[city-pulse-voice] TTS event=%d lang=%s returned no audio", job.id, lang)
        try:
            await backend_client.submit_voice_failed(job.id, "no_audio_returned")
        except Exception:
            pass
        return False

    try:
        resp = await backend_client.upload_voice(
            city_event_id=job.id,
            lang=lang,
            audio_bytes=result.audio_bytes,
            content_type=result.content_type,
        )
    except Exception as exc:
        logger.warning(
            "[city-pulse-voice] upload event=%d lang=%s failed: %s", job.id, lang, exc)
        try:
            await backend_client.submit_voice_failed(job.id, f"upload_failed: {exc}")
        except Exception:
            pass
        return False

    logger.info(
        "[city-pulse-voice] event=%d lang=%s ok (%d chars, %d KB)",
        job.id, lang, result.chars_used, len(result.audio_bytes) // 1024,
    )
    return True
