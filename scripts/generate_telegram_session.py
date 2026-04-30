#!/usr/bin/env python3
"""One-time script: generates a Telethon StringSession for the bot.

Run locally:
    python scripts/generate_telegram_session.py

You'll be asked for your phone number and a Telegram code.
Paste the resulting StringSession into TELEGRAM_SESSION in /opt/imin/.env
on the VPS, then `docker compose up -d --force-recreate imin-bot`.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = input("Enter TELEGRAM_API_ID: ").strip()
    api_hash = input("Enter TELEGRAM_API_HASH: ").strip()

    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        print("\n=== Your StringSession (copy everything between the lines) ===")
        print("---START---")
        print(client.session.save())
        print("---END---")
        print("\nPaste this value into TELEGRAM_SESSION in /opt/imin/.env on the VPS.")


if __name__ == "__main__":
    asyncio.run(main())
