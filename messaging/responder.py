from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform
from content.generator import generate_auto_reply
from db.database import async_session
from db.models import Message, MessageDirection
from scheduler.jobs import get_platform_instance

logger = logging.getLogger(__name__)


async def respond_to_pending_messages() -> int:
    """Process all unresponded incoming messages, generate AI replies, and send them."""
    replied_count = 0

    async with async_session() as session:
        result = await session.execute(
            select(Message).where(
                Message.direction == MessageDirection.INCOMING,
                Message.replied == False,
                Message.category != "spam",
            )
        )
        messages = result.scalars().all()

        for msg in messages:
            try:
                if not msg.text:
                    msg.replied = True
                    continue

                platform = Platform(msg.platform)
                reply_text, category = await generate_auto_reply(
                    incoming_message=msg.text,
                    platform=platform,
                    sender_name=msg.sender_name or "",
                )

                msg.category = category

                if category == "spam":
                    msg.replied = True
                    logger.info("Skipping spam message %s", msg.id)
                    continue

                if category == "human_needed":
                    logger.warning(
                        "Message %s on %s requires human attention: %s",
                        msg.id, msg.platform, msg.text[:100],
                    )
                    await _notify_admin(msg)

                adapter = get_platform_instance(platform)
                sent = await adapter.send_reply(msg.platform_message_id, reply_text)

                if sent:
                    outgoing = Message(
                        platform=msg.platform,
                        platform_message_id=None,
                        sender_id="bot",
                        sender_name="bot",
                        direction=MessageDirection.OUTGOING,
                        text=reply_text,
                        category=category,
                        replied=True,
                    )
                    session.add(outgoing)
                    msg.replied = True
                    replied_count += 1
                    logger.info("Replied to message %s on %s", msg.id, msg.platform)
                else:
                    logger.warning("Failed to send reply for message %s", msg.id)

            except Exception:
                logger.exception("Error replying to message %s", msg.id)

        await session.commit()

    return replied_count


async def _notify_admin(msg: Message) -> None:
    """Send important messages to admin via Telegram bot."""
    try:
        from config.settings import settings
        if not settings.telegram_bot_token:
            return

        tg = get_platform_instance(Platform.TELEGRAM)
        notification = (
            f"⚠️ Повідомлення потребує уваги!\n"
            f"Платформа: {msg.platform}\n"
            f"Від: {msg.sender_name} ({msg.sender_id})\n"
            f"Текст: {msg.text[:500]}"
        )
        await tg.bot.send_message(
            chat_id=settings.telegram_channel_id,
            text=notification,
        )
    except Exception:
        logger.exception("Failed to notify admin")
