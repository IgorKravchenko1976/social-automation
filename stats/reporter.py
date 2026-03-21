"""Generate HTML email report and send via Resend HTTP API."""
from __future__ import annotations

import base64
import io
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sqlalchemy import select

from config.platforms import Platform
from config.settings import settings
from db.database import async_session
from db.models import DailyStats, Post, Publication, PostStatus

logger = logging.getLogger(__name__)

PLATFORM_LABELS = {
    "telegram": "Telegram",
    "facebook": "Facebook",
    "twitter": "X / Twitter",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

PLATFORM_COLORS = {
    "telegram": "#2AABEE",
    "facebook": "#1877F2",
    "twitter": "#E7E9EA",
    "instagram": "#E4405F",
    "tiktok": "#00F2EA",
}


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
    """Load aggregated stats per month for the last N months."""
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
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
                data[platform.value][key] = {
                    "subscribers": 0, "posts": 0, "comments": 0,
                    "views": 0, "likes": 0, "dislikes": 0,
                }

    return data


def _make_monthly_chart(month_data: dict, metric_keys: list[str], title: str) -> str:
    """Render a bar+line chart and return base64-encoded PNG."""
    try:
        fig, ax = plt.subplots(figsize=(10, 4.5))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        months_sorted = sorted({m for p in month_data.values() for m in p})
        if not months_sorted:
            months_sorted = [datetime.now().strftime("%Y-%m")]

        x_positions = list(range(len(months_sorted)))

        bar_width = 0.15
        has_data = False
        for idx, platform in enumerate(month_data):
            values = [
                sum(month_data[platform].get(m, {}).get(k, 0) for k in metric_keys)
                for m in months_sorted
            ]
            if not any(values):
                continue
            has_data = True
            color = PLATFORM_COLORS.get(platform, "#888888")
            label = PLATFORM_LABELS.get(platform, platform)
            offsets = [x + bar_width * idx for x in x_positions]
            ax.bar(offsets, values, bar_width, label=label, color=color, alpha=0.85)

        if not has_data:
            ax.text(0.5, 0.5, "No data yet", transform=ax.transAxes,
                    ha="center", va="center", color="#64748b", fontsize=14)

        ax.set_xticks([x + bar_width * 2 for x in x_positions])
        ax.set_xticklabels(months_sorted, color="#94a3b8", fontsize=9)
        ax.tick_params(axis="y", colors="#94a3b8")
        ax.set_title(title, color="#e2e8f0", fontsize=13, pad=12)
        if has_data:
            ax.legend(facecolor="#262640", edgecolor="#333", labelcolor="#e2e8f0", fontsize=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.grid(axis="y", color="#333", alpha=0.3)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()
    except Exception:
        logger.exception("Failed to generate chart: %s", title)
        plt.close("all")
        return ""


POST_TYPE_LABELS = {
    0: "Туристична новина",
    1: "Активний спорт",
    2: "Туристична новина",
    3: "Функціонал додатку",
    4: "Красиве місце",
}


async def _build_post_schedule_section() -> str:
    """Build HTML showing today's post schedule and publication status."""
    from datetime import timezone
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start.astimezone(timezone.utc).replace(tzinfo=None)
    schedule = settings.post_schedule

    async with async_session() as session:
        result = await session.execute(
            select(Post)
            .where(Post.created_at >= today_start_utc)
            .order_by(Post.created_at)
        )
        today_posts = result.scalars().all()

        rows = ""
        for idx, time_str in enumerate(schedule):
            hour, minute = map(int, time_str.split(":"))
            slot_time = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            is_past = now_local > slot_time
            type_label = POST_TYPE_LABELS.get(idx, f"Пост #{idx+1}")

            if idx < len(today_posts):
                post = today_posts[idx]
                title = (post.title or post.content_raw or "")[:60]
                if len(post.title or post.content_raw or "") > 60:
                    title += "..."

                from scheduler.jobs import _configured_platforms
                active_platforms = {p.value for p in _configured_platforms()}

                pub_result = await session.execute(
                    select(Publication).where(Publication.post_id == post.id)
                )
                all_pubs = pub_result.scalars().all()
                pubs = [p for p in all_pubs if p.platform in active_platforms]

                platform_icons = {
                    "telegram": "TG", "facebook": "FB", "instagram": "IG",
                    "twitter": "X", "tiktok": "TT",
                }
                parts = []
                for pub in sorted(pubs, key=lambda p: p.platform):
                    icon = platform_icons.get(pub.platform, pub.platform[:2].upper())
                    if pub.status == PostStatus.PUBLISHED:
                        parts.append(f'<span style="color:#6ee7b7;" title="{pub.platform}">✅{icon}</span>')
                    elif pub.status == PostStatus.FAILED:
                        err = (pub.error_message or "")[:40]
                        parts.append(f'<span style="color:#f87171;" title="{err}">❌{icon}</span>')
                    elif pub.status == PostStatus.QUEUED and is_past:
                        parts.append(f'<span style="color:#fbbf24;" title="{pub.platform}">⏳{icon}</span>')
                    elif pub.status == PostStatus.QUEUED:
                        parts.append(f'<span style="color:#94a3b8;" title="{pub.platform}">🕐{icon}</span>')
                    else:
                        parts.append(f'<span style="color:#94a3b8;">{icon}</span>')

                status_html = " ".join(parts) if parts else '<span style="color:#94a3b8;">—</span>'
            else:
                title = "—"
                status_html = '<span style="color:#f87171;">❌ не створено</span>'

            time_color = "#6ee7b7" if is_past else "#94a3b8"
            rows += f"""
            <tr>
              <td style="padding:8px 14px;border-bottom:1px solid #262640;">
                <span style="color:{time_color};font-weight:700;">{time_str}</span>
              </td>
              <td style="padding:8px 10px;border-bottom:1px solid #262640;color:#94a3b8;">{type_label}</td>
              <td style="padding:8px 10px;border-bottom:1px solid #262640;">{title}</td>
              <td style="padding:8px 10px;border-bottom:1px solid #262640;text-align:center;">{status_html}</td>
            </tr>"""

    return f"""
  <h2 style="color:#e2e8f0;font-size:17px;border-bottom:2px solid #22d3ee;padding-bottom:6px;margin-top:32px;">
    Розклад постів сьогодні
  </h2>
  <table style="width:100%;border-collapse:collapse;color:#e2e8f0;font-size:14px;">
    <thead>
      <tr style="background:#1a1a2e;">
        <th style="padding:8px 14px;text-align:left;color:#94a3b8;font-weight:400;">Час</th>
        <th style="padding:8px 10px;text-align:left;color:#94a3b8;font-weight:400;">Тип</th>
        <th style="padding:8px 10px;text-align:left;color:#94a3b8;font-weight:400;">Тема</th>
        <th style="padding:8px 10px;text-align:center;color:#94a3b8;font-weight:400;">Статус</th>
      </tr>
    </thead>
    <tbody>{rows}
    </tbody>
  </table>"""


def _build_token_section(token_statuses: list) -> str:
    """Build HTML section showing token validity and expiry."""
    rows = ""
    for ts in token_statuses:
        if not ts.configured:
            status_html = '<span style="color:#64748b;">не налаштовано</span>'
            expiry_html = "—"
        elif not ts.valid:
            status_html = '<span style="color:#f87171;font-weight:700;">❌ НЕВАЛІДНИЙ</span>'
            expiry_html = ts.error or "—"
        else:
            status_html = '<span style="color:#6ee7b7;">✅ активний</span>'
            if ts.expires_at:
                expiry_html = ts.expires_at.strftime("%Y-%m-%d")
            else:
                expiry_html = "безстроковий"

        warning = ""
        if ts.days_remaining is not None and ts.days_remaining <= 5:
            warning = (
                '<div style="background:#7f1d1d;color:#fca5a5;padding:8px 12px;'
                'margin-top:6px;border-radius:4px;font-size:16px;font-weight:700;'
                'text-transform:uppercase;text-align:center;">'
                f'⚠️ ТОКЕН {ts.platform.upper()} ЗАКІНЧУЄТЬСЯ ЧЕРЕЗ {ts.days_remaining} ДНІВ — ПРОДОВЖИТИ!'
                '</div>'
            )

        days_html = ""
        if ts.days_remaining is not None:
            if ts.days_remaining <= 5:
                days_html = f'<span style="color:#f87171;font-weight:700;">{ts.days_remaining} дн.</span>'
            elif ts.days_remaining <= 14:
                days_html = f'<span style="color:#fbbf24;">{ts.days_remaining} дн.</span>'
            else:
                days_html = f'<span style="color:#6ee7b7;">{ts.days_remaining} дн.</span>'
        else:
            days_html = "—"

        rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #262640;font-weight:600;">{ts.platform}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">токен</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{status_html}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{expiry_html}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{days_html}</td>
        </tr>"""
        if warning:
            rows += f'<tr><td colspan="5" style="padding:0 14px 10px;">{warning}</td></tr>'

    return f"""
  <h2 style="color:#e2e8f0;font-size:17px;border-bottom:2px solid #f59e0b;padding-bottom:6px;margin-top:32px;">
    Стан токенів
  </h2>
  <table style="width:100%;border-collapse:collapse;color:#e2e8f0;font-size:14px;">
    <thead>
      <tr style="background:#1a1a2e;">
        <th style="padding:10px 14px;text-align:left;color:#94a3b8;font-weight:400;">Соцмережа</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Тип</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Статус</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Дійсний до</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Залишилось</th>
      </tr>
    </thead>
    <tbody>{rows}
    </tbody>
  </table>"""


def _build_html(today_stats: list[DailyStats], month_data: dict, date_str: str,
                token_section: str = "", post_schedule_section: str = "") -> str:
    """Build full HTML email body."""

    # ── Block 1: Today ──
    rows_html = ""
    for s in today_stats:
        label = PLATFORM_LABELS.get(s.platform, s.platform)
        color = PLATFORM_COLORS.get(s.platform, "#888")
        rows_html += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #262640;">
            <span style="color:{color};font-weight:600;">{label}</span>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{s.subscribers}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{s.posts}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{s.comments}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;">{s.views}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;color:#6ee7b7;">{s.likes}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #262640;text-align:center;color:#f87171;">{s.dislikes}</td>
        </tr>"""

    # ── Block 2: Monthly activity chart ──
    chart_activity_b64 = _make_monthly_chart(
        month_data,
        ["subscribers", "comments", "views"],
        "Підписники + Коментарі + Перегляди за місяць",
    )

    # ── Block 3: Monthly subscribers chart ──
    chart_subs_b64 = _make_monthly_chart(
        month_data,
        ["subscribers"],
        "Підписники по місяцях",
    )

    # ── Block 3 table ──
    months_sorted = sorted({m for p in month_data.values() for m in p})
    month_headers = "".join(
        f'<th style="padding:8px 10px;color:#94a3b8;font-weight:400;">{m}</th>'
        for m in months_sorted
    )
    subs_rows = ""
    for pv in month_data:
        label = PLATFORM_LABELS.get(pv, pv)
        color = PLATFORM_COLORS.get(pv, "#888")
        cells = "".join(
            f'<td style="padding:8px 10px;text-align:center;">{month_data[pv].get(m, {}).get("subscribers", 0)}</td>'
            for m in months_sorted
        )
        subs_rows += f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #262640;">
            <span style="color:{color};font-weight:600;">{label}</span>
          </td>
          {cells}
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f0f23;color:#e2e8f0;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:24px;">

  <h1 style="color:#6ee7b7;font-size:22px;margin-bottom:4px;">I'M IN — Щоденний звіт</h1>
  <p style="color:#94a3b8;margin-top:0;">{date_str}</p>

  <!-- BLOCK 1: Today -->
  <h2 style="color:#e2e8f0;font-size:17px;border-bottom:2px solid #6ee7b7;padding-bottom:6px;">
    Сьогодні
  </h2>
  <table style="width:100%;border-collapse:collapse;color:#e2e8f0;font-size:14px;">
    <thead>
      <tr style="background:#1a1a2e;">
        <th style="padding:10px 14px;text-align:left;color:#94a3b8;font-weight:400;">Платформа</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Підписники</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Пости</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Коментарі</th>
        <th style="padding:10px 8px;text-align:center;color:#94a3b8;font-weight:400;">Перегляди</th>
        <th style="padding:10px 8px;text-align:center;color:#6ee7b7;font-weight:400;">✅ Позитивні</th>
        <th style="padding:10px 8px;text-align:center;color:#f87171;font-weight:400;">❌ Негативні</th>
      </tr>
    </thead>
    <tbody>{rows_html}
    </tbody>
  </table>

  <!-- BLOCK 2: Monthly chart -->
  <h2 style="color:#e2e8f0;font-size:17px;border-bottom:2px solid #3b82f6;padding-bottom:6px;margin-top:32px;">
    Графік за місяць
  </h2>
  <img src="data:image/png;base64,{chart_activity_b64}"
       style="width:100%;border-radius:8px;margin:12px 0;" alt="Monthly activity chart"/>

  <!-- BLOCK 3: Subscribers per month -->
  <h2 style="color:#e2e8f0;font-size:17px;border-bottom:2px solid #a78bfa;padding-bottom:6px;margin-top:32px;">
    Підписники по місяцях
  </h2>
  <img src="data:image/png;base64,{chart_subs_b64}"
       style="width:100%;border-radius:8px;margin:12px 0;" alt="Monthly subscribers chart"/>

  <table style="width:100%;border-collapse:collapse;color:#e2e8f0;font-size:14px;margin-top:8px;">
    <thead>
      <tr style="background:#1a1a2e;">
        <th style="padding:8px 10px;text-align:left;color:#94a3b8;font-weight:400;">Платформа</th>
        {month_headers}
      </tr>
    </thead>
    <tbody>{subs_rows}
    </tbody>
  </table>

  {post_schedule_section}

  {token_section}

  <p style="color:#64748b;font-size:12px;margin-top:32px;text-align:center;">
    Автоматичний звіт від I'M IN Social Automation • www.im-in.net
  </p>
</div>
</body>
</html>"""

    return html


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


def _build_token_urgent_email(expiring: list) -> str:
    """Build urgent HTML email for tokens about to expire."""
    rows = ""
    for ts in expiring:
        rows += f"""
        <div style="background:#7f1d1d;color:#fca5a5;padding:16px;margin:12px 0;
                    border-radius:8px;border:2px solid #f87171;">
          <div style="font-size:24px;font-weight:700;text-align:center;text-transform:uppercase;">
            ⚠️ ТОКЕН {ts.platform.upper()} ЗАКІНЧУЄТЬСЯ!
          </div>
          <div style="text-align:center;margin-top:8px;font-size:16px;">
            Залишилось: <strong>{ts.days_remaining} дн.</strong>
            {(' — до ' + ts.expires_at.strftime('%Y-%m-%d')) if ts.expires_at else ''}
          </div>
          <div style="text-align:center;margin-top:12px;font-size:14px;color:#fbbf24;">
            Перегенеруйте токен через Graph API Explorer → /me/accounts
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f0f23;color:#e2e8f0;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
  <h1 style="color:#f87171;font-size:28px;text-align:center;">🚨 УВАГА: ТОКЕНИ ЗАКІНЧУЮТЬСЯ</h1>
  {rows}
  <p style="color:#64748b;font-size:12px;margin-top:32px;text-align:center;">
    I'M IN Social Automation • www.im-in.net
  </p>
</div></body></html>"""


async def send_daily_report() -> None:
    """Collect stats, build report, send email via Resend."""
    from stats.collector import collect_all_stats
    from stats.token_checker import check_all_tokens

    if not settings.resend_api_key or not settings.report_email_to:
        logger.warning("Resend not configured (key=%s, to=%r) — skipping",
                        "set" if settings.resend_api_key else "missing",
                        settings.report_email_to)
        return

    logger.info("=== REPORT === Starting → %s", settings.report_email_to)

    logger.info("=== REPORT === Collecting daily stats...")
    today_stats = await collect_all_stats()

    tz = ZoneInfo(settings.timezone)
    date_str = datetime.now(tz).strftime("%Y-%m-%d")

    logger.info("=== REPORT === Loading monthly data...")
    month_data = await _load_monthly_totals(months=6)

    logger.info("=== REPORT === Building post schedule...")
    post_schedule_section = await _build_post_schedule_section()

    logger.info("=== REPORT === Checking tokens...")
    token_statuses = await check_all_tokens()
    token_section = _build_token_section(token_statuses)

    logger.info("=== REPORT === Building HTML...")
    html = _build_html(today_stats, month_data, date_str, token_section, post_schedule_section)

    logger.info("=== REPORT === Sending via Resend API...")
    await _send_email(f"I'M IN — Звіт за {date_str}", html)

    expiring = [t for t in token_statuses if t.days_remaining is not None and t.days_remaining <= 5]
    if expiring:
        logger.warning("=== REPORT === Tokens expiring soon: %s",
                        ", ".join(f"{t.platform} ({t.days_remaining}d)" for t in expiring))
        urgent_html = _build_token_urgent_email(expiring)
        days_list = ", ".join(f"{t.platform} ({t.days_remaining}д)" for t in expiring)
        await _send_email(
            f"🚨 УВАГА: Токени закінчуються! {days_list}",
            urgent_html,
        )
