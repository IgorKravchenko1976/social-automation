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
    """Instagram publishing via Facebook Graph API (graph.facebook.com).

    Tries two strategies to find the right token + IG account ID:
    1. Instagram token + settings.instagram_user_id  (if user configured them)
    2. Facebook Page token + auto-discovered IG Business Account ID
    """
    platform = Platform.INSTAGRAM
    _platform_name = "facebook"
    _env_token_attr = "facebook_page_access_token"

    _publish_token: str | None = None
    _publish_ig_id: str | None = None

    async def _resolve_credentials(self) -> tuple[str, str] | None:
        """Find a working (token, ig_user_id) pair. Cached after first success."""
        if self._publish_token and self._publish_ig_id:
            return self._publish_token, self._publish_ig_id

        # Primary: FB Page token + auto-discovered IG Business Account
        fb_token = await self._get_fb_token()
        if fb_token and settings.facebook_page_id:
            ig_id = await self._discover_ig_from_page(fb_token)
            if ig_id:
                self._publish_token = fb_token
                self._publish_ig_id = ig_id
                logger.info("Instagram credentials: FB Page token + IG Business ID %s", ig_id)
                return self._publish_token, self._publish_ig_id

        # Fallback: dedicated Instagram token + configured user ID
        ig_token = await self._get_ig_token()
        if ig_token and settings.instagram_user_id:
            self._publish_token = ig_token
            self._publish_ig_id = settings.instagram_user_id
            logger.info("Instagram credentials: IG token + user_id %s", settings.instagram_user_id)
            return self._publish_token, self._publish_ig_id

        logger.error("No valid Instagram credentials found")
        return None

    async def _get_ig_token(self) -> str | None:
        from stats.token_renewer import get_active_token
        return await get_active_token("instagram") or settings.instagram_access_token or None

    async def _get_fb_token(self) -> str | None:
        from stats.token_renewer import get_active_token
        return await get_active_token("facebook") or settings.facebook_page_access_token or None

    async def _discover_ig_from_page(self, fb_token: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{GRAPH_API}/{settings.facebook_page_id}",
                    params={"access_token": fb_token, "fields": "instagram_business_account"},
                )
                data = resp.json()
                ig_id = data.get("instagram_business_account", {}).get("id")
                if ig_id:
                    return ig_id
                logger.warning("FB page has no linked IG Business Account")
        except Exception:
            logger.exception("IG Business Account discovery failed")
        return None

    # ── Publishing ───────────────────────────────────────────────────────

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        if not image_path:
            from content.media import get_image_for_post
            image_path = await get_image_for_post(
                text[:100], use_dalle=True, prefer_dalle=True,
            )
            if not image_path:
                return PublishResult(success=False, error="Instagram requires an image, none available")

        try:
            creds = await self._resolve_credentials()
            if not creds:
                return PublishResult(success=False, error="Instagram not configured (no valid token + IG account)")

            token, ig_id = creds
            async with httpx.AsyncClient(timeout=120) as client:
                return await self._publish_with_image(client, text, image_path, token, ig_id)
        except Exception as e:
            logger.exception("Instagram publish failed")
            return PublishResult(success=False, error=str(e))

    async def _publish_with_image(
        self, client: httpx.AsyncClient, caption: str, image_path: str,
        token: str, ig_id: str,
    ) -> PublishResult:
        image_url = await self._get_public_image_url(client, image_path)
        if not image_url:
            return PublishResult(success=False, error="Could not get public URL for image")

        resp = await client.post(
            f"{GRAPH_API}/{ig_id}/media",
            params={"access_token": token},
            data={"image_url": image_url, "caption": caption[:2200]},
        )
        data = resp.json()
        if "error" in data:
            err = data["error"].get("message", str(data["error"]))
            logger.error("Instagram create media error: %s", err)
            if self._publish_token and ("does not exist" in err.lower() or "access token" in err.lower()):
                logger.warning("Resetting cached IG credentials — will re-discover on next attempt")
                self._publish_token = None
                self._publish_ig_id = None
            return PublishResult(success=False, error=err)

        container_id = data.get("id")
        if not container_id:
            return PublishResult(success=False, error="No container ID returned")

        await asyncio.sleep(5)

        for _ in range(6):
            status_resp = await client.get(
                f"{GRAPH_API}/{container_id}",
                params={"access_token": token, "fields": "status_code"},
            )
            status_data = status_resp.json()
            status_code = status_data.get("status_code", "")
            if status_code == "FINISHED":
                break
            if status_code == "ERROR":
                return PublishResult(success=False, error=f"Container error: {status_data}")
            await asyncio.sleep(3)

        pub_resp = await client.post(
            f"{GRAPH_API}/{ig_id}/media_publish",
            params={"access_token": token},
            data={"creation_id": container_id},
        )
        pub_data = pub_resp.json()
        if "error" in pub_data:
            err = pub_data["error"].get("message", str(pub_data["error"]))
            logger.error("Instagram publish error: %s", err)
            return PublishResult(success=False, error=err)

        return PublishResult(success=True, platform_post_id=pub_data.get("id"))

    # ── Image hosting via Facebook CDN ───────────────────────────────────

    async def _get_public_image_url(self, client: httpx.AsyncClient, image_path: str) -> str | None:
        fb_token = await self._get_fb_token()
        if fb_token and settings.facebook_page_id:
            return await self._upload_via_facebook(client, image_path, fb_token)
        logger.error("Cannot get public URL for image (need Facebook credentials)")
        return None

    async def _upload_via_facebook(
        self, client: httpx.AsyncClient, image_path: str, fb_token: str,
    ) -> str | None:
        try:
            with open(image_path, "rb") as f:
                resp = await client.post(
                    f"{GRAPH_API}/{settings.facebook_page_id}/photos",
                    params={"access_token": fb_token},
                    data={"published": "false"},
                    files={"source": ("image.jpg", f, "image/jpeg")},
                )
            data = resp.json()
            if "error" in data:
                logger.warning("Facebook photo upload failed: %s",
                               data["error"].get("message", data["error"]))
                return None

            photo_id = data.get("id")
            if not photo_id:
                return None

            img_resp = await client.get(
                f"{GRAPH_API}/{photo_id}",
                params={"access_token": fb_token, "fields": "images"},
            )
            images = img_resp.json().get("images", [])
            if images:
                url = images[0].get("source", "")
                logger.info("Public image URL via Facebook CDN: %s", url[:80])
                return url
            return None
        except Exception:
            logger.exception("Facebook image upload for Instagram failed")
            return None

    async def delete_post(self, platform_post_id: str) -> tuple[bool, str]:
        """Attempt to delete a media post from Instagram via Graph API.

        NOTE: Instagram Graph API has very limited delete support.
        Most media types cannot be deleted via API — manual deletion required.
        """
        try:
            creds = await self._resolve_credentials()
            if not creds:
                return False, "Немає токена — видаліть вручну з Instagram"
            token, _ = creds
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    f"{GRAPH_API}/{platform_post_id}",
                    params={"access_token": token},
                )
                data = resp.json()
                if data.get("success") or data is True:
                    return True, f"Deleted IG media {platform_post_id}"
                if "error" in data:
                    err = data["error"].get("message", str(data["error"]))
                    if "Unsupported delete" in err or "does not support" in err:
                        return False, (
                            f"Instagram API НЕ ПІДТРИМУЄ видалення постів. "
                            f"ID: {platform_post_id} — ВИДАЛІТЬ ВРУЧНУ з Instagram."
                        )
                    return False, f"API error: {err}"
                return False, f"Unexpected: {data}"
        except Exception as e:
            return False, f"Exception: {e}"

    # ── Video, messages, replies ─────────────────────────────────────────

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        return PublishResult(success=False, error="Instagram video publishing not yet implemented")

    async def get_new_messages(self) -> list[dict]:
        try:
            creds = await self._resolve_credentials()
            if not creds:
                return []
            token, ig_id = creds
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{GRAPH_API}/{ig_id}/media",
                    params={
                        "access_token": token,
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
        try:
            creds = await self._resolve_credentials()
            if not creds:
                return False
            token, _ = creds
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{GRAPH_API}/{platform_message_id}/replies",
                    params={"access_token": token},
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
