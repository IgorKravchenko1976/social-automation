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


@router.post("/trigger/announce-release")
async def trigger_announce_release():
    """Create and publish a special App Store release announcement to all platforms."""
    from config.platforms import configured_platforms, get_platform_instance
    from db.database import async_session
    from db.models import Post, Publication
    from content.generator import translate_post
    from content.media import get_image_for_post
    import json

    announcement_text = (
        "🎉 I'M IN вже в App Store! 🚀\n\n"
        "Безкоштовний додаток для мандрівників тепер доступний для завантаження! "
        "Створюйте фото та відео події з прив'язкою до карти, знаходьте цікаві місця "
        "навколо, спілкуйтесь з мандрівниками з усього світу.\n\n"
        "📱 Що всередині:\n"
        "• Інтерактивна карта з подіями від мандрівників\n"
        "• Вбудована камера з фільтрами\n"
        "• Чат в реальному часі з автоперекладачем\n"
        "• Офлайн карти та GPS без інтернету\n"
        "• Ланцюжки подій — створюйте фотоісторії подорожей\n"
        "• Відстеження друзів на карті\n"
        "• 8 мов інтерфейсу\n\n"
        "Завантажуй безкоштовно: https://apps.apple.com/app/im-in/id6502195381\n"
        "🌍 www.im-in.net\n\n"
        "#imin #travel #appstore #мандрівки #подорожі #ukraine"
    )

    try:
        platforms = configured_platforms()
        if not platforms:
            return {"status": "error", "error": "No platforms configured"}

        async with async_session() as session:
            post = Post(
                title="I'M IN вже в App Store! Безкоштовний додаток для мандрівників",
                content_raw=announcement_text,
                source="manual",
            )
            post.log_pipeline("topic", "ok", "Manual: App Store release announcement")
            session.add(post)
            await session.flush()

            for platform in platforms:
                session.add(Publication(post_id=post.id, platform=platform.value))

            try:
                tr = await translate_post(post.title or "", announcement_text)
                if tr:
                    post.translations = json.dumps(tr, ensure_ascii=False)
            except Exception:
                pass

            await session.commit()
            post_id = post.id

        image_path = await get_image_for_post("travel app launch celebration world map adventure")

        from config.platforms import Platform
        results = {}
        async with async_session() as session:
            from sqlalchemy import select
            pub_result = await session.execute(
                select(Publication).where(Publication.post_id == post_id)
            )
            pubs = pub_result.scalars().all()

            for pub in pubs:
                try:
                    adapter = get_platform_instance(Platform(pub.platform))
                    result = await adapter.publish_text(announcement_text, image_path)

                    if result.success:
                        pub.status = "PUBLISHED"
                        pub.platform_post_id = result.platform_post_id
                        results[pub.platform] = "ok"
                    else:
                        pub.status = "FAILED"
                        pub.error_message = result.error
                        results[pub.platform] = f"error: {result.error}"
                except Exception as e:
                    pub.status = "FAILED"
                    pub.error_message = str(e)[:500]
                    results[pub.platform] = f"error: {str(e)[:200]}"

            await session.commit()

        if image_path:
            from content.media import cleanup_media_file
            cleanup_media_file(image_path)

        return {"status": "ok", "post_id": post_id, "results": results}
    except Exception as e:
        logger.exception("Release announcement failed")
        return {"status": "error", "error": str(e)}


@router.post("/trigger/blog-sync")
async def trigger_blog_sync():
    """Регенерація HTML блогу + доставка: API imin-backend (пріоритет) або SFTP."""
    from scheduler.blog_sync import sync_blog_to_vps
    try:
        count = await sync_blog_to_vps()
        return {"status": "ok", "synced_files": count}
    except Exception as e:
        logger.exception("Blog sync trigger failed")
        return {"status": "error", "error": str(e)}


