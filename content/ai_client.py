"""Shared OpenAI async client singleton."""
from __future__ import annotations

from typing import Optional

from openai import AsyncOpenAI

from config.settings import settings

_client: Optional[AsyncOpenAI] = None


def get_client() -> AsyncOpenAI:
    """Return (and lazily create) the shared AsyncOpenAI client."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client
