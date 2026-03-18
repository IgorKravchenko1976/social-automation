from __future__ import annotations

from enum import Enum


class Platform(str, Enum):
    TELEGRAM = "telegram"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


PLATFORM_LIMITS = {
    Platform.TELEGRAM: {
        "max_text_length": 4096,
        "supports_images": True,
        "supports_video": True,
        "supports_links": True,
        "hashtags": False,
    },
    Platform.FACEBOOK: {
        "max_text_length": 63206,
        "supports_images": True,
        "supports_video": True,
        "supports_links": True,
        "hashtags": True,
    },
    Platform.TWITTER: {
        "max_text_length": 280,
        "supports_images": True,
        "supports_video": True,
        "supports_links": True,
        "hashtags": True,
    },
    Platform.INSTAGRAM: {
        "max_text_length": 2200,
        "supports_images": True,
        "supports_video": True,
        "supports_links": False,
        "hashtags": True,
    },
    Platform.TIKTOK: {
        "max_text_length": 2200,
        "supports_images": False,
        "supports_video": True,
        "supports_links": False,
        "hashtags": True,
    },
}
