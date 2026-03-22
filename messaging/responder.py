from __future__ import annotations

import logging

from sqlalchemy import select, or_, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform, get_platform_instance
from content.generator import generate_auto_reply
from db.database import async_session
from db.models import Message, MessageDirection, Publication, Post

logger = logging.getLogger(__name__)

MAX_REPLIES_PER_AUTHOR = 4
FAREWELL_MESSAGE = (
    "Дякуємо за спілкування! 🙌\n"
    "Слідкуйте за нашими оновленнями:\n"
    "🌍 Сайт: www.im-in.net\n"
    "📱 Telegram: @iminapp_bot\n"
    "Якщо залишились питання — напишіть нам на сайті!"
)


async def count_replies_to_sender(
    session: AsyncSession, platform: str, sender_id: str, thread_id: str = "",
) -> int:
    """Count how many times we've already replied to this sender in this thread.

    The limit is per author per thread (post or chat session), not global.
    If thread_id is empty, falls back to counting all replies on the platform.
    """
    filters = [
        Message.platform == platform,
        Message.sender_id == sender_id,
        Message.direction == MessageDirection.INCOMING,
        Message.replied == True,
    ]
    if thread_id:
        filters.append(Message.thread_id == thread_id)
    result = await session.execute(
        select(sa_func.count(Message.id)).where(*filters)
    )
    return result.scalar() or 0


async def respond_to_pending_messages() -> int:
    """Process all unresponded incoming messages, generate AI replies, and send them."""
    replied_count = 0
    fail_count = 0

    async with async_session() as session:
        result = await session.execute(
            select(Message).where(
                Message.direction == MessageDirection.INCOMING,
                Message.replied == False,
                or_(Message.category != "spam", Message.category.is_(None)),
            )
        )
        messages = result.scalars().all()

        if messages:
            logger.info("=== REPLY === Processing %d pending messages", len(messages))

        for msg in messages:
            try:
                if not msg.text:
                    msg.replied = True
                    continue

                thread_id = msg.thread_id or ""
                if not thread_id:
                    from datetime import date
                    thread_id = f"{msg.platform}_{date.today().isoformat()}"
                    msg.thread_id = thread_id

                prior_replies = await count_replies_to_sender(
                    session, msg.platform, msg.sender_id or "", thread_id,
                )

                if prior_replies > MAX_REPLIES_PER_AUTHOR:
                    msg.replied = True
                    logger.info("Reply limit exceeded for sender=%s thread=%s (%d), skipping",
                                msg.sender_id, thread_id, prior_replies)
                    continue

                platform = Platform(msg.platform)

                if prior_replies == MAX_REPLIES_PER_AUTHOR:
                    reply_text = FAREWELL_MESSAGE
                    category = "farewell"
                else:
                    post_context = await _find_post_context(session, msg)
                    reply_text, category = await generate_auto_reply(
                        incoming_message=msg.text,
                        platform=platform,
                        sender_name=msg.sender_name or "",
                        post_context=post_context,
                        prior_replies=prior_replies,
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
                        thread_id=thread_id,
                    )
                    session.add(outgoing)
                    msg.replied = True
                    replied_count += 1
                    logger.info("=== REPLY === OK msg_id=%s %s [%s]", msg.id, msg.platform, category)
                else:
                    fail_count += 1
                    logger.warning("=== REPLY === SEND FAILED msg_id=%s %s", msg.id, msg.platform)

            except Exception:
                fail_count += 1
                logger.exception("=== REPLY === ERROR msg_id=%s %s", msg.id, msg.platform)

        await session.commit()

    if messages:
        logger.info("=== REPLY === Done: %d replied, %d failed, %d still pending",
                     replied_count, fail_count, len(messages) - replied_count - fail_count)

    return replied_count


async def _find_post_context(session: AsyncSession, msg: Message) -> str:
    """Try to find the original post content for a comment/reply.

    Looks for the most recent published post on the same platform
    (within the last day) as a likely context for the comment.
    """
    try:
        from config.settings import get_today_start_utc
        today = get_today_start_utc()

        result = await session.execute(
            select(Post)
            .join(Publication)
            .where(
                Publication.platform == msg.platform,
                Publication.status == "published",
                Post.created_at >= today,
            )
            .order_by(Publication.published_at.desc())
            .limit(1)
        )
        post = result.scalar_one_or_none()
        if post:
            parts = []
            if post.title:
                parts.append(post.title)
            if post.content_raw:
                parts.append(post.content_raw[:1000])
            return "\n".join(parts)
    except Exception:
        logger.warning("Could not find post context for msg_id=%s", msg.id)
    return ""


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
