"""Orchestrate daily report: collect data, build HTML, send via Resend."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from config.platforms import Platform, EMPTY_STATS
from config.settings import settings, get_now_local
from db.database import async_session
from db.models import DailyStats

logger = logging.getLogger(__name__)


async def _load_month_stats(year: int, month: int) -> list[DailyStats]:
    month_prefix = f"{year:04d}-{month:02d}"
    async with async_session() as session:
        result = await session.execute(
            select(DailyStats)
            .where(DailyStats.date.startswith(month_prefix))
            .order_by(DailyStats.date, DailyStats.platform)
        )
        return list(result.scalars().all())


async def _load_monthly_totals(months: int = 6) -> dict:
    now = get_now_local()
    data: dict[str, dict[str, dict]] = {}

    for i in range(months):
        dt_ = now - timedelta(days=30 * i)
        year, month = dt_.year, dt_.month
        key = f"{year:04d}-{month:02d}"
        rows = await _load_month_stats(year, month)

        for platform in Platform:
            if platform.value not in data:
                data[platform.value] = {}
            platform_rows = [r for r in rows if r.platform == platform.value]
            if platform_rows:
                last = max(platform_rows, key=lambda r: r.date)
                data[platform.value][key] = {
                    "subscribers": last.subscribers,
                    "posts": sum(r.posts for r in platform_rows),
                    "comments": sum(r.comments for r in platform_rows),
                    "views": sum(r.views for r in platform_rows),
                    "likes": sum(r.likes for r in platform_rows),
                    "dislikes": sum(r.dislikes for r in platform_rows),
                }
            else:
                data[platform.value][key] = EMPTY_STATS.copy()
    return data


async def _send_email(subject: str, html: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.report_email_from,
                "to": [settings.report_email_to],
                "subject": subject,
                "html": html,
            },
        )
        if resp.status_code in (200, 201):
            logger.info("Email sent: %s (id=%s)", subject, resp.json().get("id"))
        else:
            raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")


async def _check_blog_api() -> dict:
    """Check blog status from local posts.json and VPS availability."""
    import json
    from pathlib import Path

    blog_dir = Path(settings.data_dir) / "blog"
    posts_json = blog_dir / "posts.json"

    try:
        if posts_json.is_file():
            posts = json.loads(posts_json.read_text(encoding="utf-8"))
        else:
            base_url = settings.webhook_base_url.rstrip("/")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base_url}/api/blog/posts", params={"limit": 50})
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            posts = resp.json()

        if not posts:
            return {"ok": True, "total_posts": 0, "last_title": "—", "last_date": "—"}
        first = posts[0] if isinstance(posts, list) else {}
        last_date = str(first.get("published_at") or first.get("created_at") or "")[:10]

        vps_ok = False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get("https://www.im-in.net/blog/posts.json")
                vps_ok = r.status_code == 200 and len(r.content) > 10
        except Exception:
            pass

        return {
            "ok": True,
            "total_posts": len(posts) if isinstance(posts, list) else 0,
            "last_title": first.get("title") or "(без заголовку)",
            "last_date": last_date,
            "vps_synced": vps_ok,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


async def send_daily_report() -> None:
    """Collect stats, build report, send email via Resend."""
    from stats.collector import collect_all_stats
    from stats.token_checker import check_all_tokens
    from stats.report_html import (
        build_html, build_post_schedule_section,
        build_token_section, build_token_urgent_email,
        build_website_section, build_ml_section,
    )

    if not settings.resend_api_key or not settings.report_email_to:
        logger.warning("Resend not configured (key=%s, to=%r) — skipping",
                        "set" if settings.resend_api_key else "missing",
                        settings.report_email_to)
        return

    logger.info("=== REPORT === Starting → %s", settings.report_email_to)

    today_stats = await collect_all_stats()

    date_str = get_now_local().strftime("%Y-%m-%d")

    month_data = await _load_monthly_totals(months=6)
    post_schedule_section = await build_post_schedule_section()

    token_statuses = await check_all_tokens()
    token_section = build_token_section(token_statuses)

    blog_status = await _check_blog_api()
    website_section = build_website_section(blog_status)
    logger.info("=== REPORT === Blog API: %s", "OK" if blog_status["ok"] else blog_status.get("error"))

    ml_section = build_ml_section()
    html = build_html(today_stats, month_data, date_str, token_section, post_schedule_section,
                      website_section, ml_section=ml_section)

    logger.info("=== REPORT === Sending via Resend API to %s ...", settings.report_email_to)
    try:
        await _send_email(f"I'M IN — Звіт за {date_str}", html)
        logger.info("=== REPORT === Sent successfully")
    except Exception:
        logger.exception("=== REPORT === FAILED to send email")
        raise

    expiring = [t for t in token_statuses if t.days_remaining is not None and t.days_remaining <= 5]
    if expiring:
        logger.warning("=== REPORT === Tokens expiring soon: %s",
                        ", ".join(f"{t.platform} ({t.days_remaining}d)" for t in expiring))
        urgent_html = build_token_urgent_email(expiring)
        days_list = ", ".join(f"{t.platform} ({t.days_remaining}д)" for t in expiring)
        await _send_email(f"🚨 УВАГА: Токени закінчуються! {days_list}", urgent_html)
