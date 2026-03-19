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
    from stats.reporter import send_daily_report

    tz = settings.timezone
    logger.info("Scheduler timezone: %s", tz)

    scheduler.add_job(
        create_daily_posts,
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="create_daily_posts",
        replace_existing=True,
    )

    for idx, time_str in enumerate(settings.post_schedule):
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(
            publish_scheduled_post,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[idx],
            id=f"publish_slot_{idx}",
            replace_existing=True,
        )
        logger.info("Publish slot %d scheduled at %s:%s %s", idx, time_str, "00", tz)

    scheduler.add_job(
        send_daily_report,
        CronTrigger(hour=20, minute=0, timezone=tz),
        id="daily_report",
        replace_existing=True,
    )
    logger.info("Daily report scheduled at 20:00 %s → %s", tz, settings.report_email_to)

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

    logger.info("Checking if today's posts exist...")
    from scheduler.jobs import ensure_daily_posts_exist
    await ensure_daily_posts_exist()

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


@app.get("/api/test/facebook")
async def test_facebook():
    """Test Facebook connection and token validity."""
    import httpx
    try:
        token = settings.facebook_page_access_token
        page_id = settings.facebook_page_id
        if not token or token.startswith("your-"):
            return {"status": "error", "error": "FACEBOOK_PAGE_ACCESS_TOKEN not configured"}
        if not page_id or page_id.startswith("your-"):
            return {"status": "error", "error": "FACEBOOK_PAGE_ID not configured"}

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://graph.facebook.com/v21.0/debug_token",
                params={"input_token": token, "access_token": token},
            )
            debug_data = r.json().get("data", {})

            r2 = await client.get(
                f"https://graph.facebook.com/v21.0/{page_id}",
                params={"fields": "name,id,followers_count", "access_token": token},
            )
            page_data = r2.json()

        return {
            "status": "ok",
            "page_id": page_id,
            "page_info": page_data,
            "token_scopes": debug_data.get("scopes", []),
            "token_valid": debug_data.get("is_valid", False),
            "token_expires": debug_data.get("expires_at"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/test/facebook-post")
async def test_facebook_post():
    """Publish a test post to Facebook page."""
    from platforms.facebook import FacebookPlatform
    fb = FacebookPlatform()
    result = await fb.publish_text("👋 Тестовий пост від I'M IN — автоматизація працює! 🚀\n\nСлідкуйте за новинами додатку для мандрівників.\n🌍 im-in.net")
    return {"status": "ok" if result.success else "error", "post_id": result.platform_post_id, "error": result.error}


@app.post("/api/trigger/daily-report")
async def trigger_daily_report():
    """Manually trigger the daily report email."""
    try:
        from stats.reporter import send_daily_report
        await send_daily_report()
        return {"status": "ok", "message": f"Report sent to {settings.report_email_to}"}
    except Exception as e:
        logger.exception("Daily report failed")
        return {
            "status": "error",
            "error": str(e),
            "debug_to": settings.report_email_to,
            "debug_resend_key_set": bool(settings.resend_api_key),
        }


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
