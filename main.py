from __future__ import annotations

import asyncio
import logging
import pathlib
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from config.settings import settings
from config.app_logger import setup_logging
from db.database import init_db
from api.routes import router as admin_router, public_router
from api.routes_geo import geo_router
from api.triggers import router as triggers_router

log_file = setup_logging(data_dir=settings.data_dir, level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("Log file: %s (3-day rotation)", log_file)

scheduler = AsyncIOScheduler(timezone=settings.timezone)


def _setup_scheduler() -> None:
    from scheduler.jobs import publish_scheduled_post, retry_failed_publications
    from scheduler.jobs import publish_missed_slots
    from scheduler.health_check import run_health_check
    from messaging.monitor import poll_all_messages
    from messaging.responder import respond_to_pending_messages
    from stats.reporter import send_daily_report
    from stats.token_renewer import renew_all_tokens

    tz = settings.timezone

    for idx, time_str in enumerate(settings.post_schedule):
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(publish_scheduled_post, CronTrigger(hour=hour, minute=minute, timezone=tz),
                          args=[idx], id=f"publish_slot_{idx}", replace_existing=True)
        logger.info("Publish slot %d → %s %s", idx, time_str, tz)

    scheduler.add_job(send_daily_report, CronTrigger(hour=20, minute=0, timezone=tz),
                      id="daily_report", replace_existing=True)
    scheduler.add_job(renew_all_tokens, CronTrigger(hour=3, minute=0, timezone=tz),
                      id="renew_tokens", replace_existing=True)
    scheduler.add_job(poll_all_messages, "interval", minutes=5,
                      id="poll_messages", replace_existing=True)
    scheduler.add_job(respond_to_pending_messages, "interval", minutes=6,
                      id="auto_reply", replace_existing=True)
    scheduler.add_job(retry_failed_publications, "interval", hours=1,
                      id="retry_failed", replace_existing=True)
    scheduler.add_job(publish_missed_slots, "interval", minutes=15,
                      id="catchup_missed_slots", replace_existing=True)
    scheduler.add_job(run_health_check, "interval", minutes=30,
                      id="health_check", replace_existing=True)

    from scheduler.blog_sync import sync_blog_to_vps
    scheduler.add_job(sync_blog_to_vps, CronTrigger(hour=21, minute=0, timezone=tz),
                      id="blog_sync_daily", replace_existing=True)

    from geo_agent.processor import process_geo_queue
    scheduler.add_job(process_geo_queue, "interval", minutes=2,
                      id="geo_research_queue", replace_existing=True)

    from geo_agent.backend_client import is_configured as _backend_ok, trigger_build_queue
    if _backend_ok():
        async def _daily_build_queue():
            try:
                result = await trigger_build_queue()
                logger.info("[geo] Daily queue rebuild: %s", result)
            except Exception as exc:
                logger.warning("[geo] Daily queue rebuild failed: %s", exc)

        scheduler.add_job(_daily_build_queue, CronTrigger(hour=6, minute=0, timezone=tz),
                          id="geo_build_queue_daily", replace_existing=True)
        logger.info("[geo] Backend mode enabled — daily queue rebuild at 06:00")

        from geo_agent.daily_report import send_daily_research_report
        scheduler.add_job(send_daily_research_report, CronTrigger(hour=21, minute=0, timezone=tz),
                          id="geo_daily_report", replace_existing=True)
        logger.info("[geo] Daily research report at 21:00")

        # Airport research pipeline (separate from geo research)
        from geo_agent.airport_processor import process_airport_queue
        scheduler.add_job(process_airport_queue, "interval", minutes=2,
                          id="airport_research_queue", replace_existing=True)
        logger.info("[airports] Airport research queue enabled — every 2 min")

        from geo_agent.backend_client import trigger_build_airport_queue, trigger_sync_airports

        async def _daily_build_airport_queue():
            try:
                result = await trigger_build_airport_queue()
                logger.info("[airports] Daily airport queue rebuild: %s", result)
            except Exception as exc:
                logger.warning("[airports] Daily airport queue rebuild failed: %s", exc)

        scheduler.add_job(_daily_build_airport_queue, CronTrigger(hour=6, minute=5, timezone=tz),
                          id="airport_build_queue_daily", replace_existing=True)
        logger.info("[airports] Daily airport queue rebuild at 06:05")

        async def _weekly_sync_airports():
            try:
                result = await trigger_sync_airports()
                logger.info("[airports] Weekly sync: %s", result)
            except Exception as exc:
                logger.warning("[airports] Weekly sync failed: %s", exc)

        scheduler.add_job(_weekly_sync_airports, CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=tz),
                          id="airport_weekly_sync", replace_existing=True)
        logger.info("[airports] Weekly airport sync every Monday at 04:00")

        # Fix/translate pipeline for existing events & airports
        from geo_agent.fixer import run_fix_cycle
        scheduler.add_job(run_fix_cycle, "interval", minutes=3,
                          id="fix_translate_cycle", replace_existing=True)
        logger.info("[fixer] Fix/translate cycle enabled — every 3 min")

        # POI research pipeline — deep research for enriched POI points
        from geo_agent.poi_researcher import process_poi_research
        scheduler.add_job(process_poi_research, "interval", minutes=5,
                          id="poi_research_queue", replace_existing=True)
        logger.info("[poi-researcher] POI research queue enabled — every 5 min")

    logger.info("Scheduler configured: %d jobs, tz=%s", len(scheduler.get_jobs()), tz)


async def _safe(coro, label: str) -> None:
    """Run a coroutine and log errors without crashing the whole startup."""
    try:
        await coro
    except Exception:
        logger.exception("Startup [%s] FAILED — continuing anyway", label)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()

    _setup_scheduler()
    scheduler.start()

    from scheduler.jobs import (
        ensure_daily_posts_exist, publish_missed_slots,
        expire_old_queued_publications, expire_inactive_platform_publications,
    )
    await _safe(expire_old_queued_publications(), "expire_old_pubs")
    await _safe(expire_inactive_platform_publications(), "expire_inactive_platforms")
    await _safe(ensure_daily_posts_exist(), "ensure_posts")
    await _safe(publish_missed_slots(), "publish_missed")

    from stats.token_renewer import seed_tokens_from_env
    await _safe(seed_tokens_from_env(), "seed_tokens")

    from platforms.telegram import start_telegram_bot, stop_telegram_bot
    await _safe(start_telegram_bot(), "telegram_bot")

    from scheduler.health_check import run_health_check
    await _safe(run_health_check(), "health_check")

    from scheduler.blog_sync import sync_blog_to_vps
    await _safe(sync_blog_to_vps(), "blog_generate")

    from scheduler.server_monitor import start_monitor_loop, stop_monitor
    monitor_task = asyncio.create_task(start_monitor_loop())

    logger.info("Social Media Automation is running! Schedule: %s (%s)",
                settings.post_schedule, settings.timezone)
    yield

    stop_monitor()
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

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
    allow_origins=[
        "https://www.im-in.net",
        "https://im-in.net",
        "http://localhost:3000",
        "http://localhost:8080",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(public_router)
app.include_router(admin_router)
app.include_router(triggers_router)
app.include_router(geo_router)

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


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
