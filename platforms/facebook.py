from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform, FACEBOOK_GRAPH_API as GRAPH_API_BASE
from platforms.base import BasePlatform, PublishResult, TokenPlatformMixin, retry_on_transient

logger = logging.getLogger(__name__)


class FacebookPlatform(TokenPlatformMixin, BasePlatform):
    platform = Platform.FACEBOOK
    _platform_name = "facebook"
    _env_token_attr = "facebook_page_access_token"

    async def _fb_get(self, client: httpx.AsyncClient, url: str, **kwargs) -> dict:
        resp = await client.get(url, **kwargs)
        return resp.json()

    async def _fb_post(self, client: httpx.AsyncClient, url: str, **kwargs) -> dict:
        resp = await client.post(url, **kwargs)
        return resp.json()

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=60) as client:
                if image_path:
                    return await self._publish_with_image(client, text, image_path)

                data = await retry_on_transient(
                    self._fb_post, client,
                    f"{GRAPH_API_BASE}/{settings.facebook_page_id}/feed",
                    params={"access_token": self._token},
                    json={"message": text},
                    label="FB publish_text",
                )
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
        post_id = data.get("post_id") or data.get("id")
        return PublishResult(success=True, platform_post_id=post_id)

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

    async def delete_post(self, platform_post_id: str) -> tuple[bool, str]:
        """Delete a post from Facebook. Returns (success, detail)."""
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    f"{GRAPH_API_BASE}/{platform_post_id}",
                    params={"access_token": self._token},
                )
                data = resp.json()
                if data.get("success") or data is True:
                    return True, f"Deleted FB post {platform_post_id}"
                if "error" in data:
                    return False, f"API error: {data['error'].get('message', data['error'])}"
                return False, f"Unexpected response: {data}"
        except Exception as e:
            return False, f"Exception: {e}"

    async def get_new_messages(self) -> list[dict]:
        """Fetch recent comments on page posts (with retry on transient errors)."""
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                data = await retry_on_transient(
                    self._fb_get, client,
                    f"{GRAPH_API_BASE}/{settings.facebook_page_id}/feed",
                    params={
                        "access_token": self._token,
                        "fields": "id,comments.limit(50){id,from,message,created_time}",
                        # 16+ posts/day → cover ~2 days of feed so late comments
                        # still get picked up by the 15-min poll cycle.
                        "limit": 30,
                    },
                    label="FB get_messages",
                )
                if "error" in data:
                    logger.error("Facebook fetch messages error: %s", data["error"])
                    return []

                messages = []
                for post in data.get("data", []):
                    post_id = post.get("id", "")
                    for comment in post.get("comments", {}).get("data", []):
                        messages.append({
                            "platform_message_id": comment["id"],
                            "sender_id": comment.get("from", {}).get("id", ""),
                            "sender_name": comment.get("from", {}).get("name", ""),
                            "text": comment.get("message", ""),
                            "thread_id": f"fb_post_{post_id}",
                        })
                return messages
        except Exception:
            logger.exception("Facebook fetch messages failed")
            return []

    async def send_reply(self, platform_message_id: str, text: str) -> bool:
        try:
            await self._ensure_token()
            async with httpx.AsyncClient(timeout=30) as client:
                data = await retry_on_transient(
                    self._fb_post, client,
                    f"{GRAPH_API_BASE}/{platform_message_id}/comments",
                    params={"access_token": self._token},
                    json={"message": text},
                    label="FB send_reply",
                )
                if "error" in data:
                    err = data["error"]
                    code = err.get("code")
                    sub = err.get("error_subcode")
                    # Permanent: comment/post deleted or unsupported post target.
                    # Mark as replied to stop infinite retry loops.
                    if code in (33, 100, 200) or sub in (33,):
                        logger.warning(
                            "Facebook reply skipped (permanent error %s/%s, msg=%s): %s",
                            code, sub, platform_message_id, err.get("message"),
                        )
                        return True
                    logger.error("Facebook reply error: %s", err)
                    return False
                return True
        except Exception:
            logger.exception("Facebook reply failed")
            return False