@router.post("/trigger/geo-build-queue")
async def trigger_geo_build_queue():
    """Trigger imin-backend research queue rebuild."""
    from geo_agent.backend_client import trigger_build_queue, is_configured
    if not is_configured():
        return {"status": "error", "error": "IMIN_BACKEND_API_BASE or IMIN_BACKEND_SYNC_KEY not set"}
    try:
        result = await trigger_build_queue()
        return {"status": "ok", "backend_response": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/trigger/geo-queue-status")
async def trigger_geo_queue_status():
    """Get imin-backend research queue status."""
    from geo_agent.backend_client import get_queue_status, is_configured
    if not is_configured():
        return {"status": "error", "error": "IMIN_BACKEND_API_BASE or IMIN_BACKEND_SYNC_KEY not set"}
    try:
        result = await get_queue_status()
        return {"status": "ok", "backend_response": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/trigger/geo-process")
async def trigger_geo_process():
    """Manually trigger one geo-research processing cycle."""
    from geo_agent.processor import process_geo_queue
    try:
        await process_geo_queue()
        return {"status": "ok", "message": "One processing cycle completed"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/trigger/geo-daily-report")
async def trigger_geo_daily_report():
    """Manually trigger daily research email report."""
    from geo_agent.daily_report import send_daily_research_report
    try:
        ok = await send_daily_research_report()
        return {"status": "ok" if ok else "skipped", "sent": ok}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/trigger/health-check")
async def trigger_health_check():
    from scheduler.health_check import run_health_check
    from config.app_logger import read_log_tail
    await run_health_check()
    return {"status": "ok", "recent_log": read_log_tail(40)}


@router.get("/monitor/status")
async def monitor_status():
    """Current server monitoring state: all checks + failure history."""
    from scheduler.server_monitor import get_monitor_status
    return get_monitor_status()


@router.post("/trigger/monitor-check")
async def trigger_monitor_check():
    """Run all monitoring checks once and return results."""
    from scheduler.server_monitor import run_all_checks, get_monitor_status
    results = await run_all_checks()
    return {
        "results": [
            {"server": r.server_id, "check": r.check_id, "status": r.status.value,
             "response_ms": r.response_ms, "error": r.error,
             "status_code": r.status_code}
            for r in results
        ],
        "monitor": get_monitor_status(),
    }


@router.post("/trigger/monitor-test-email")
async def trigger_monitor_test_email():
    """Run all checks and send a test monitoring report email."""
    from scheduler.server_monitor import send_test_email
    result = await send_test_email()
    return {"result": result}


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


@router.get("/debug/comment-system")
async def debug_comment_system():
    """Full diagnostic: token permissions, comment reading, reply capability."""
    import httpx
    from stats.token_renewer import get_active_token

    report = {"facebook": {}, "instagram": {}}

    fb_token = await get_active_token("facebook") or settings.facebook_page_access_token
    page_id = settings.facebook_page_id

    if not fb_token or is_placeholder(fb_token):
        report["facebook"]["error"] = "No Facebook token configured"
    else:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{FACEBOOK_GRAPH_API}/debug_token",
                    params={"input_token": fb_token, "access_token": fb_token},
                )
                debug_data = r.json().get("data", {})
                scopes = debug_data.get("scopes", [])
                report["facebook"]["token_valid"] = debug_data.get("is_valid", False)
                report["facebook"]["scopes"] = scopes

                needed = ["pages_read_engagement", "pages_manage_engagement", "pages_show_list"]
                missing = [s for s in needed if s not in scopes]
                report["facebook"]["missing_permissions"] = missing
                report["facebook"]["comments_can_read"] = "pages_read_engagement" in scopes
                report["facebook"]["comments_can_reply"] = "pages_manage_engagement" in scopes

                r2 = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{page_id}/feed",
                    params={
                        "access_token": fb_token,
                        "fields": "id,message,comments.limit(2){id,from,message}",
                        "limit": 3,
                    },
                )
                feed = r2.json()
                if "error" in feed:
                    report["facebook"]["feed_error"] = feed["error"].get("message", str(feed["error"]))
                else:
                    posts = feed.get("data", [])
                    total_comments = sum(
                        len(p.get("comments", {}).get("data", []))
                        for p in posts
                    )
                    report["facebook"]["recent_posts"] = len(posts)
                    report["facebook"]["comments_found"] = total_comments
                    if posts:
                        report["facebook"]["sample_post"] = {
                            "id": posts[0].get("id"),
                            "text": (posts[0].get("message") or "")[:80],
                            "comments": [
                                {"from": c.get("from", {}).get("name"), "text": c.get("message", "")[:60]}
                                for c in posts[0].get("comments", {}).get("data", [])
                            ],
                        }
        except Exception as e:
            report["facebook"]["error"] = str(e)

    if fb_token and page_id:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{page_id}",
                    params={"access_token": fb_token, "fields": "instagram_business_account"},
                )
                ig_data = r.json()
                ig_id = ig_data.get("instagram_business_account", {}).get("id")
                if ig_id:
                    report["instagram"]["ig_business_id"] = ig_id
                    r2 = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{ig_id}/media",
                        params={
                            "access_token": fb_token,
                            "fields": "id,caption,comments.limit(2){id,from,text}",
                            "limit": 3,
                        },
                    )
                    media = r2.json()
                    if "error" in media:
                        report["instagram"]["media_error"] = media["error"].get("message", str(media["error"]))
                    else:
                        items = media.get("data", [])
                        total_ig_comments = sum(
                            len(m.get("comments", {}).get("data", []))
                            for m in items
                        )
                        report["instagram"]["recent_media"] = len(items)
                        report["instagram"]["comments_found"] = total_ig_comments
                        if items:
                            report["instagram"]["sample_media"] = {
                                "id": items[0].get("id"),
                                "caption": (items[0].get("caption") or "")[:80],
                                "comments": [
                                    {"from": c.get("from", {}).get("username", "?"), "text": c.get("text", "")[:60]}
                                    for c in items[0].get("comments", {}).get("data", [])
                                ],
                            }
                else:
                    report["instagram"]["error"] = "No IG Business Account linked to FB Page"
        except Exception as e:
            report["instagram"]["error"] = str(e)

    return report


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
