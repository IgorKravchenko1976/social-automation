"""Telegram platform adapter (publish, reply) and bot lifecycle."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult
from platforms.telegram_api import api_url, ensure_client, request as tg_request

logger = logging.getLogger(__name__)

_polling_task: Optional[asyncio.Task] = None


class TelegramPlatform(BasePlatform):
    platform = Platform.TELEGRAM

    async def _save_channel_post(self, result_msg: dict, text: str) -> None:
        """Save published channel post to Message table for view tracking."""
        from db.database import async_session
        from db.models import Message as MsgModel, MessageDirection
        try:
            message_id = result_msg.get("message_id")
            chat = result_msg.get("chat", {})
            views = result_msg.get("views", 0) or 0
            async with async_session() as session:
                from sqlalchemy import select
                existing = await session.execute(
                    select(MsgModel).where(
                        MsgModel.platform == "telegram",
                        MsgModel.platform_message_id == str(message_id),
                        MsgModel.category == "channel_post",
                    )
                )
                if not existing.scalar_one_or_none():
                    session.add(MsgModel(
                        platform="telegram",
                        platform_message_id=str(message_id),
                        sender_id="channel",
                        sender_name=chat.get("title", "channel"),
                        direction=MessageDirection.OUTGOING,
                        text=(text or "")[:500],
                        category="channel_post",
                        view_count=views,
                    ))
                    await session.commit()
                    logger.info("Saved channel post #%s to DB for view tracking", message_id)
        except Exception:
            logger.exception("Failed to save channel post to Message table")

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            if image_path:
                client = await ensure_client()
                with open(image_path, "rb") as photo:
                    resp = await client.post(
                        api_url("sendPhoto"),
                        data={"chat_id": settings.telegram_channel_id, "caption": text[:1024]},
                        files={"photo": photo},
                    )
                data = resp.json()
            else:
                data = await tg_request(
                    "sendMessage",
                    chat_id=settings.telegram_channel_id,
                    text=text,
                    disable_web_page_preview=False,
                )
            if data.get("ok"):
                msg_id = str(data["result"]["message_id"])
                await self._save_channel_post(data["result"], text)
                return PublishResult(success=True, platform_post_id=msg_id)
            return PublishResult(success=False, error=str(data))
        except Exception as e:
            logger.exception("Telegram publish failed")
            return PublishResult(success=False, error=str(e))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            client = await ensure_client()
            with open(video_path, "rb") as video:
                resp = await client.post(
                    api_url("sendVideo"),
                    data={"chat_id": settings.telegram_channel_id, "caption": text[:1024]},
                    files={"video": video},
                )
            data = resp.json()
            if data.get("ok"):
                msg_id = str(data["result"]["message_id"])
                await self._save_channel_post(data["result"], text)
                return PublishResult(success=True, platform_post_id=msg_id)
            return PublishResult(success=False, error=str(data))
        except Exception as e:
            logger.exception("Telegram video publish failed")
            return PublishResult(success=False, error=str(e))

    async def delete_post(self, platform_post_id: str) -> tuple[bool, str]:
        """Delete a message from the Telegram channel. Returns (success, detail)."""
        try:
            data = await tg_request(
                "deleteMessage",
                chat_id=settings.telegram_channel_id,
                message_id=int(platform_post_id),
            )
            if data.get("ok"):
                return True, f"Deleted message #{platform_post_id}"
            return False, f"API error: {data.get('description', data)}"
        except Exception as e:
            return False, f"Exception: {e}"

    async def send_reply(self, chat_id: str, text: str, reply_to: Optional[int] = None) -> bool:
        try:
            params = {"chat_id": int(chat_id), "text": text}
            if reply_to:
                params["reply_to_message_id"] = reply_to
            data = await tg_request("sendMessage", **params)
            return data.get("ok", False)
        except Exception:
            logger.exception("Telegram reply failed")
            return False


# ── Bot lifecycle ─────────────────────────────────────────────────────────────

async def start_telegram_bot() -> None:
    global _polling_task

    if not settings.telegram_bot_token:
        logger.warning("Telegram bot token not set, skipping bot startup")
        return

    from platforms.telegram_bot import polling_loop
    _polling_task = asyncio.create_task(polling_loop())
    logger.info("Telegram bot polling started")


async def stop_telegram_bot() -> None:
    global _polling_task
    if _polling_task:
        logger.info("Stopping Telegram bot...")
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
        _polling_task = None

    from platforms.telegram_api import close_client
    await close_client()
