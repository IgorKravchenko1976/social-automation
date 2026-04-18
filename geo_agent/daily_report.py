"""Daily research report — sends summary email with stats, errors, and countries."""
from __future__ import annotations

import logging

import httpx

from config.settings import settings
from geo_agent import backend_client

logger = logging.getLogger(__name__)

REPORT_EMAIL = "igork2011@gmail.com"


def _build_poi_research_block(stats: dict) -> str:
    poi_today = stats.get("poiResearchedToday", 0)
    poi_total = stats.get("poiResearchedTotal", 0)
    poi_blocks = stats.get("poiBlocksTotal", 0)
    poi_pending = stats.get("poiPendingResearch", 0)

    if poi_today == 0 and poi_total == 0 and poi_pending == 0:
        return ""

    return f"""
        <div style="margin-bottom:20px">
            <h2 style="font-size:16px;color:#60a5fa;margin-bottom:8px">\U0001f50d POI Research (web search)</h2>
            <div style="background:#16213e;border-radius:8px;padding:16px">
                <table style="width:100%;border-collapse:collapse">
                    <tr>
                        <td style="padding:6px 8px;color:#e0e0e0">\U0001f4dd Досліджено сьогодні</td>
                        <td style="padding:6px 8px;color:#60a5fa;text-align:center;font-weight:600">{poi_today}</td>
                    </tr>
                    <tr>
                        <td style="padding:6px 8px;color:#e0e0e0">\U0001f4da Всього досліджено</td>
                        <td style="padding:6px 8px;color:#60a5fa;text-align:center">{poi_total}</td>
                    </tr>
                    <tr>
                        <td style="padding:6px 8px;color:#e0e0e0">\U0001f4c4 Блоків інформації</td>
                        <td style="padding:6px 8px;color:#60a5fa;text-align:center">{poi_blocks}</td>
                    </tr>
                    <tr>
                        <td style="padding:6px 8px;color:#e0e0e0">\u23f3 В черзі</td>
                        <td style="padding:6px 8px;color:#f59e0b;text-align:center;font-weight:600">{poi_pending}</td>
                    </tr>
                </table>
            </div>
        </div>"""


