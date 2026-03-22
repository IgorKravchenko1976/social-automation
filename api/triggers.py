"""Manual trigger, test, debug, and log endpoints — all require admin API key."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends

from config.settings import settings, get_today_start_utc, is_placeholder
from config.platforms import FACEBOOK_GRAPH_API, INSTAGRAM_GRAPH_API
from api.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["triggers"],
    dependencies=[Depends(require_admin)],
)


# ── Triggers ──────────────────────────────────────────────────────────────────

@router.post("/trigger/create-posts")
async def trigger_create_posts():
    from scheduler.jobs import create_daily_posts
    await create_daily_posts()
    return {"status": "ok", "message": "Daily posts created"}


@router.post("/trigger/publish/{slot}")
async def trigger_publish(slot: int):
    from scheduler.jobs import publish_scheduled_post
    try:
        await publish_scheduled_post(slot)
        return {"status": "ok", "message": f"Published slot {slot}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/trigger/poll-messages")
async def trigger_poll():
    from messaging.monitor import poll_all_messages
    messages = await poll_all_messages()
    return {"status": "ok", "new_messages": len(messages)}


@router.post("/trigger/auto-reply")
async def trigger_auto_reply():
    from messaging.responder import respond_to_pending_messages
    count = await respond_to_pending_messages()
    return {"status": "ok", "replied": count}


@router.post("/trigger/renew-tokens")
async def trigger_renew_tokens():
    try:
        from stats.token_renewer import renew_all_tokens
        results = await renew_all_tokens()
        return {"status": "ok", "results": results}
    except Exception as e:
        logger.exception("Token renewal failed")
        return {"status": "error", "error": str(e)}


@router.post("/trigger/daily-report")
async def trigger_daily_report():
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


@router.post("/trigger/blog-sync")
async def trigger_blog_sync():
    """Force regeneration of all blog pages + SFTP push to VPS."""
    from scheduler.blog_sync import sync_blog_to_vps
    try:
        count = await sync_blog_to_vps()
        return {"status": "ok", "synced_files": count}
    except Exception as e:
        logger.exception("Blog sync trigger failed")
        return {"status": "error", "error": str(e)}


@router.post("/trigger/health-check")
async def trigger_health_check():
    from scheduler.health_check import run_health_check
    from config.app_logger import read_log_tail
    await run_health_check()
    return {"status": "ok", "recent_log": read_log_tail(40)}


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs")
async def view_logs(lines: int = 200):
    from config.app_logger import read_log_tail, get_log_path
    path = get_log_path()
    return {
        "log_file": str(path),
        "exists": path.exists(),
        "size_kb": round(path.stat().st_size / 1024, 1) if path.exists() else 0,
        "tail": read_log_tail(lines),
    }


@router.get("/logs/full")
async def view_full_log():
    from fastapi.responses import PlainTextResponse
    from config.app_logger import read_full_log
    return PlainTextResponse(read_full_log(), media_type="text/plain; charset=utf-8")


# ── Debug ─────────────────────────────────────────────────────────────────────

@router.get("/debug/publications")
async def debug_publications():
    from db.database import async_session
    from db.models import Post, Publication
    from sqlalchemy import select

    today_start_utc = get_today_start_utc()

    async with async_session() as session:
        result = await session.execute(
            select(Post).where(Post.created_at >= today_start_utc).order_by(Post.created_at)
        )
        posts = result.scalars().all()

        data = []
        for p in posts:
            pub_result = await session.execute(
                select(Publication).where(Publication.post_id == p.id)
            )
            pubs = pub_result.scalars().all()
            data.append({
                "post_id": p.id,
                "title": (p.title or "")[:60],
                "created": str(p.created_at),
                "publications": [
                    {"platform": pub.platform, "status": pub.status,
                     "error": pub.error_message, "retries": pub.retry_count}
                    for pub in pubs
                ],
            })
    return data


# ── Tests ─────────────────────────────────────────────────────────────────────

@router.get("/test/facebook")
async def test_facebook():
    import httpx
    try:
        token = settings.facebook_page_access_token
        page_id = settings.facebook_page_id
        if is_placeholder(token):
            return {"status": "error", "error": "FACEBOOK_PAGE_ACCESS_TOKEN not configured"}
        if is_placeholder(page_id):
            return {"status": "error", "error": "FACEBOOK_PAGE_ID not configured"}

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{FACEBOOK_GRAPH_API}/debug_token",
                params={"input_token": token, "access_token": token},
            )
            debug_data = r.json().get("data", {})

            r2 = await client.get(
                f"{FACEBOOK_GRAPH_API}/{page_id}",
                params={"fields": "name,id,followers_count", "access_token": token},
            )
            page_data = r2.json()

        return {
            "status": "ok",
            "page_id": page_id,
            "page_info": page_data,
            "token_type": debug_data.get("type", "unknown"),
            "token_scopes": debug_data.get("scopes", []),
            "token_valid": debug_data.get("is_valid", False),
            "token_expires": debug_data.get("expires_at"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/test/facebook-post")
async def test_facebook_post():
    from platforms.facebook import FacebookPlatform
    fb = FacebookPlatform()
    result = await fb.publish_text(
        "\U0001f44b Тестовий пост від I'M IN — автоматизація працює! \U0001f680\n\n"
        "Слідкуйте за новинами додатку для мандрівників.\n\U0001f30d www.im-in.net"
    )
    return {"status": "ok" if result.success else "error",
            "post_id": result.platform_post_id, "error": result.error}


@router.get("/test/instagram-business-id")
async def get_instagram_business_id():
    import httpx
    from stats.token_renewer import get_active_token
    token = await get_active_token("facebook") or settings.facebook_page_access_token
    page_id = settings.facebook_page_id
    if not token or not page_id:
        return {"status": "error", "error": "Facebook not configured"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{FACEBOOK_GRAPH_API}/{page_id}",
                params={"fields": "instagram_business_account,name", "access_token": token},
            )
            return r.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/test/instagram")
async def test_instagram():
    import httpx
    try:
        token = settings.instagram_access_token
        user_id = settings.instagram_user_id
        if not token:
            return {"status": "error", "error": "INSTAGRAM_ACCESS_TOKEN not configured"}
        if not user_id:
            return {"status": "error", "error": "INSTAGRAM_USER_ID not configured"}

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{INSTAGRAM_GRAPH_API}/{user_id}",
                params={"fields": "id,username,followers_count,media_count", "access_token": token},
            )
            data = r.json()

        if "error" in data:
            return {"status": "error", "error": data["error"].get("message")}

        return {
            "status": "ok",
            "user_id": user_id,
            "username": data.get("username"),
            "followers": data.get("followers_count"),
            "media_count": data.get("media_count"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
