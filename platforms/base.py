from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from config.platforms import Platform


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
