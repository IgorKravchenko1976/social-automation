from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform, FACEBOOK_GRAPH_API as GRAPH_API_BASE
from platforms.base import BasePlatform, PublishResult, TokenPlatformMixin

logger = logging.getLogger(__name__)


class FacebookPlatform(TokenPlatformMixin, BasePlatform):
    platform = Platform.FACEBOOK
    _platform_name = "facebook"
    _env_token_attr = "facebook_page_access_token"

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=60) as client:
                if image_path:
                    return await self._publish_with_image(client, text, image_path)

                resp = await client.post(
                    f"{GRAPH_API_BASE}/{settings.facebook_page_id}/feed",
                    params={"access_token": self._token},
                    json={"message": text},
                )
                data = resp.json()
                if "error" in data:
                    err = data["error"].get("message", str(data["error"]))
                    logger.error("Facebook publish error: %s", err)
                    return PublishResult(success=False, error=err)
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
                params={"access_token": self._token},
                data={"message": text},
                files={"source": ("image.jpg", f, "image/jpeg")},
            )
        data = resp.json()
        if "error" in data:
            err = data["error"].get("message", str(data["error"]))
            logger.error("Facebook photo publish error: %s", err)
            return PublishResult(success=False, error=err)
        return PublishResult(success=True, platform_post_id=data.get("id"))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=120) as client:
                with open(video_path, "rb") as f:
                    resp = await client.post(
                        f"{GRAPH_API_BASE}/{settings.facebook_page_id}/videos",
                        params={"access_token": self._token},
                        data={"description": text},
                        files={"source": ("video.mp4", f, "video/mp4")},
                    )
                data = resp.json()
                if "error" in data:
                    err = data["error"].get("message", str(data["error"]))
                    return PublishResult(success=False, error=err)
                return PublishResult(success=True, platform_post_id=data.get("id"))
        except Exception as e:
            logger.exception("Facebook video publish failed")
            return PublishResult(success=False, error=str(e))

    async def get_new_messages(self) -> list[dict]:
        """Fetch recent comments on page posts."""
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{GRAPH_API_BASE}/{settings.facebook_page_id}/feed",
                    params={
                        "access_token": self._token,
                        "fields": "id,comments{id,from,message,created_time}",
                        "limit": 5,
                    },
                )
                data = resp.json()
                if "error" in data:
                    logger.error("Facebook fetch messages error: %s", data["error"])
                    return []

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
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{GRAPH_API_BASE}/{platform_message_id}/comments",
                    params={"access_token": self._token},
                    json={"message": text},
                )
                data = resp.json()
                if "error" in data:
                    logger.error("Facebook reply error: %s", data["error"])
                    return False
                return True
        except Exception:
            logger.exception("Facebook reply failed")
            return False
