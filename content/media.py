from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

import httpx

from config.settings import settings
from content.ai_client import get_client

logger = logging.getLogger(__name__)


def cleanup_media_file(file_path: str | None) -> None:
    """Delete a local media file if it exists. Called after publishing is done."""
    if not file_path:
        return
    try:
        p = Path(file_path)
        if p.exists() and p.is_file():
            p.unlink()
            logger.info("Cleaned up media file: %s", p.name)
    except Exception:
        logger.warning("Failed to delete media file: %s", file_path)

def _get_media_dir() -> Path:
    d = Path(settings.media_cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def download_image_pexels(query: str) -> Optional[str]:
    """Download a relevant image from Pexels. Returns local file path or None."""
    if not settings.pexels_api_key:
        logger.warning("Pexels API key not configured")
        return None

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": settings.pexels_api_key},
        )
        if resp.status_code != 200:
            logger.error("Pexels API error: %s", resp.text)
            return None

        data = resp.json()
        photos = data.get("photos", [])
        if not photos:
            return None

        image_url = photos[0]["src"]["large"]
        img_resp = await client.get(image_url)
        if img_resp.status_code != 200:
            return None

        filename = f"pexels_{uuid.uuid4().hex[:8]}.jpg"
        filepath = _get_media_dir() / filename
        filepath.write_bytes(img_resp.content)
        return str(filepath)


async def generate_image_dalle(prompt: str) -> Optional[str]:
    """Generate an image via DALL-E 3. Returns local file path or None."""
    if not settings.openai_api_key:
        return None

    try:
        response = await get_client().images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        image_url = response.data[0].url

        async with httpx.AsyncClient(timeout=60) as http_client:
            img_resp = await http_client.get(image_url)
            if img_resp.status_code != 200:
                return None

            filename = f"dalle_{uuid.uuid4().hex[:8]}.png"
            filepath = _get_media_dir() / filename
            filepath.write_bytes(img_resp.content)
            return str(filepath)
    except Exception:
        logger.exception("DALL-E image generation failed")
        return None


# Minimum bytes for a "real" raster image. Wikipedia/Wikimedia and Google
# Places sometimes return tiny SVG logos (~400 B) or 1×1 placeholder JPEGs.
# Telegram/Facebook/Instagram all reject these as IMAGE_PROCESS_FAILED /
# Invalid parameter. 5 KB is a safe floor for a legitimate landscape photo.
_MIN_IMAGE_BYTES = 5 * 1024


def _detect_image_format(payload: bytes) -> Optional[str]:
    """Return 'jpg' / 'png' / 'webp' if magic bytes match, else None.

    Social platforms only accept raster formats. SVG/HEIC/AVIF/etc. are
    rejected upstream — we filter them out here so we never store a file
    that the publish step is doomed to fail on.
    """
    if len(payload) < 12:
        return None
    if payload[:3] == b"\xff\xd8\xff":
        return "jpg"
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    return None


