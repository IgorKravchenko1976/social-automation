from __future__ import annotations

from enum import Enum


class Platform(str, Enum):
    TELEGRAM = "telegram"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


def configured_platforms() -> list[Platform]:
    """Return only platforms that have real (non-placeholder) credentials."""
    from config.settings import settings, is_placeholder

    configured = []
    if not is_placeholder(settings.telegram_bot_token) and not is_placeholder(settings.telegram_channel_id):
        configured.append(Platform.TELEGRAM)
    if not is_placeholder(settings.facebook_page_id) and not is_placeholder(settings.facebook_page_access_token):
        configured.append(Platform.FACEBOOK)
    has_ig_token = not is_placeholder(settings.instagram_user_id) and not is_placeholder(settings.instagram_access_token)
    has_fb_for_ig = not is_placeholder(settings.facebook_page_id) and not is_placeholder(settings.facebook_page_access_token)
    if has_ig_token or has_fb_for_ig:
        configured.append(Platform.INSTAGRAM)
    if not is_placeholder(settings.twitter_bearer_token) and not is_placeholder(settings.twitter_api_key):
        configured.append(Platform.TWITTER)
    if not is_placeholder(settings.tiktok_access_token):
        configured.append(Platform.TIKTOK)
    return configured


def get_platform_instance(platform: Platform):
    """Create a platform adapter instance."""
    from platforms.telegram import TelegramPlatform
    from platforms.facebook import FacebookPlatform
    from platforms.twitter import TwitterPlatform
    from platforms.instagram import InstagramPlatform
    from platforms.tiktok import TikTokPlatform

    _registry = {
        Platform.TELEGRAM: TelegramPlatform,
        Platform.FACEBOOK: FacebookPlatform,
        Platform.TWITTER: TwitterPlatform,
        Platform.INSTAGRAM: InstagramPlatform,
        Platform.TIKTOK: TikTokPlatform,
    }
    return _registry[platform]()


FACEBOOK_GRAPH_API = "https://graph.facebook.com/v21.0"
INSTAGRAM_GRAPH_API = "https://graph.instagram.com/v21.0"

PLATFORM_LABELS = {
    "telegram": "Telegram",
    "facebook": "Facebook",
    "twitter": "X / Twitter",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

PLATFORM_COLORS = {
    "telegram": "#2AABEE",
    "facebook": "#1877F2",
    "twitter": "#E7E9EA",
    "instagram": "#E4405F",
    "tiktok": "#00F2EA",
}

PLATFORM_ICONS = {
    "telegram": "TG", "facebook": "FB", "instagram": "IG",
    "twitter": "X", "tiktok": "TT",
}

EMPTY_STATS = dict(subscribers=0, posts=0, comments=0, views=0, likes=0, dislikes=0)

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
