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
    scheduler.add_job(poll_all_messages, "interval", minutes=15,
                      id="poll_messages", replace_existing=True)
    scheduler.add_job(respond_to_pending_messages, "interval", minutes=16,
                      id="auto_reply", replace_existing=True)
    scheduler.add_job(retry_failed_publications, "interval", hours=1,
                      id="retry_failed", replace_existing=True)
    # Periodic cleanup of stale QUEUED pubs from previous days so the
    # publisher doesn't endlessly burn slots on backlog from yesterday
    # (root cause of the "no posts again" incident on 2026-05-02).
    from scheduler.maintenance import expire_old_queued_publications
    scheduler.add_job(expire_old_queued_publications, "interval", hours=6,
                      id="expire_old_queued", replace_existing=True)
    scheduler.add_job(publish_missed_slots, "interval", minutes=15,
                      id="catchup_missed_slots", replace_existing=True)
    scheduler.add_job(run_health_check, "interval", minutes=30,
                      id="health_check", replace_existing=True)

    # Phase 3 of priority-ml-system: per-post engagement snapshots at
    # 1h / 24h / 7d / 30d after publish. The cron is idempotent (UNIQUE
    # on (post_id, platform, window_hours)) so re-running it within a
    # window only refreshes the row. Drives Phase 4 ML training labels.
    from stats.post_engagement import collect_post_engagement
    scheduler.add_job(collect_post_engagement, "interval", minutes=30,
                      id="collect_post_engagement", replace_existing=True)

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

        from geo_agent.backend_client import trigger_sync_airports_to_points

        async def _daily_sync_airports_to_points():
            try:
                result = await trigger_sync_airports_to_points()
                logger.info("[airports] Sync airports → map_points: %s", result)
            except Exception as exc:
                logger.warning("[airports] Sync airports → map_points failed: %s", exc)

        scheduler.add_job(_daily_sync_airports_to_points, CronTrigger(hour=7, minute=0, timezone=tz),
                          id="airport_sync_to_points", replace_existing=True)
        logger.info("[airports] Daily airports→map_points sync at 07:00")

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

        # Region research pipeline — hierarchical admin regions
        from geo_agent.region_processor import seed_country_structure, process_region_queue

        scheduler.add_job(seed_country_structure, CronTrigger(hour=5, minute=30, timezone=tz),
                          id="region_seed_daily", replace_existing=True)
        logger.info("[regions] Daily region seeder at 05:30")

        async def _daily_build_region_queue():
            try:
                await backend_client.trigger_build_region_queue()
                logger.info("[regions] Daily region queue rebuild triggered")
            except Exception as exc:
                logger.warning("[regions] Queue rebuild failed: %s", exc)

        scheduler.add_job(_daily_build_region_queue, CronTrigger(hour=6, minute=10, timezone=tz),
                          id="region_build_queue_daily", replace_existing=True)
        logger.info("[regions] Daily region queue build at 06:10")

        scheduler.add_job(process_region_queue, "interval", minutes=15,
                          id="region_research_queue", replace_existing=True)
        logger.info("[regions] Region research queue enabled — every 15 min")

        # ── City Pulse — cultural events vertical (added April 2026) ──
        from geo_agent.city_pulse import (
            process_city_pulse_discover,
            process_city_pulse_verify,
            process_city_pulse_fetch,
        )
        from geo_agent.city_pulse_voice import process_city_pulse_voice

        scheduler.add_job(process_city_pulse_discover, "interval", minutes=10,
                          id="city_pulse_discover_queue", replace_existing=True)
        scheduler.add_job(process_city_pulse_verify, "interval", minutes=4,
                          id="city_pulse_verify_queue", replace_existing=True)
        scheduler.add_job(process_city_pulse_fetch, "interval", minutes=3,
                          id="city_pulse_fetch_queue", replace_existing=True)
        scheduler.add_job(process_city_pulse_voice, "interval", minutes=3,
                          id="city_pulse_voice_queue", replace_existing=True)

        from geo_agent.city_pulse_enrich import process_city_pulse_enrich
        # 90s cycle drains ~960 events/day — comfortably ahead of the
        # ~480 pending backlog while staying inside Perplexity quotas.
        scheduler.add_job(process_city_pulse_enrich, "interval", seconds=90,
                          id="city_pulse_enrich_queue", replace_existing=True)

        from geo_agent.events_enrich import process_event_enrich
        # Researcher events backlog is smaller (~50 short descriptions),
        # 120s cycle = ~720/day, easily covers it inside an hour.
        scheduler.add_job(process_event_enrich, "interval", seconds=120,
                          id="events_enrich_queue", replace_existing=True)
        logger.info(
            "[city-pulse] Discover/verify/fetch/voice loops scheduled "
            "(discover=10m, verify=4m, fetch=6m, voice=3m)"
        )

        async def _weekly_build_verify_queue():
            try:
                result = await backend_client.trigger_city_pulse_build_verify_queue()
                logger.info("[city-pulse] Weekly verify queue built: %s", result)
            except Exception as exc:
                logger.warning("[city-pulse] Weekly verify queue build failed: %s", exc)

        scheduler.add_job(
            _weekly_build_verify_queue,
            CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=tz),
            id="city_pulse_build_verify_weekly", replace_existing=True,
        )
        logger.info("[city-pulse] Weekly verify queue rebuild every Monday at 04:00")

        async def _daily_build_fetch_queue():
            try:
                result = await backend_client.trigger_city_pulse_build_fetch_queue()
                logger.info("[city-pulse] Daily fetch queue built: %s", result)
            except Exception as exc:
                logger.warning("[city-pulse] Daily fetch queue build failed: %s", exc)

        scheduler.add_job(
            _daily_build_fetch_queue,
            CronTrigger(hour=6, minute=15, timezone=tz),
            id="city_pulse_build_fetch_daily", replace_existing=True,
        )
        logger.info("[city-pulse] Daily fetch queue rebuild at 06:15")

        async def _nightly_archive_expired():
            try:
                result = await backend_client.trigger_city_pulse_archive_expired()
                logger.info("[city-pulse] Archived expired events: %s", result)
            except Exception as exc:
                logger.warning("[city-pulse] Archive expired failed: %s", exc)

        scheduler.add_job(
            _nightly_archive_expired,
            CronTrigger(hour=2, minute=30, timezone=tz),
            id="city_pulse_archive_nightly", replace_existing=True,
        )
        logger.info("[city-pulse] Nightly archive of expired events at 02:30")

        async def _daily_collective_interests():
            try:
                result = await backend_client.trigger_city_pulse_collective_interests(threshold=3)
                logger.info("[city-pulse] Group interest aggregation: %s", result)
            except Exception as exc:
                logger.warning("[city-pulse] Group interest aggregation failed: %s", exc)

        scheduler.add_job(
            _daily_collective_interests,
            CronTrigger(hour=18, minute=30, timezone=tz),
            id="city_pulse_collective_interests_daily", replace_existing=True,
        )
        logger.info("[city-pulse] Daily group-interest aggregation at 18:30")

        async def _weekly_auto_discover():
            try:
                result = await backend_client.trigger_city_pulse_auto_discover(max_cities=5)
                logger.info("[city-pulse] Auto-discover scan: %s", result)
            except Exception as exc:
                logger.warning("[city-pulse] Auto-discover failed: %s", exc)

        scheduler.add_job(
            _weekly_auto_discover,
            CronTrigger(day_of_week="tue", hour=5, minute=0, timezone=tz),
            id="city_pulse_auto_discover_weekly", replace_existing=True,
        )
        logger.info("[city-pulse] Weekly auto-discover scan every Tuesday at 05:00")

        # ── City Pulse social post pipeline ──
        #
        # Two paths gated by settings.use_handoff_api:
        #   true  (default, since 2026-05-02): backend hand-off API
        #     drives everything. ONE job every 15 min that fetches
        #     a 3-event batch, creates Posts, publishes, and reports
        #     each result back. Backend owns the queue, dedup, lease
        #     expiry and retry policy → no more "no posts again"
        #     incidents from a stuck local QUEUED row.
        #   false: legacy two-job path (5-min creator + 15-min
        #     publisher) for emergency rollback.
        if getattr(settings, "use_handoff_api", True):
            from scheduler.city_pulse_handoff_publisher import publish_via_handoff
            scheduler.add_job(
                publish_via_handoff,
                "interval", minutes=15,
                id="city_pulse_handoff_publisher", replace_existing=True,
            )
            logger.info(
                "[city-pulse] Hand-off publisher every 15 min "
                "(backend-driven queue, batch=3)"
            )

            # POI hand-off runs independently every 60 min. Backend
            # anti-burst keeps it from flooding (same point_type in
            # same city in last 6h gets a -5 score penalty). Slot
            # system continues to fire POI in slot 2 / web_news
            # fallbacks — the two paths share the
            # map_point_details.posted_to_social_at flag for dedup.
            if getattr(settings, "use_handoff_api_poi", True):
                from scheduler.poi_handoff_publisher import publish_poi_via_handoff
                scheduler.add_job(
                    publish_poi_via_handoff,
                    "interval", minutes=60,
                    id="poi_handoff_publisher", replace_existing=True,
                )
                logger.info(
                    "[poi] Hand-off publisher every 60 min "
                    "(backend-driven queue, batch=1)"
                )
        else:
            from scheduler.city_pulse_post_creator import process_city_pulse_post
            from scheduler.publisher import publish_city_pulse_queue

            scheduler.add_job(
                process_city_pulse_post,
                "interval", minutes=5,
                id="city_pulse_post_creator", replace_existing=True,
            )
            scheduler.add_job(
                publish_city_pulse_queue,
                "interval", minutes=15,
                id="city_pulse_publisher", replace_existing=True,
            )
            logger.info(
                "[city-pulse] LEGACY pipeline: creator every 5 min + "
                "publisher every 15 min (set USE_HANDOFF_API=true to switch)"
            )

    logger.info("Scheduler configured: %d jobs, tz=%s", len(scheduler.get_jobs()), tz)


