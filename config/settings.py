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

    # Email reports
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    report_email_to: str = ""

    # Scheduling
    post_times: str = "09:00,13:00,18:00"
    timezone: str = "Europe/Kyiv"

    # Webhook
    webhook_base_url: str = "http://localhost:8000"
    webhook_secret: str = "change-me"

    # Database
    database_url: str = Field(
        default_factory=lambda: f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'social.db'}"
    )

    # Media cache
    media_cache_dir: str = Field(
        default_factory=lambda: str(BASE_DIR / "media_cache")
    )

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def post_schedule(self) -> list[str]:
        return [t.strip() for t in self.post_times.split(",")]


settings = Settings()
