from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from config.settings import settings
from db.database import init_db
from api.routes import router as api_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=settings.timezone)


def _setup_scheduler() -> None:
    from scheduler.jobs import create_daily_posts, publish_scheduled_post, retry_failed_publications
    from messaging.monitor import poll_all_messages
    from messaging.responder import respond_to_pending_messages

    # Generate daily content at 08:00
    scheduler.add_job(
        create_daily_posts,
        CronTrigger(hour=8, minute=0),
        id="create_daily_posts",
        replace_existing=True,
    )

    # Publish at configured times (default: 09:00, 13:00, 18:00)
    for idx, time_str in enumerate(settings.post_schedule):
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(
            publish_scheduled_post,
            CronTrigger(hour=hour, minute=minute),
            args=[idx],
            id=f"publish_slot_{idx}",
            replace_existing=True,
        )

    # Poll messages every 5 minutes
    scheduler.add_job(
        poll_all_messages,
        "interval",
        minutes=5,
        id="poll_messages",
        replace_existing=True,
    )

    # Auto-reply every 6 minutes (offset from polling)
    scheduler.add_job(
        respond_to_pending_messages,
        "interval",
        minutes=6,
        id="auto_reply",
        replace_existing=True,
    )

    # Retry failed publications every hour
    scheduler.add_job(
        retry_failed_publications,
        "interval",
        hours=1,
        id="retry_failed",
        replace_existing=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()
    logger.info("Starting scheduler...")
    _setup_scheduler()
    scheduler.start()

    logger.info("Starting Telegram bot...")
    from platforms.telegram import start_telegram_bot, stop_telegram_bot
    await start_telegram_bot()

    logger.info("Social Media Automation is running!")
    logger.info("Post schedule: %s (%s)", settings.post_schedule, settings.timezone)
    yield

    await stop_telegram_bot()
    scheduler.shutdown()
    logger.info("Scheduler stopped.")


app = FastAPI(
    title="Social Media Automation",
    description="Automated social media management for your app",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(api_router)

import pathlib
_static_dir = pathlib.Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
async def root():
    return {
        "service": "Social Media Automation",
        "status": "running",
        "post_schedule": settings.post_schedule,
        "timezone": settings.timezone,
    }


@app.post("/api/trigger/create-posts")
async def trigger_create_posts():
    """Manually trigger daily post creation."""
    from scheduler.jobs import create_daily_posts
    await create_daily_posts()
    return {"status": "ok", "message": "Daily posts created"}


@app.post("/api/trigger/publish/{slot}")
async def trigger_publish(slot: int):
    """Manually trigger publishing for a time slot."""
    from scheduler.jobs import publish_scheduled_post
    await publish_scheduled_post(slot)
    return {"status": "ok", "message": f"Published slot {slot}"}


@app.post("/api/trigger/poll-messages")
async def trigger_poll():
    """Manually trigger message polling."""
    from messaging.monitor import poll_all_messages
    messages = await poll_all_messages()
    return {"status": "ok", "new_messages": len(messages)}


@app.post("/api/trigger/auto-reply")
async def trigger_auto_reply():
    """Manually trigger auto-replies."""
    from messaging.responder import respond_to_pending_messages
    count = await respond_to_pending_messages()
    return {"status": "ok", "replied": count}


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
