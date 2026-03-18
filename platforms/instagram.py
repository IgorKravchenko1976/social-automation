from __future__ import annotations

import logging
from typing import Optional

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult

logger = logging.getLogger(__name__)


class InstagramPlatform(BasePlatform):
    platform = Platform.INSTAGRAM

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from instagrapi import Client
            self._client = Client()
            self._client.login(settings.instagram_username, settings.instagram_password)
        return self._client

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        """Instagram requires an image for every post."""
        if not image_path:
            return PublishResult(success=False, error="Instagram requires an image")

        try:
            cl = self._get_client()
            media = cl.photo_upload(image_path, caption=text[:2200])
            return PublishResult(success=True, platform_post_id=str(media.pk))
        except Exception as e:
            logger.exception("Instagram publish failed")
            self._client = None  # reset on auth errors
            return PublishResult(success=False, error=str(e))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            cl = self._get_client()
            media = cl.video_upload(video_path, caption=text[:2200])
            return PublishResult(success=True, platform_post_id=str(media.pk))
        except Exception as e:
            logger.exception("Instagram video publish failed")
            self._client = None
            return PublishResult(success=False, error=str(e))

    async def get_new_messages(self) -> list[dict]:
        """Fetch recent DMs via instagrapi."""
        try:
            cl = self._get_client()
            threads = cl.direct_threads(amount=10)
            messages = []
            for thread in threads:
                for msg in thread.messages[:3]:
                    if msg.user_id != cl.user_id:
                        messages.append({
                            "platform_message_id": str(msg.id),
                            "sender_id": str(msg.user_id),
                            "sender_name": thread.thread_title or "",
                            "text": msg.text or "",
                        })
            return messages
        except Exception:
            logger.exception("Instagram fetch messages failed")
            self._client = None
            return []

    async def send_reply(self, platform_message_id: str, text: str) -> bool:
        try:
            cl = self._get_client()
            # instagrapi uses thread_id; we store message id, so derive thread
            cl.direct_answer(thread_id=0, text=text)
            return True
        except Exception:
            logger.exception("Instagram reply failed")
            return False