async def download_image_from_url(url: str) -> Optional[str]:
    """Download an image from a direct URL (e.g. Wikipedia/Wikimedia).

    Returns local file path or None. Rejects SVG, files smaller than
    _MIN_IMAGE_BYTES and any payload whose magic bytes are not a raster
    format that Telegram/Facebook/Instagram all accept.

    Why so strict (incident 2026-05-02):
      Wikidata's `imageUrl` for McDonald's returned a 389-byte SVG logo.
      We saved it as poi_*.jpg and shipped it to all 3 platforms; every
      one rejected the file (TG: IMAGE_PROCESS_FAILED, FB: Invalid
      parameter, IG: Could not get public URL). The POI handoff then
      retried the same point hourly, blocking 5 regular slots from
      publishing. Better: reject the bad image at download time, let the
      pipeline fall back to text-only (or skip Instagram).
    """
    if not url or not url.startswith("http"):
        return None

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "ImInBot/1.0 (travel app; igork2011@gmail.com)",
            })
            if resp.status_code != 200:
                logger.warning("[media] Download failed (%d) for %s", resp.status_code, url[:100])
                return None

            content_type = resp.headers.get("content-type", "").lower()
            if "svg" in content_type:
                logger.warning(
                    "[media] Rejecting SVG image (not supported by social platforms): %s",
                    url[:100],
                )
                return None
            if not any(t in content_type for t in ("image/", "octet-stream")):
                logger.warning("[media] Not an image (%s): %s", content_type, url[:100])
                return None

            payload = resp.content
            if len(payload) < _MIN_IMAGE_BYTES:
                logger.warning(
                    "[media] Rejecting tiny image (%d B < %d B floor): %s",
                    len(payload), _MIN_IMAGE_BYTES, url[:100],
                )
                return None

            fmt = _detect_image_format(payload)
            if fmt is None:
                logger.warning(
                    "[media] Unsupported image format (magic=%s, content-type=%s): %s",
                    payload[:8].hex(), content_type, url[:100],
                )
                return None

            filename = f"poi_{uuid.uuid4().hex[:8]}.{fmt}"
            filepath = _get_media_dir() / filename
            filepath.write_bytes(payload)
            logger.info(
                "[media] Downloaded real image: %s (%d KB, format=%s)",
                filename, len(payload) // 1024, fmt,
            )
            return str(filepath)
    except Exception:
        logger.warning("[media] Failed to download image from %s", url[:100], exc_info=True)
        return None


async def get_image_for_post(
    query: str,
    use_dalle: bool = True,
    prefer_dalle: bool = False,
    dalle_prompt: str | None = None,
) -> Optional[str]:
    """Get an image for a post.

    prefer_dalle=True → DALL-E first (unique per location), Pexels fallback.
    prefer_dalle=False → Pexels first (cheaper), DALL-E fallback.
    dalle_prompt → custom DALL-E prompt instead of auto-generating from query.
    """
    if prefer_dalle and settings.openai_api_key:
        prompt = dalle_prompt
        if not prompt:
            from content.generator import generate_image_prompt
            prompt = await generate_image_prompt(query)
        path = await generate_image_dalle(prompt)
        if path:
            return path
        logger.info("[media] DALL-E failed, falling back to Pexels for: %s", query[:60])

    path = await download_image_pexels(query)
    if path:
        return path

    if not prefer_dalle and use_dalle and settings.openai_api_key:
        prompt = dalle_prompt
        if not prompt:
            from content.generator import generate_image_prompt
            prompt = await generate_image_prompt(query)
        return await generate_image_dalle(prompt)

    return None


async def create_slideshow_video(
    image_paths: list[str],
    text_overlay: str = "",
    duration_per_image: float = 3.0,
) -> Optional[str]:
    """Create a simple slideshow video from images for TikTok."""
    try:
        from moviepy import ImageClip, concatenate_videoclips, TextClip, CompositeVideoClip

        clips = []
        for img_path in image_paths:
            clip = ImageClip(img_path, duration=duration_per_image)
            clips.append(clip)

        if not clips:
            return None

        video = concatenate_videoclips(clips, method="compose")

        if text_overlay:
            txt_clip = TextClip(
                text=text_overlay,
                font_size=40,
                color="white",
                bg_color=(0, 0, 0, 128),
                size=(video.w - 40, None),
                method="caption",
            )
            txt_clip = txt_clip.with_duration(video.duration).with_position("bottom")
            video = CompositeVideoClip([video, txt_clip])

        filename = f"slideshow_{uuid.uuid4().hex[:8]}.mp4"
        filepath = _get_media_dir() / filename
        video.write_videofile(
            str(filepath),
            fps=24,
            codec="libx264",
            audio=False,
            logger=None,
        )
        video.close()
        return str(filepath)
    except Exception:
        logger.exception("Video creation failed")
        return None
