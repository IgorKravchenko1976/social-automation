from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform, INSTAGRAM_GRAPH_API as GRAPH_API
from platforms.base import BasePlatform, PublishResult, TokenPlatformMixin

logger = logging.getLogger(__name__)


class InstagramPlatform(TokenPlatformMixin, BasePlatform):
    platform = Platform.INSTAGRAM
    _platform_name = "instagram"
    _env_token_attr = "instagram_access_token"

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
        image_url = await self._upload_image_and_get_url(image_path)
        if not image_url:
            with open(image_path, "rb") as f:
                image_data = f.read()
            import base64
            logger.error("Cannot get public URL for image, Instagram requires a public URL")
            return PublishResult(success=False, error="Instagram requires a publicly accessible image URL")

        resp = await client.post(
            f"{GRAPH_API}/{self._user_id}/media",
            params={"access_token": self._token},
            data={
                "image_url": image_url,
                "caption": caption[:2200],
            },
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

        for attempt in range(6):
            status_resp = await client.get(
                f"{GRAPH_API}/{container_id}",
                params={
                    "access_token": self._token,
                    "fields": "status_code",
                },
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

    async def _upload_image_and_get_url(self, image_path: str) -> str | None:
        """Upload image to a temporary public host and return the URL.
        Uses transfer.sh or similar; falls back to None.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                with open(image_path, "rb") as f:
                    resp = await client.put(
                        f"https://transfer.sh/{image_path.split('/')[-1]}",
                        content=f.read(),
                        headers={"Content-Type": "image/jpeg"},
                    )
                if resp.status_code == 200:
                    url = resp.text.strip()
                    logger.info("Image uploaded for Instagram: %s", url)
                    return url
        except Exception:
            logger.warning("transfer.sh upload failed, trying alternative")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                with open(image_path, "rb") as f:
                    resp = await client.post(
                        "https://0x0.st",
                        files={"file": ("image.jpg", f, "image/jpeg")},
                    )
                if resp.status_code == 200:
                    url = resp.text.strip()
                    logger.info("Image uploaded for Instagram (0x0.st): %s", url)
                    return url
        except Exception:
            logger.warning("0x0.st upload failed")

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
