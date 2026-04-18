from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from config.platforms import Platform

logger = logging.getLogger(__name__)

TRANSIENT_FB_CODES = {190, 1, 2, 4, 17, 368}
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


def is_transient_fb_error(error_data: dict) -> bool:
    """Check if a Facebook Graph API error is likely transient (account block, rate limit, etc.)."""
    code = error_data.get("code", 0)
    subcode = error_data.get("error_subcode", 0)
    if code in TRANSIENT_FB_CODES:
        return True
    if code == 100 and subcode in (33, 2018001):
        return True
    msg = error_data.get("message", "").lower()
    if "temporarily" in msg or "try again" in msg or "rate limit" in msg:
        return True
    return False


async def retry_on_transient(func, *args, label: str = "API call", **kwargs):
    """Execute an async function with retry on transient Facebook API errors.

    Returns the raw response dict. Raises on non-transient errors or exhausted retries.
    The wrapped function must return a dict (parsed JSON response).
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        result = await func(*args, **kwargs)
        if "error" not in result:
            return result
        error_data = result["error"]
        if not is_transient_fb_error(error_data):
            return result
        last_error = error_data
        delay = RETRY_BASE_DELAY * (2 ** attempt)
        logger.warning(
            "%s: transient error (code=%s, attempt %d/%d), retrying in %.0fs — %s",
            label, error_data.get("code"), attempt + 1, MAX_RETRIES, delay,
            error_data.get("message", "")[:120],
        )
        await asyncio.sleep(delay)
    logger.error("%s: exhausted %d retries, last error: %s", label, MAX_RETRIES, last_error)
    return {"error": last_error}


@dataclass
class PublishResult:
    success: bool
    platform_post_id: Optional[str] = None
    error: Optional[str] = None


class BasePlatform(abc.ABC):
    platform: Platform

    @abc.abstractmethod
    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        ...

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        return PublishResult(success=False, error="Video publishing not supported")

    async def get_new_messages(self) -> list[dict]:
        """Poll for new messages/comments. Returns list of dicts with keys:
        platform_message_id, sender_id, sender_name, text
        """
        return []

    async def send_reply(self, platform_message_id: str, text: str) -> bool:
        return False


class TokenPlatformMixin:
    """Shared token caching for platforms using get_active_token (Facebook, Instagram)."""
    _cached_token: str | None = None
    _platform_name: str = ""
    _env_token_attr: str = ""

    async def _get_token(self) -> str:
        from stats.token_renewer import get_active_token
        from config.settings import settings
        db_token = await get_active_token(self._platform_name)
        if db_token:
            return db_token
        return getattr(settings, self._env_token_attr, "")

    @property
    def _token(self) -> str:
        from config.settings import settings
        return self._cached_token or getattr(settings, self._env_token_attr, "")

    async def _ensure_token(self) -> None:
        self._cached_token = await self._get_token()
