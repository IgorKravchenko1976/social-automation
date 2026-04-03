"""Send geo-research results as individual emails via Resend API."""
import asyncio
import json
import uuid
from datetime import datetime, timezone


def _build_html(request_id: str, lat: float, lon: float, name: str,
                language: str, received_at: str, completed_at: str,
                processing_secs: float, result: dict) -> str:
    """Build HTML email for a single geo-research result."""

    history_rows = ""
    for h in result.get("history", []):
        history_rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #333;color:#60a5fa;white-space:nowrap;vertical-align:top;font-weight:600">{h['period']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">{h['description']}</td>
        </tr>"""

    places_rows = ""
    for p in result.get("places", []):
        type_emoji = {
            "museum": "🏛️", "theater": "🎭", "hotel": "🏨", "restaurant": "🍽️",
            "park": "🌳", "monument": "🗿", "church": "⛪", "market": "🏪",
        }.get(p.get("type", ""), "📍")
        url_link = f'<a href="{p["url"]}" style="color:#60a5fa">{p["url"]}</a>' if p.get("url") else "—"
        places_rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0;vertical-align:top">
                {type_emoji} <strong>{p['name']}</strong><br>
                <span style="color:#9ca3af;font-size:12px">{p.get('type', '')}</span>
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">{p['description']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #333;font-size:13px">{url_link}</td>
        </tr>"""

    news_rows = ""
    for n in result.get("news", []):
        source = f'<br><span style="color:#9ca3af;font-size:12px">Джерело: {n["source"]}</span>' if n.get("source") else ""
        news_rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">
                <strong>{n['title']}</strong>{source}
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #333;color:#e0e0e0">{n['description']}</td>
        </tr>"""

    maps_url = f"https://maps.google.com/?q={lat},{lon}"
    total_chars = len(json.dumps(result, ensure_ascii=False))

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">

        <div style="background:linear-gradient(135deg,#0f3460,#16213e);padding:24px 28px">
            <h1 style="margin:0;font-size:22px;color:#fff">🌍 Гео-дослідження: {name or f'{lat}, {lon}'}</h1>
            <p style="margin:8px 0 0;color:#9ca3af;font-size:14px">Geo Research Agent — Результат обробки</p>
        </div>

        <div style="padding:20px 28px">

            <table style="width:100%;border-collapse:collapse;margin-bottom:20px;background:#16213e;border-radius:8px">
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px">Request ID</td>
                    <td style="padding:10px 14px;color:#e0e0e0;font-family:monospace;font-size:13px">{request_id}</td>
                </tr>
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">Координати</td>
                    <td style="padding:10px 14px;border-top:1px solid #333">
                        <a href="{maps_url}" style="color:#60a5fa">{lat}, {lon}</a>
                    </td>
                </tr>
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">Мова</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{language}</td>
                </tr>
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">📥 Отримано</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{received_at}</td>
                </tr>
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">📤 Оброблено</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{completed_at}</td>
                </tr>
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">⏱️ Час обробки</td>
                    <td style="padding:10px 14px;color:#4ade80;border-top:1px solid #333;font-weight:600">{processing_secs:.1f} сек</td>
                </tr>
                <tr>
                    <td style="padding:10px 14px;color:#9ca3af;font-size:13px;border-top:1px solid #333">📏 Розмір</td>
                    <td style="padding:10px 14px;color:#e0e0e0;border-top:1px solid #333">{total_chars} символів</td>
                </tr>
            </table>

            <div style="background:#16213e;border-radius:8px;padding:16px 20px;margin-bottom:20px">
                <h2 style="margin:0 0 12px;font-size:17px;color:#60a5fa">📝 Опис місцевості</h2>
                <p style="margin:0;line-height:1.7;color:#e0e0e0">{result.get('summary', '—')}</p>
            </div>

            {'<div style="margin-bottom:20px"><h2 style="font-size:17px;color:#f59e0b;margin-bottom:12px">📜 Історія</h2><table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">' + history_rows + '</table></div>' if history_rows else ''}

            {'<div style="margin-bottom:20px"><h2 style="font-size:17px;color:#10b981;margin-bottom:12px">📍 Визначні місця</h2><table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">' + places_rows + '</table></div>' if places_rows else ''}

            {'<div style="margin-bottom:20px"><h2 style="font-size:17px;color:#ef4444;margin-bottom:12px">📰 Новини</h2><table style="width:100%;border-collapse:collapse;background:#16213e;border-radius:8px">' + news_rows + '</table></div>' if news_rows else ''}

            <div style="text-align:center;padding:16px;color:#9ca3af;font-size:12px;border-top:1px solid #333;margin-top:20px">
                I'M IN — Geo Research Agent | <a href="https://www.im-in.net" style="color:#60a5fa">www.im-in.net</a>
            </div>
        </div>
    </div>
    """


async def main():
    import os
    os.environ.setdefault("DATA_DIR", "/tmp/geo-test-data")

    from config.settings import settings
    from db.database import init_db, async_session
    from db.models import GeoResearchTask, GeoResearchStatus
    from geo_agent.researcher import research_location

    import httpx

    if not settings.resend_api_key:
        print("ERROR: RESEND_API_KEY not set")
        return
    if not settings.report_email_to:
        print("ERROR: REPORT_EMAIL_TO not set")
        return

    print(f"Sending to: {settings.report_email_to}")

    await init_db()

    today_posts = [
        {"lat": -20.2833, "lon": 57.4333, "name": "Ланкеві, Маврикій"},
        {"lat": 32.0809, "lon": -81.0912, "name": "Savannah, Georgia, USA"},
    ]

    for i, post in enumerate(today_posts, 1):
        request_id = str(uuid.uuid4())
        received_at = datetime.now(timezone.utc)

        print(f"\n[{i}/{len(today_posts)}] Досліджую {post['name']}...")

        async with async_session() as session:
            task = GeoResearchTask(
                request_id=request_id,
                latitude=post["lat"],
                longitude=post["lon"],
                name=post["name"],
                language="uk",
                status=GeoResearchStatus.PROCESSING,
                received_at=received_at,
            )
            session.add(task)
            await session.commit()

        result = await research_location(
            latitude=post["lat"],
            longitude=post["lon"],
            name=post["name"],
            language="uk",
        )

        completed_at = datetime.now(timezone.utc)
        processing_secs = (completed_at - received_at).total_seconds()

        if not result:
            print(f"  Пусто — пропускаю")
            continue

        print(f"  Готово за {processing_secs:.1f}s, формую email...")

        html = _build_html(
            request_id=request_id,
            lat=post["lat"],
            lon=post["lon"],
            name=post["name"],
            language="uk",
            received_at=received_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            completed_at=completed_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            processing_secs=processing_secs,
            result=result,
        )

        subject = f"🌍 Geo Research: {post['name']} ({post['lat']}, {post['lon']})"

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
                email_id = resp.json().get("id", "?")
                print(f"  ✅ Email відправлено! (id={email_id})")
            else:
                print(f"  ❌ Помилка {resp.status_code}: {resp.text}")

    print(f"\nГотово!")


if __name__ == "__main__":
    asyncio.run(main())
