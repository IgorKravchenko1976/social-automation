from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic import Field

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""

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

    # Instagram
    instagram_username: str = ""
    instagram_password: str = ""

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
    post_times: str = "09:00,13:00,18:00"
    timezone: str = "Europe/Kyiv"

    # Webhook
    webhook_base_url: str = "http://localhost:8000"
    webhook_secret: str = "change-me"

    # Persistent data directory (mount Railway Volume here)
    data_dir: str = "/data"

    # Database
    database_url: str = ""

    # Media cache
    media_cache_dir: str = ""

    def model_post_init(self, __context) -> None:
        import pathlib
        pathlib.Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{self.data_dir}/social.db"
        if not self.media_cache_dir:
            self.media_cache_dir = f"{self.data_dir}/media_cache"
        pathlib.Path(self.media_cache_dir).mkdir(parents=True, exist_ok=True)

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def post_schedule(self) -> list[str]:
        return [t.strip() for t in self.post_times.split(",")]


settings = Settings()
