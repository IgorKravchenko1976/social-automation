from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult

logger = logging.getLogger(__name__)

TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"


class TikTokPlatform(BasePlatform):
    platform = Platform.TIKTOK

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.tiktok_access_token}",
            "Content-Type": "application/json",
        }

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        return PublishResult(success=False, error="TikTok requires video content")

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        """Publish via TikTok Content Posting API (direct post)."""
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                # Step 1: Init upload
                init_resp = await client.post(
                    f"{TIKTOK_API_BASE}/post/publish/video/init/",
                    headers=self._headers,
                    json={
                        "post_info": {
                            "title": text[:150],
                            "privacy_level": "PUBLIC_TO_EVERYONE",
                            "disable_duet": False,
                            "disable_comment": False,
                            "disable_stitch": False,
                        },
                        "source_info": {
                            "source": "FILE_UPLOAD",
                            "video_size": self._get_file_size(video_path),
                            "chunk_size": self._get_file_size(video_path),
                            "total_chunk_count": 1,
                        },
                    },
                )
                init_resp.raise_for_status()
                init_data = init_resp.json()

                upload_url = init_data.get("data", {}).get("upload_url")
                publish_id = init_data.get("data", {}).get("publish_id")

                if not upload_url:
                    return PublishResult(
                        success=False,
                        error=f"No upload URL returned: {init_data}",
                    )

                # Step 2: Upload video
                with open(video_path, "rb") as f:
                    video_data = f.read()

                upload_resp = await client.put(
                    upload_url,
                    content=video_data,
                    headers={
                        "Content-Range": f"bytes 0-{len(video_data) - 1}/{len(video_data)}",
                        "Content-Type": "video/mp4",
                    },
                )
                upload_resp.raise_for_status()

                return PublishResult(success=True, platform_post_id=publish_id)
        except Exception as e:
            logger.exception("TikTok publish failed")
            return PublishResult(success=False, error=str(e))

    @staticmethod
    def _get_file_size(path: str) -> int:
        from pathlib import Path
        return Path(path).stat().st_size
