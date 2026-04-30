import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    client = TelegramClient(StringSession(), 33365313, "c26d2b6677d4a54c8c4022da0a4d98f5")
    await client.start()
    session = client.session.save()
    print("\n\n========================================")
    print("TELEGRAM_SESSION value for /opt/imin/.env on the VPS:")
    print("========================================")
    print(session)
    print("========================================\n")
    await client.disconnect()

asyncio.run(main())