def _build_html(stats: dict) -> str:
    date = stats.get("date", "?")
    completed = stats.get("completedToday", 0)
    rejected = stats.get("rejectedToday", 0)
    errors = stats.get("errorsToday", 0)
    total_completed = stats.get("totalCompleted", 0)
    total_rejected = stats.get("totalRejected", 0)
    total_errors = stats.get("totalErrors", 0)

    countries = stats.get("countries") or []
    error_countries = stats.get("errorCountries") or []

    country_rows = ""
    for c in countries:
        country_rows += (
            f'<tr><td style="padding:6px 12px;border-bottom:1px solid #333;color:#e0e0e0">'
            f'{c["code"]}</td>'
            f'<td style="padding:6px 12px;border-bottom:1px solid #333;color:#4ade80;text-align:center">'
            f'{c["count"]}</td></tr>'
        )

    error_rows = ""
    for e in error_countries:
        reasons = e.get("reasons") or []
        reason_text = "; ".join(r[:80] for r in reasons[:3])
        error_rows += (
            f'<tr><td style="padding:6px 12px;border-bottom:1px solid #333;color:#e0e0e0">'
            f'{e["code"]}</td>'
            f'<td style="padding:6px 12px;border-bottom:1px solid #333;color:#ef4444;text-align:center">'
            f'{e["count"]}</td>'
            f'<td style="padding:6px 12px;border-bottom:1px solid #333;color:#9ca3af;font-size:12px">'
            f'{reason_text}</td></tr>'
        )

    countries_block = ""
    if country_rows:
        countries_block = f"""
        <div style="margin-bottom:20px">
            <h2 style="font-size:16px;color:#10b981;margin-bottom:8px">\U0001f30d Країни (за сьогодні)</h2>
            <table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">
                <tr style="color:#9ca3af;font-size:12px">
                    <th style="padding:6px 12px;text-align:left">Країна</th>
                    <th style="padding:6px 12px;text-align:center">Кількість</th>
                </tr>
                {country_rows}
            </table>
        </div>"""

    errors_block = ""
    if error_rows:
        errors_block = f"""
        <div style="margin-bottom:20px">
            <h2 style="font-size:16px;color:#ef4444;margin-bottom:8px">\u26a0\ufe0f Помилки (за сьогодні)</h2>
            <table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">
                <tr style="color:#9ca3af;font-size:12px">
                    <th style="padding:6px 12px;text-align:left">Країна</th>
                    <th style="padding:6px 12px;text-align:center">Кількість</th>
                    <th style="padding:6px 12px;text-align:left">Причина</th>
                </tr>
                {error_rows}
            </table>
        </div>"""

    status_color = "#4ade80" if errors == 0 else "#ef4444"
    status_text = "Усі дослідження пройшли перевірку" if errors == 0 else f"{errors} відбраковано"

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                max-width:600px;margin:0 auto;background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">
        <div style="background:linear-gradient(135deg,#0f3460,#16213e);padding:20px 24px">
            <h1 style="margin:0;font-size:20px;color:#fff">\U0001f4ca Щоденний звіт дослідника</h1>
            <p style="margin:4px 0 0;color:#9ca3af;font-size:14px">{date}</p>
        </div>
        <div style="padding:16px 24px">
            <div style="background:#16213e;border-radius:8px;padding:16px;margin-bottom:16px">
                <div style="display:flex;justify-content:space-between;margin-bottom:12px">
                    <span style="color:#9ca3af;font-size:13px">Статус</span>
                    <span style="color:{status_color};font-weight:600">{status_text}</span>
                </div>
                <table style="width:100%;border-collapse:collapse">
                    <tr style="color:#9ca3af;font-size:12px">
                        <th></th><th style="padding:4px 8px;text-align:center">Сьогодні</th>
                        <th style="padding:4px 8px;text-align:center">Всього</th>
                    </tr>
                    <tr>
                        <td style="padding:6px 8px;color:#e0e0e0">\u2705 Пройшли</td>
                        <td style="padding:6px 8px;color:#4ade80;text-align:center;font-weight:600">{completed}</td>
                        <td style="padding:6px 8px;color:#4ade80;text-align:center">{total_completed}</td>
                    </tr>
                    <tr>
                        <td style="padding:6px 8px;color:#e0e0e0">\u274c Відбраковано</td>
                        <td style="padding:6px 8px;color:#ef4444;text-align:center;font-weight:600">{rejected + errors}</td>
                        <td style="padding:6px 8px;color:#ef4444;text-align:center">{total_rejected + total_errors}</td>
                    </tr>
                </table>
            </div>
            {countries_block}
            {errors_block}
            {_build_poi_research_block(stats)}
            <div style="text-align:center;padding:12px;color:#9ca3af;font-size:11px;border-top:1px solid #333;margin-top:16px">
                I'M IN \u2014 Geo Research Bot | <a href="https://www.im-in.net" style="color:#60a5fa">www.im-in.net</a>
            </div>
        </div>
    </div>"""


async def send_daily_research_report() -> bool:
    """Fetch stats from backend and send daily email report."""
    try:
        stats = await backend_client.get_daily_stats()
        if stats.get("error"):
            logger.warning("[daily-report] Backend not configured, skipping")
            return False
    except Exception as exc:
        logger.warning("[daily-report] Failed to fetch stats: %s", exc)
        return False

    if not settings.resend_api_key:
        logger.warning("[daily-report] Resend not configured, skipping")
        return False

    html = _build_html(stats)
    date = stats.get("date", "?")
    completed = stats.get("completedToday", 0)
    errors = stats.get("errorsToday", 0) + stats.get("rejectedToday", 0)
    subject = f"\U0001f4ca Дослідник: {date} — {completed} done, {errors} errors"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.report_email_from,
                    "to": [REPORT_EMAIL],
                    "subject": subject,
                    "html": html,
                },
            )
            if resp.status_code in (200, 201):
                logger.info("[daily-report] Sent: %s", subject)
                return True
            else:
                logger.error("[daily-report] Failed %d: %s", resp.status_code, resp.text[:200])
                return False
    except Exception:
        logger.exception("[daily-report] Email send failed")
        return False
