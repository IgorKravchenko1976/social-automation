from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform, get_platform_instance
from db.database import async_session
from db.models import Message, MessageDirection

logger = logging.getLogger(__name__)

MONITORED_PLATFORMS = [Platform.TELEGRAM, Platform.FACEBOOK, Platform.TWITTER, Platform.INSTAGRAM]


async def poll_all_messages() -> list[Message]:
    """Poll all platforms for new messages and store them in the DB."""
    new_messages = []
    poll_errors: list[str] = []

    async with async_session() as session:
        for platform in MONITORED_PLATFORMS:
            try:
                adapter = get_platform_instance(platform)
                raw_messages = await adapter.get_new_messages()

                new_count = 0
                for raw in raw_messages:
                    exists = await _message_exists(
                        session, platform.value, raw["platform_message_id"]
                    )
                    if exists:
                        continue

                    msg = Message(
                        platform=platform.value,
                        platform_message_id=raw["platform_message_id"],
                        sender_id=raw.get("sender_id", ""),
                        sender_name=raw.get("sender_name", ""),
                        direction=MessageDirection.INCOMING,
                        text=raw.get("text", ""),
                        replied=False,
                    )
                    session.add(msg)
                    new_messages.append(msg)
                    new_count += 1

                if new_count > 0:
                    logger.info("=== POLL === %s: %d new messages stored", platform.value, new_count)
            except Exception:
                poll_errors.append(platform.value)
                logger.exception("=== POLL === FAILED to poll %s", platform.value)

        await session.commit()

    if poll_errors:
        logger.error("=== POLL === Errors on platforms: %s", ", ".join(poll_errors))
    logger.info("=== POLL === Total new messages: %d", len(new_messages))

    return new_messages


async def _message_exists(session: AsyncSession, platform: str, platform_msg_id: str) -> bool:
    result = await session.execute(
        select(Message.id).where(
            Message.platform == platform,
            Message.platform_message_id == platform_msg_id,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None
