"""Build and send geo-research results as HTML emails."""
from __future__ import annotations

import json
import logging

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


def build_geo_email_html(
    request_id: str, lat: float, lon: float, name: str,
    language: str, received_at: str, completed_at: str,
    processing_secs: float, result: dict,
) -> str:
    """Build dark-themed HTML email for a single geo-research result."""

    history_rows = ""
    for h in result.get("history", []):
        history_rows += (
            '<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;color:#60a5fa;'
            f'white-space:nowrap;vertical-align:top;font-weight:600">{h["period"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">{h["description"]}</td>'
            '</tr>'
        )

    type_emojis = {
        "museum": "\U0001f3db\ufe0f", "theater": "\U0001f3ad", "hotel": "\U0001f3e8",
        "restaurant": "\U0001f37d\ufe0f", "park": "\U0001f333", "monument": "\U0001f5ff",
        "church": "\u26ea", "market": "\U0001f3ea",
    }

    places_rows = ""
    for p in result.get("places", []):
        emoji = type_emojis.get(p.get("type", ""), "\U0001f4cd")
        url_link = (
            f'<a href="{p["url"]}" style="color:#60a5fa">{p["url"]}</a>'
            if p.get("url") else "\u2014"
        )
        places_rows += (
            '<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0;vertical-align:top">'
            f'{emoji} <strong>{p["name"]}</strong><br>'
            f'<span style="color:#9ca3af;font-size:12px">{p.get("type", "")}</span></td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">{p["description"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;font-size:13px">{url_link}</td>'
            '</tr>'
        )

    news_rows = ""
    for n in result.get("news", []):
        source = (
            f'<br><span style="color:#9ca3af;font-size:12px">Джерело: {n["source"]}</span>'
            if n.get("source") else ""
        )
        news_rows += (
            '<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">'
            f'<strong>{n["title"]}</strong>{source}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">{n["description"]}</td>'
            '</tr>'
        )

    maps_url = f"https://maps.google.com/?q={lat},{lon}"
    total_chars = len(json.dumps(result, ensure_ascii=False))
    display_name = name or f"{lat}, {lon}"

    history_block = (
        f'<div style="margin-bottom:20px">'
        f'<h2 style="font-size:17px;color:#f59e0b;margin-bottom:12px">\U0001f4dc Історія</h2>'
        f'<table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">'
        f'{history_rows}</table></div>'
        if history_rows else ""
    )
    places_block = (
        f'<div style="margin-bottom:20px">'
        f'<h2 style="font-size:17px;color:#10b981;margin-bottom:12px">\U0001f4cd Визначні місця</h2>'
        f'<table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">'
        f'{places_rows}</table></div>'
        if places_rows else ""
    )
    news_block = (
        f'<div style="margin-bottom:20px">'
        f'<h2 style="font-size:17px;color:#ef4444;margin-bottom:12px">\U0001f4f0 Новини</h2>'
        f'<table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">'
        f'{news_rows}</table></div>'
        if news_rows else ""
    )

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                max-width:700px;margin:0 auto;background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">
        <div style="background:linear-gradient(135deg,#0f3460,#16213e);padding:24px 28px">
            <h1 style="margin:0;font-size:22px;color:#fff">\U0001f30d Гео-дослідження: {display_name}</h1>
            <p style="margin:8px 0 0;color:#9ca3af;font-size:14px">Geo Research Agent — Результат обробки</p>
        </div>
        <div style="padding:20px 28px">
            <table style="width:100%;border-collapse:collapse;margin-bottom:20px;background:#16213e;border-radius:8px">
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px">Request ID</td>
                    <td style="padding:10px 14px;color:#e0e0e0;font-family:monospace;font-size:13px">{request_id}</td></tr>
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">Координати</td>
                    <td style="padding:10px 14px;border-top:1px solid #333">
                        <a href="{maps_url}" style="color:#60a5fa">{lat}, {lon}</a></td></tr>
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">Мова</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{language}</td></tr>
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">\U0001f4e5 Отримано</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{received_at}</td></tr>
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">\U0001f4e4 Оброблено</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{completed_at}</td></tr>
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">\u23f1\ufe0f Час обробки</td>
                    <td style="padding:10px 14px;color:#4ade80;border-top:1px solid #333;font-weight:600">{processing_secs:.1f} сек</td></tr>
                <tr><td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">\U0001f4cf Розмір</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{total_chars} символів</td></tr>
            </table>
            <div style="background:#16213e;border-radius:8px;padding:16px 20px;margin-bottom:20px">
                <h2 style="margin:0 0 12px;font-size:17px;color:#60a5fa">\U0001f4dd Опис місцевості</h2>
                <p style="margin:0;line-height:1.7;color:#e0e0e0">{result.get("summary", "\u2014")}</p>
            </div>
            {history_block}
            {places_block}
            {news_block}
            <div style="text-align:center;padding:16px;color:#9ca3af;font-size:12px;border-top:1px solid #333;margin-top:20px">
                I'M IN \u2014 Geo Research Agent | <a href="https://www.im-in.net" style="color:#60a5fa">www.im-in.net</a>
            </div>
        </div>
    </div>"""


async def send_geo_result_email(
    request_id: str, lat: float, lon: float, name: str,
    language: str, received_at: str, completed_at: str,
    processing_secs: float, result: dict,
) -> bool:
    """Send a single geo-research result as email. Returns True on success."""
    if not settings.resend_api_key or not settings.report_email_to:
        logger.warning("Resend not configured — cannot send geo email")
        return False

    html = build_geo_email_html(
        request_id=request_id, lat=lat, lon=lon, name=name,
        language=language, received_at=received_at, completed_at=completed_at,
        processing_secs=processing_secs, result=result,
    )

    display_name = name or f"{lat}, {lon}"
    subject = f"\U0001f30d Geo Research: {display_name}"

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
            logger.info("Geo email sent: %s (id=%s)", subject, resp.json().get("id"))
            return True
        else:
            logger.error("Geo email failed %d: %s", resp.status_code, resp.text[:200])
            return False
