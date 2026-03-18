from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import httpx

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult

logger = logging.getLogger(__name__)

_polling_task: Optional[asyncio.Task] = None
_http_client: Optional[httpx.AsyncClient] = None

API_BASE = "https://api.telegram.org/bot{token}"


def _api_url(method: str) -> str:
    return f"{API_BASE.format(token=settings.telegram_bot_token)}/{method}"


class TelegramPlatform(BasePlatform):
    platform = Platform.TELEGRAM

    async def _request(self, method: str, **params):
        global _http_client
        if _http_client is None:
            _http_client = httpx.AsyncClient(timeout=60)
        resp = await _http_client.post(_api_url(method), json=params)
        return resp.json()

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            if image_path:
                global _http_client
                if _http_client is None:
                    _http_client = httpx.AsyncClient(timeout=60)
                with open(image_path, "rb") as photo:
                    resp = await _http_client.post(
                        _api_url("sendPhoto"),
                        data={"chat_id": settings.telegram_channel_id, "caption": text[:1024]},
                        files={"photo": photo},
                    )
                data = resp.json()
            else:
                data = await self._request(
                    "sendMessage",
                    chat_id=settings.telegram_channel_id,
                    text=text,
                    disable_web_page_preview=False,
                )
            if data.get("ok"):
                return PublishResult(success=True, platform_post_id=str(data["result"]["message_id"]))
            return PublishResult(success=False, error=str(data))
        except Exception as e:
            logger.exception("Telegram publish failed")
            return PublishResult(success=False, error=str(e))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            global _http_client
            if _http_client is None:
                _http_client = httpx.AsyncClient(timeout=60)
            with open(video_path, "rb") as video:
                resp = await _http_client.post(
                    _api_url("sendVideo"),
                    data={"chat_id": settings.telegram_channel_id, "caption": text[:1024]},
                    files={"video": video},
                )
            data = resp.json()
            if data.get("ok"):
                return PublishResult(success=True, platform_post_id=str(data["result"]["message_id"]))
            return PublishResult(success=False, error=str(data))
        except Exception as e:
            logger.exception("Telegram video publish failed")
            return PublishResult(success=False, error=str(e))

    async def send_reply(self, chat_id: str, text: str, reply_to: Optional[int] = None) -> bool:
        try:
            params = {"chat_id": int(chat_id), "text": text}
            if reply_to:
                params["reply_to_message_id"] = reply_to
            data = await self._request("sendMessage", **params)
            return data.get("ok", False)
        except Exception:
            logger.exception("Telegram reply failed")
            return False


# ── Manual polling loop ──────────────────────────────────────────────────────

async def _process_message(message: dict) -> None:
    """Process an incoming message and generate AI reply."""
    text = message.get("text", "")
    chat_id = message["chat"]["id"]
    from_user = message.get("from", {})
    sender_name = from_user.get("first_name", "")
    message_id = message.get("message_id")

    logger.info("Processing message from %s (chat %s): %s", sender_name, chat_id, text[:100])

    # Handle /start and /help
    if text.startswith("/start") or text.startswith("/help"):
        reply = (
            "Привіт! \U0001f44b Я бот додатку I'M IN — додатку для мандрівників.\n\n"
            "Напиши мені будь-яке питання про додаток, і я відповім!\n\n"
            "\U0001f30d Сайт: im-in.net\n"
            "\U0001f4f1 Скоро в App Store!"
        )
        platform = TelegramPlatform()
        await platform._request("sendMessage", chat_id=chat_id, text=reply, reply_to_message_id=message_id)
        return

    if not text:
        return

    # Save to DB
    try:
        from db.database import async_session
        from db.models import Message as MsgModel, MessageDirection

        async with async_session() as session:
            msg = MsgModel(
                platform="telegram",
                platform_message_id=str(message_id),
                sender_id=str(from_user.get("id", "")),
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

        platform = TelegramPlatform()
        await platform._request("sendMessage", chat_id=chat_id, text=reply_text, reply_to_message_id=message_id)

        try:
            from db.database import async_session
            from db.models import Message as MsgModel, MessageDirection

            async with async_session() as session:
                out_msg = MsgModel(
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
        platform = TelegramPlatform()
        await platform._request(
            "sendMessage",
            chat_id=chat_id,
            text="Дякую за повідомлення! Наша команда скоро відповість. \U0001f64f",
            reply_to_message_id=message_id,
        )


async def _polling_loop() -> None:
    """Manual getUpdates polling loop -- simple and reliable."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=60)

    offset = 0
    logger.info("=== BOT v3 === Deleting webhook...")
    try:
        await _http_client.post(_api_url("deleteWebhook"), json={"drop_pending_updates": False})
    except Exception:
        logger.exception("Failed to delete webhook")

    logger.info("=== BOT v3 === Starting manual polling loop")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "channel_post"]}
            if offset:
                params["offset"] = offset
            resp = await _http_client.post(_api_url("getUpdates"), json=params, timeout=45)
            raw_body = resp.text
            data = resp.json()

            logger.info(
                "=== BOT v3 === getUpdates status=%s body_len=%d updates=%d body_preview=%s",
                resp.status_code,
                len(raw_body),
                len(data.get("result", [])) if data.get("ok") else -1,
                raw_body[:300],
            )

            if not data.get("ok"):
                logger.error("=== BOT v3 === getUpdates error: %s", json.dumps(data))
                await asyncio.sleep(5)
                continue

            updates = data.get("result", [])
            if updates:
                logger.info("=== BOT v3 === Got %d updates!", len(updates))

            for upd in updates:
                offset = upd["update_id"] + 1
                logger.info("=== BOT v3 === Update %s: keys=%s", upd["update_id"], list(upd.keys()))

                msg = upd.get("message") or upd.get("channel_post")
                if msg and msg.get("text"):
                    try:
                        await _process_message(msg)
                    except Exception:
                        logger.exception("=== BOT v3 === Error processing update %s", upd["update_id"])

        except asyncio.CancelledError:
            logger.info("=== BOT v3 === Polling cancelled")
            break
        except Exception:
            logger.exception("=== BOT v3 === Polling error, retrying in 5s...")
            await asyncio.sleep(5)


async def start_telegram_bot() -> None:
    """Start the Telegram bot with manual polling."""
    global _polling_task

    if not settings.telegram_bot_token:
        logger.warning("Telegram bot token not set, skipping bot startup")
        return

    logger.info("=== BOT v3 === Token: %s...%s", settings.telegram_bot_token[:8], settings.telegram_bot_token[-4:])
    _polling_task = asyncio.create_task(_polling_loop())
    logger.info("=== BOT v3 === Polling task created")


async def stop_telegram_bot() -> None:
    global _polling_task, _http_client
    if _polling_task:
        logger.info("Stopping Telegram bot...")
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
        _polling_task = None
    if _http_client:
        await _http_client.aclose()
        _http_client = None
