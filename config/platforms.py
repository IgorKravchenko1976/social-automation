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


_platform_registry: dict | None = None


def get_platform_instance(platform: Platform):
    """Create a platform adapter instance."""
    global _platform_registry
    if _platform_registry is None:
        from platforms.telegram import TelegramPlatform
        from platforms.facebook import FacebookPlatform
        from platforms.twitter import TwitterPlatform
        from platforms.instagram import InstagramPlatform
        from platforms.tiktok import TikTokPlatform

        _platform_registry = {
            Platform.TELEGRAM: TelegramPlatform,
            Platform.FACEBOOK: FacebookPlatform,
            Platform.TWITTER: TwitterPlatform,
            Platform.INSTAGRAM: InstagramPlatform,
            Platform.TIKTOK: TikTokPlatform,
        }
    return _platform_registry[platform]()


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

# Per-platform anti-ban / shadowban daily caps for OUTGOING publications.
# These are conservative thresholds well below the platforms' hard API limits;
# the goal is to keep accounts in good standing, not to maximise throughput.
#   Telegram: TG does not penalise high volume — 50 is a safety upper bound.
#   Facebook: ~10/day keeps reach healthy; FB throttles spammy pages aggressively.
#   Instagram: hard API ceiling is 25/24h; shadowban risk rises sharply >10/day,
#              so we cap at 8 with ≥1h spacing implicit via slot/cycle scheduling.
#   Twitter: well below Free tier 50/day write cap.
#   TikTok: bot rarely posts video — keep low.
PLATFORM_DAILY_LIMITS = {
    Platform.TELEGRAM: 50,
    Platform.FACEBOOK: 10,
    Platform.INSTAGRAM: 8,
    Platform.TWITTER: 30,
    Platform.TIKTOK: 5,
}

# Per-platform minimum spacing (minutes) between consecutive PUBLISHED posts.
# Prevents bursts that look like spam to FB / IG anti-spam systems.
# When the publisher cycle (15 min) finds the spacing is too tight, the
# publication is kept QUEUED and re-attempted on the next cycle.
PLATFORM_MIN_SPACING_MINUTES = {
    Platform.TELEGRAM: 0,    # TG tolerates frequent posts
    Platform.FACEBOOK: 60,   # 60 min keeps reach healthy
    Platform.INSTAGRAM: 60,  # 60 min keeps shadowban risk low
    Platform.TWITTER: 10,
    Platform.TIKTOK: 30,
}

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
