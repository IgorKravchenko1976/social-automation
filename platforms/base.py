from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional

from config.platforms import Platform

logger = logging.getLogger(__name__)


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
