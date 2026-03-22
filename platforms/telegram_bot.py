"""Telegram long-polling bot: message handling, channel tracking, reactions."""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from config.settings import settings, get_now_local, ensure_utc
from config.platforms import Platform
from config.emoji_classification import classify_emoji
from db.database import async_session
from db.models import Message as MsgModel, MessageDirection, ReactionSnapshot
from messaging.responder import count_replies_to_sender, MAX_REPLIES_PER_AUTHOR, FAREWELL_MESSAGE
from platforms.telegram_api import api_url, ensure_client, request as tg_request

logger = logging.getLogger(__name__)


# ── Channel post tracking ────────────────────────────────────────────────────

async def _track_channel_post(post: dict) -> None:
    text = post.get("text") or post.get("caption") or ""
    message_id = post.get("message_id")
    chat = post.get("chat", {})

    logger.info("Channel post #%s in %s: %s", message_id, chat.get("title", "?"), text[:60])

    try:
        async with async_session() as session:
            session.add(MsgModel(
                platform="telegram",
                platform_message_id=str(message_id) if message_id else None,
                sender_id="channel",
                sender_name=chat.get("title", "channel"),
                direction=MessageDirection.OUTGOING,
                text=text[:500] if text else None,
                category="channel_post",
            ))
            await session.commit()
    except Exception:
        logger.exception("Failed to save channel post to DB")


# ── Message processing ───────────────────────────────────────────────────────

def _extract_thread_info(message: dict, is_group: bool) -> tuple[str, str]:
    """Return (thread_id, post_context) for a message.

    For group comments: thread = original channel post message_id, context = post text.
    For private chats:  thread = "dm_{YYYY-MM-DD}" (daily session), context = "".
    """
    if is_group:
        reply_to = message.get("reply_to_message")
        if reply_to:
            thread_id = f"post_{reply_to.get('message_id', '')}"
            post_text = reply_to.get("text") or reply_to.get("caption") or ""
            return thread_id, post_text
        return f"group_{message['chat']['id']}", ""

    from datetime import date
    return f"dm_{date.today().isoformat()}", ""


async def _process_message(message: dict) -> None:
    text = message.get("text", "")
    chat_id = message["chat"]["id"]
    chat_type = message["chat"].get("type", "private")
    from_user = message.get("from", {})
    sender_name = from_user.get("first_name", "")
    message_id = message.get("message_id")
    is_group_comment = chat_type in ("group", "supergroup")

    if text.startswith("/start") or text.startswith("/help"):
        reply = (
            "Привіт! \U0001f44b Ми — команда I'M IN, додатку для мандрівників.\n\n"
            "Напиши нам будь-яке питання, і ми відповімо!\n\n"
            "\U0001f30d Сайт: www.im-in.net\n\U0001f4f1 Скоро в App Store!"
        )
        await tg_request("sendMessage", chat_id=chat_id, text=reply, reply_to_message_id=message_id)
        return

    if not text:
        return

    thread_id, post_context = _extract_thread_info(message, is_group_comment)
    sender_id = str(from_user.get("id", ""))

    try:
        async with async_session() as session:
            session.add(MsgModel(
                platform="telegram",
                platform_message_id=str(message_id),
                sender_id=sender_id,
                sender_name=sender_name,
                direction=MessageDirection.INCOMING,
                text=text,
                thread_id=thread_id,
                category="comment" if is_group_comment else None,
                replied=False,
            ))
            await session.commit()
    except Exception:
        logger.exception("Failed to save Telegram message to DB")

    try:
        async with async_session() as session:
            prior_replies = await count_replies_to_sender(
                session, "telegram", sender_id, thread_id,
            )

        if prior_replies > MAX_REPLIES_PER_AUTHOR:
            logger.info("Reply limit exceeded for sender=%s thread=%s (%d), not replying",
                        sender_id, thread_id, prior_replies)
            async with async_session() as session:
                result = await session.execute(
                    select(MsgModel).where(
                        MsgModel.platform == "telegram",
                        MsgModel.platform_message_id == str(message_id),
                    )
                )
                row = result.scalar_one_or_none()
                if row:
                    row.replied = True
                    await session.commit()
            return

        if prior_replies == MAX_REPLIES_PER_AUTHOR:
            reply_text = FAREWELL_MESSAGE
            category = "farewell"
        else:
            from content.generator import generate_auto_reply
            reply_text, category = await generate_auto_reply(
                incoming_message=text, platform=Platform.TELEGRAM,
                sender_name=sender_name, post_context=post_context,
            )

        if category == "spam":
            return

        await tg_request("sendMessage", chat_id=chat_id, text=reply_text, reply_to_message_id=message_id)

        try:
            async with async_session() as session:
                session.add(MsgModel(
                    platform="telegram", platform_message_id=None,
                    sender_id="bot", sender_name="bot",
                    direction=MessageDirection.OUTGOING,
                    text=reply_text, thread_id=thread_id,
                    category=category, replied=True,
                ))
                result = await session.execute(
                    select(MsgModel).where(
                        MsgModel.platform == "telegram",
                        MsgModel.platform_message_id == str(message_id),
                    )
                )
                incoming_row = result.scalar_one_or_none()
                if incoming_row:
                    incoming_row.replied = True
                await session.commit()
        except Exception:
            logger.exception("Failed to save outgoing message to DB")

        if category == "human_needed":
            logger.warning("Message from %s needs human attention: %s", sender_name, text[:200])

    except Exception:
        logger.exception("Failed to generate reply for Telegram message")
        await tg_request(
            "sendMessage", chat_id=chat_id,
            text="Дякую за повідомлення! Наша команда скоро відповість. \U0001f64f",
            reply_to_message_id=message_id,
        )


