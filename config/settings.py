from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session: str = ""  # Telethon StringSession for view counts

    # Facebook
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""
    facebook_app_id: str = ""
    facebook_app_secret: str = ""

    # Twitter / X
    twitter_api_key: str = ""
    twitter_api_secret: str = ""
    twitter_access_token: str = ""
    twitter_access_token_secret: str = ""
    twitter_bearer_token: str = ""

    # Instagram (Graph API)
    instagram_user_id: str = ""
    instagram_access_token: str = ""

    # TikTok
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_access_token: str = ""

    # Pexels
    pexels_api_key: str = ""

    # App context for AI
    app_name: str = "MyApp"
    app_description: str = "An awesome application"
    app_website: str = "https://example.com"

    # Email reports (Resend HTTP API — works on Railway without SMTP)
    resend_api_key: str = ""
    report_email_from: str = "I'M IN Reports <onboarding@resend.dev>"
    report_email_to: str = ""

    # Scheduling
    post_times: str = "08:00,10:00,12:00,15:00,18:00"
    timezone: str = "Europe/Kyiv"

    # Webhook
    webhook_base_url: str = "http://localhost:8000"
    webhook_secret: str = "change-me"

    # Admin API key (required for all write/admin endpoints)
    admin_api_key: str = ""

    # VPS for blog sync (optional — if not set, blog pages stay on Railway only)
    vps_ssh_host: str = ""
    vps_ssh_port: int = 22
    vps_ssh_user: str = "dmytros"
    vps_ssh_password: str = ""
    vps_ssh_key: str = ""
    vps_blog_path: str = "/var/www/im-in.net/html/blog"

    # IM-IN API #2: публікація блогу без SFTP (пріоритет над VPS_SSH_* якщо ключ заданий).
    marketing_publish_api_base: str = ""
    marketing_publish_api_key: str = ""
    # Шаблони кореня сайту (blog.html тощо). Порожньо = GitLab raw imin-backend web/marketing.
    marketing_template_base_url: str = ""

    # IM-IN Backend Geo-Research API (bot fetches tasks and submits results)
    imin_backend_api_base: str = ""
    imin_backend_sync_key: str = ""

    # Web search AI for POI research (fallback chain: Perplexity -> Tavily -> Brave)
    perplexity_api_key: str = ""
    tavily_api_key: str = ""
    brave_search_api_key: str = ""

    # ElevenLabs Multilingual v2 TTS — voice narration for City Pulse cards.
    # Bot reads city_event title+description, ElevenLabs returns MP3, we
    # upload via backend to B2 (event-audio bucket).
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "9BWtsMINqrJLrRacOk9x"  # Aria — universal female
    elevenlabs_model_id: str = "eleven_multilingual_v2"

    # Persistent data directory (mount Railway Volume here)
    data_dir: str = "/data"

    # Operational mode:
    #   "full"        — schedulers, Telegram polling, all jobs (VPS deploy)
    #   "monitoring"  — FastAPI only, no scheduler/polling/jobs (Railway)
    # Default is intentionally "monitoring" so a Railway deploy never
    # accidentally double-publishes if BOT_MODE is missed in env.
    bot_mode: str = "monitoring"

    # Database
    database_url: str = ""

    # Media cache
    media_cache_dir: str = ""

    def model_post_init(self, __context) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{self.data_dir}/social.db"
        if not self.media_cache_dir:
            self.media_cache_dir = f"{self.data_dir}/media_cache"
        Path(self.media_cache_dir).mkdir(parents=True, exist_ok=True)

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def post_schedule(self) -> list[str]:
        return [t.strip() for t in self.post_times.split(",")]


settings = Settings()


# ── Shared date/time helpers (used throughout the project) ────────────────────

def get_today_start_utc() -> "datetime":
    """Return midnight of today (in project timezone) as a naive UTC datetime."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_start.astimezone(timezone.utc).replace(tzinfo=None)


def get_now_local() -> "datetime":
    """Return the current time in project timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.timezone))


def parse_slot_time(time_str: str, now_local: "datetime") -> "datetime":
    """Parse 'HH:MM' and return a datetime for that time today."""
    hour, minute = map(int, time_str.split(":"))
    return now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def is_placeholder(value: str) -> bool:
    """Check if a credential value is empty or a placeholder."""
    return not value or value.startswith("your-")


def ensure_utc(dt: "datetime") -> "datetime":
    """Ensure a datetime has UTC tzinfo (handles naive datetimes from SQLite)."""
    from datetime import timezone
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
