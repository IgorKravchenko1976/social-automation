from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class FacebookPlatform(BasePlatform):
    platform = Platform.FACEBOOK

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {settings.facebook_page_access_token}"}

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                if image_path:
                    return await self._publish_with_image(client, text, image_path)

                resp = await client.post(
                    f"{GRAPH_API_BASE}/{settings.facebook_page_id}/feed",
                    headers=self._headers,
                    json={"message": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return PublishResult(success=True, platform_post_id=data.get("id"))
        except Exception as e:
            logger.exception("Facebook publish failed")
            return PublishResult(success=False, error=str(e))

    async def _publish_with_image(
        self, client: httpx.AsyncClient, text: str, image_path: str
    ) -> PublishResult:
        with open(image_path, "rb") as f:
            resp = await client.post(
                f"{GRAPH_API_BASE}/{settings.facebook_page_id}/photos",
                headers=self._headers,
                data={"message": text},
                files={"source": ("image.jpg", f, "image/jpeg")},
            )
        resp.raise_for_status()
        data = resp.json()
        return PublishResult(success=True, platform_post_id=data.get("id"))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                with open(video_path, "rb") as f:
                    resp = await client.post(
                        f"{GRAPH_API_BASE}/{settings.facebook_page_id}/videos",
                        headers=self._headers,
                        data={"description": text},
                        files={"source": ("video.mp4", f, "video/mp4")},
                    )
                resp.raise_for_status()
                data = resp.json()
                return PublishResult(success=True, platform_post_id=data.get("id"))
        except Exception as e:
            logger.exception("Facebook video publish failed")
            return PublishResult(success=False, error=str(e))

    async def get_new_messages(self) -> list[dict]:
        """Fetch recent comments on page posts."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{GRAPH_API_BASE}/{settings.facebook_page_id}/feed",
                    headers=self._headers,
                    params={"fields": "id,comments{id,from,message,created_time}", "limit": 5},
                )
                resp.raise_for_status()
                data = resp.json()

                messages = []
                for post in data.get("data", []):
                    for comment in post.get("comments", {}).get("data", []):
                        messages.append({
                            "platform_message_id": comment["id"],
                            "sender_id": comment.get("from", {}).get("id", ""),
                            "sender_name": comment.get("from", {}).get("name", ""),
                            "text": comment.get("message", ""),
                        })
                return messages
        except Exception:
            logger.exception("Facebook fetch messages failed")
            return []

    async def send_reply(self, platform_message_id: str, text: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{GRAPH_API_BASE}/{platform_message_id}/comments",
                    headers=self._headers,
                    json={"message": text},
                )
                resp.raise_for_status()
                return True
        except Exception:
            logger.exception("Facebook reply failed")
            return False