async def _safe(coro, label: str) -> None:
    """Run a coroutine and log errors without crashing the whole startup."""
    try:
        await coro
    except Exception:
        logger.exception("Startup [%s] FAILED — continuing anyway", label)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting bot — schedulers, Telegram polling, all jobs.")
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

    from stats.token_renewer import seed_tokens_from_env
    await _safe(seed_tokens_from_env(), "seed_tokens")

    from platforms.telegram import start_telegram_bot, stop_telegram_bot
    await _safe(start_telegram_bot(), "telegram_bot")

    from scheduler.server_monitor import start_monitor_loop, stop_monitor
    monitor_task = asyncio.create_task(start_monitor_loop())

    async def _deferred_startup():
        """Run slow startup tasks after the server is accepting requests."""
        await asyncio.sleep(5)
        await _safe(publish_missed_slots(), "publish_missed")
        from scheduler.health_check import run_health_check
        await _safe(run_health_check(), "health_check")
        from scheduler.blog_sync import sync_blog_to_vps
        await _safe(sync_blog_to_vps(), "blog_generate")

    deferred_task = asyncio.create_task(_deferred_startup())

    logger.info("Social Media Automation is running! Schedule: %s (%s)",
                settings.post_schedule, settings.timezone)
    yield

    deferred_task.cancel()
    stop_monitor()
    monitor_task.cancel()
    for t in (deferred_task, monitor_task):
        try:
            await t
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
