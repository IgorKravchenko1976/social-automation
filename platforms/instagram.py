from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform, FACEBOOK_GRAPH_API as GRAPH_API
from platforms.base import BasePlatform, PublishResult, TokenPlatformMixin

logger = logging.getLogger(__name__)


class InstagramPlatform(TokenPlatformMixin, BasePlatform):
    platform = Platform.INSTAGRAM
    _platform_name = "facebook"
    _env_token_attr = "facebook_page_access_token"

    @property
    def _user_id(self) -> str:
        return settings.instagram_user_id

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        """Instagram requires an image for every post. If no image, generate one."""
        if not self._user_id or not settings.instagram_access_token:
            return PublishResult(success=False, error="Instagram not configured")

        if not image_path:
            from content.media import get_image_for_post
            image_path = await get_image_for_post(text[:100])
            if not image_path:
                return PublishResult(success=False, error="Instagram requires an image, none available")

        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=120) as client:
                return await self._publish_with_image(client, text, image_path)
        except Exception as e:
            logger.exception("Instagram publish failed")
            return PublishResult(success=False, error=str(e))

    async def _publish_with_image(
        self, client: httpx.AsyncClient, caption: str, image_path: str
    ) -> PublishResult:
        image_url = await self._get_public_image_url(client, image_path)
        if not image_url:
            return PublishResult(success=False, error="Could not get public URL for image")

        resp = await client.post(
            f"{GRAPH_API}/{self._user_id}/media",
            params={"access_token": self._token},
            data={"image_url": image_url, "caption": caption[:2200]},
        )
        data = resp.json()
        if "error" in data:
            err = data["error"].get("message", str(data["error"]))
            logger.error("Instagram create media error: %s", err)
            return PublishResult(success=False, error=err)

        container_id = data.get("id")
        if not container_id:
            return PublishResult(success=False, error="No container ID returned")

        await asyncio.sleep(5)

        for _ in range(6):
            status_resp = await client.get(
                f"{GRAPH_API}/{container_id}",
                params={"access_token": self._token, "fields": "status_code"},
            )
            status_data = status_resp.json()
            status_code = status_data.get("status_code", "")
            if status_code == "FINISHED":
                break
            if status_code == "ERROR":
                return PublishResult(success=False, error=f"Container error: {status_data}")
            await asyncio.sleep(3)

        pub_resp = await client.post(
            f"{GRAPH_API}/{self._user_id}/media_publish",
            params={"access_token": self._token},
            data={"creation_id": container_id},
        )
        pub_data = pub_resp.json()
        if "error" in pub_data:
            err = pub_data["error"].get("message", str(pub_data["error"]))
            logger.error("Instagram publish error: %s", err)
            return PublishResult(success=False, error=err)

        return PublishResult(success=True, platform_post_id=pub_data.get("id"))

    async def _get_public_image_url(self, client: httpx.AsyncClient, image_path: str) -> str | None:
        """Get a public URL for the image by uploading as unpublished Facebook photo."""
        if self._token and settings.facebook_page_id:
            url = await self._upload_via_facebook(client, image_path)
            if url:
                return url

        logger.error("No method available to get public image URL for Instagram")
        return None

    async def _upload_via_facebook(
        self, client: httpx.AsyncClient, image_path: str,
    ) -> str | None:
        """Upload image as unpublished Facebook photo, return the public source URL."""
        try:
            with open(image_path, "rb") as f:
                resp = await client.post(
                    f"{GRAPH_API}/{settings.facebook_page_id}/photos",
                    params={"access_token": self._token},
                    data={"published": "false"},
                    files={"source": ("image.jpg", f, "image/jpeg")},
                )
            data = resp.json()
            if "error" in data:
                logger.warning("Facebook unpublished photo upload failed: %s",
                               data["error"].get("message", data["error"]))
                return None

            photo_id = data.get("id")
            if not photo_id:
                return None

            img_resp = await client.get(
                f"{GRAPH_API}/{photo_id}",
                params={"access_token": self._token, "fields": "images"},
            )
            img_data = img_resp.json()
            images = img_data.get("images", [])
            if images:
                url = images[0].get("source", "")
                logger.info("Got public image URL via Facebook: %s", url[:80])
                return url

            return None
        except Exception:
            logger.exception("Facebook image upload for Instagram failed")
            return None

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        return PublishResult(success=False, error="Instagram video publishing not yet implemented via API")

    async def get_new_messages(self) -> list[dict]:
        """Fetch recent comments on Instagram media."""
        if not self._user_id or not settings.instagram_access_token:
            return []
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{GRAPH_API}/{self._user_id}/media",
                    params={
                        "access_token": self._token,
                        "fields": "id,comments{id,from,text,timestamp}",
                        "limit": 5,
                    },
                )
                data = resp.json()
                if "error" in data:
                    logger.error("Instagram fetch media error: %s", data["error"])
                    return []

                messages = []
                for media in data.get("data", []):
                    for comment in media.get("comments", {}).get("data", []):
                        from_user = comment.get("from", {})
                        messages.append({
                            "platform_message_id": comment["id"],
                            "sender_id": from_user.get("id", ""),
                            "sender_name": from_user.get("username", ""),
                            "text": comment.get("text", ""),
                        })
                return messages
        except Exception:
            logger.exception("Instagram fetch messages failed")
            return []

    async def send_reply(self, platform_message_id: str, text: str) -> bool:
        """Reply to an Instagram comment."""
        if not self._user_id:
            return False
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{GRAPH_API}/{platform_message_id}/replies",
                    params={"access_token": self._token},
                    data={"message": text},
                )
                data = resp.json()
                if "error" in data:
                    logger.error("Instagram reply error: %s", data["error"])
                    return False
                return True
        except Exception:
            logger.exception("Instagram reply failed")
            return False