# ── Reactions ─────────────────────────────────────────────────────────────────

def _event_date_str(event_date: int) -> str | None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if not event_date:
        return None
    tz = ZoneInfo(settings.timezone)
    return datetime.fromtimestamp(event_date, tz=tz).strftime("%Y-%m-%d")


async def _upsert_reaction(message_id: str, emoji: str, total: int, msg_date: str | None) -> None:
    category = classify_emoji(emoji)
    async with async_session() as session:
        existing = await session.execute(
            select(ReactionSnapshot).where(
                ReactionSnapshot.platform == "telegram",
                ReactionSnapshot.message_id == message_id,
                ReactionSnapshot.emoji == emoji,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.total_count = total
            row.category = category
            if msg_date:
                row.message_date = msg_date
        else:
            session.add(ReactionSnapshot(
                platform="telegram", message_id=message_id,
                emoji=emoji, category=category,
                total_count=total, message_date=msg_date,
            ))
        await session.commit()


async def _track_reaction_count(update: dict) -> None:
    message_id = str(update.get("message_id", ""))
    msg_date = _event_date_str(update.get("date", 0))
    for r in update.get("reactions", []):
        emoji = r.get("type", {}).get("emoji", "")
        if emoji:
            await _upsert_reaction(message_id, emoji, r.get("total_count", 0), msg_date)


async def _track_reaction_individual(update: dict) -> None:
    message_id = str(update.get("message_id", ""))
    msg_date = _event_date_str(update.get("date", 0))
    for r in update.get("new_reaction", []):
        emoji = r.get("emoji", "")
        if not emoji:
            continue
        category = classify_emoji(emoji)
        try:
            async with async_session() as session:
                existing = await session.execute(
                    select(ReactionSnapshot).where(
                        ReactionSnapshot.platform == "telegram",
                        ReactionSnapshot.message_id == message_id,
                        ReactionSnapshot.emoji == emoji,
                    )
                )
                row = existing.scalar_one_or_none()
                if row:
                    row.total_count += 1
                    row.category = category
                    if msg_date:
                        row.message_date = msg_date
                else:
                    session.add(ReactionSnapshot(
                        platform="telegram", message_id=message_id,
                        emoji=emoji, category=category,
                        total_count=1, message_date=msg_date,
                    ))
                await session.commit()
        except Exception:
            logger.exception("Failed to save individual reaction")


# ── Polling loop ──────────────────────────────────────────────────────────────

async def polling_loop() -> None:
    client = await ensure_client()
    offset = 0

    try:
        await client.post(api_url("deleteWebhook"), json={"drop_pending_updates": False})
    except Exception:
        logger.exception("Failed to delete webhook")

    logger.info("=== BOT === Starting polling loop")

    while True:
        try:
            params: dict = {
                "timeout": 30,
                "allowed_updates": ["message", "channel_post",
                                    "message_reaction", "message_reaction_count"],
            }
            if offset:
                params["offset"] = offset

            resp = await client.post(api_url("getUpdates"), json=params, timeout=45)
            data = resp.json()

            if not data.get("ok"):
                logger.error("=== BOT === getUpdates error: %s", json.dumps(data)[:300])
                await asyncio.sleep(5)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                if upd.get("message_reaction_count"):
                    await _track_reaction_count(upd["message_reaction_count"])
                elif upd.get("message_reaction"):
                    await _track_reaction_individual(upd["message_reaction"])
                elif upd.get("channel_post"):
                    await _track_channel_post(upd["channel_post"])
                elif upd.get("message") and upd["message"].get("text"):
                    await _process_message(upd["message"])

        except asyncio.CancelledError:
            logger.info("=== BOT === Polling cancelled")
            break
        except Exception:
            logger.exception("=== BOT === Polling error, retrying in 5s...")
            await asyncio.sleep(5)
