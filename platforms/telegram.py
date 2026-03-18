from __future__ import annotations

import logging
from typing import Optional

from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult

logger = logging.getLogger(__name__)

_application: Optional[Application] = None


class TelegramPlatform(BasePlatform):
    platform = Platform.TELEGRAM

    def __init__(self):
        self._bot: Optional[Bot] = None

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=settings.telegram_bot_token)
        return self._bot

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            if image_path:
                with open(image_path, "rb") as photo:
                    msg = await self.bot.send_photo(
                        chat_id=settings.telegram_channel_id,
                        photo=photo,
                        caption=text[:1024],
                    )
            else:
                msg = await self.bot.send_message(
                    chat_id=settings.telegram_channel_id,
                    text=text,
                    disable_web_page_preview=False,
                )
            return PublishResult(success=True, platform_post_id=str(msg.message_id))
        except Exception as e:
            logger.exception("Telegram publish failed")
            return PublishResult(success=False, error=str(e))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            with open(video_path, "rb") as video:
                msg = await self.bot.send_video(
                    chat_id=settings.telegram_channel_id,
                    video=video,
                    caption=text[:1024],
                )
            return PublishResult(success=True, platform_post_id=str(msg.message_id))
        except Exception as e:
            logger.exception("Telegram video publish failed")
            return PublishResult(success=False, error=str(e))

    async def send_reply(self, chat_id: str, text: str, reply_to: Optional[int] = None) -> bool:
        try:
            await self.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                reply_to_message_id=reply_to,
            )
            return True
        except Exception:
            logger.exception("Telegram reply failed")
            return False


# ── Telegram Bot Handlers (direct messages + channel comments) ────────────────

async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! 👋 Я бот додатку I'M IN — додатку для мандрівників.\n\n"
        "Напиши мені будь-яке питання про додаток, і я відповім!\n\n"
        "🌍 Сайт: im-in.net\n"
        "📱 Скоро в App Store!"
    )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle direct messages to the bot AND comments in discussion group."""
    message = update.message
    if not message or not message.text:
        return

    user = message.from_user
    sender_name = user.first_name if user else ""
    chat_id = message.chat_id
    text = message.text

    logger.info(
        "Telegram message from %s (chat %s): %s",
        sender_name, chat_id, text[:100],
    )

    # Save to DB
    try:
        from db.database import async_session
        from db.models import Message, MessageDirection

        async with async_session() as session:
            msg = Message(
                platform="telegram",
                platform_message_id=str(message.message_id),
                sender_id=str(user.id) if user else "",
                sender_name=sender_name,
                direction=MessageDirection.INCOMING,
                text=text,
                replied=False,
            )
            session.add(msg)
            await session.commit()
    except Exception:
        logger.exception("Failed to save Telegram message to DB")

    # Generate AI reply
    try:
        from content.generator import generate_auto_reply

        reply_text, category = await generate_auto_reply(
            incoming_message=text,
            platform=Platform.TELEGRAM,
            sender_name=sender_name,
        )

        if category == "spam":
            logger.info("Skipping spam from %s", sender_name)
            return

        await message.reply_text(reply_text)

        # Save outgoing to DB
        try:
            from db.database import async_session
            from db.models import Message, MessageDirection

            async with async_session() as session:
                out_msg = Message(
                    platform="telegram",
                    platform_message_id=None,
                    sender_id="bot",
                    sender_name="bot",
                    direction=MessageDirection.OUTGOING,
                    text=reply_text,
                    category=category,
                    replied=True,
                )
                session.add(out_msg)
                await session.commit()
        except Exception:
            logger.exception("Failed to save outgoing message to DB")

        if category == "human_needed":
            logger.warning("Message from %s needs human attention: %s", sender_name, text[:200])

    except Exception:
        logger.exception("Failed to generate reply for Telegram message")
        await message.reply_text(
            "Дякую за повідомлення! Наша команда скоро відповість. 🙏"
        )


async def start_telegram_bot() -> None:
    """Start the Telegram bot with polling (runs in background)."""
    global _application

    if not settings.telegram_bot_token:
        logger.warning("Telegram bot token not set, skipping bot startup")
        return

    _application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    _application.add_handler(CommandHandler("start", _handle_start))
    _application.add_handler(CommandHandler("help", _handle_start))
    _application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message)
    )

    logger.info("Starting Telegram bot polling...")
    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot is listening for messages!")


async def stop_telegram_bot() -> None:
    global _application
    if _application:
        logger.info("Stopping Telegram bot...")
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()
        _application = None
