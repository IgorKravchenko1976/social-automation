"""Multi-server uptime monitor with combined email alerting.

Monitors two servers in parallel:
  Server 1 (www.im-in.net) — Node.js app + static site
  Server 2 (api-v2.im-in.net) — Go API + PostgreSQL

On failure: sends ONE combined alert email for all servers via Resend.
On recovery: sends recovery email when all checks pass again.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import httpx

from config.settings import settings, get_now_local

logger = logging.getLogger("server_monitor")

CHECK_INTERVAL_OK = 5
CHECK_INTERVAL_FAIL = 1
EMAIL_COOLDOWN = 300
REQUEST_TIMEOUT = 10

RESEND_URL = "https://api.resend.com/emails"

# ---------------------------------------------------------------------------
# Server definitions
# ---------------------------------------------------------------------------

SERVERS: dict[str, dict] = {
    "im-in-main": {
        "label": "I'M IN Main",
        "host": "www.im-in.net",
        "port": 443,
        "checks": {
            "tcp_connect": {
                "name": "TCP з'єднання (ping)",
                "type": "tcp",
            },
            "api_docs": {
                "name": "API (api-docs)",
                "url": "https://www.im-in.net/compass-app-node/v3/api-docs",
            },
            "website": {
                "name": "Веб-сайт (index.html)",
                "url": "https://www.im-in.net/index.html",
            },
            "swagger_ui": {
                "name": "Swagger UI",
                "url": "https://www.im-in.net/webjars/swagger-ui/index.html",
            },
        },
    },
    "im-in-api-v2": {
        "label": "I'M IN API v2",
        "host": "api-v2.im-in.net",
        "port": 443,
        "checks": {
            "tcp_connect": {
                "name": "TCP з'єднання (ping)",
                "type": "tcp",
            },
            "api_health": {
                "name": "API + DB (health)",
                "url": "https://api-v2.im-in.net/v1/api/health",
                "expect_json": {"status": "ok"},
            },
            "api_ping": {
                "name": "API liveness (ping)",
                "url": "https://api-v2.im-in.net/v1/api/ping",
            },
            "api_docs": {
                "name": "API Docs (Swagger)",
                "url": "https://api-v2.im-in.net/api/v1/docs",
            },
        },
    },
    "im-in-api-v21": {
        "label": "I'M IN API v2.1 (Hetzner)",
        "host": "api-v21.im-in.net",
        "port": 443,
        "checks": {
            "tcp_connect": {
                "name": "TCP з'єднання (ping)",
                "type": "tcp",
            },
            "api_health": {
                "name": "API + DB (health)",
                "url": "https://api-v21.im-in.net/v1/api/health",
                "expect_json": {"status": "ok"},
            },
            "api_ping": {
                "name": "API liveness (ping)",
                "url": "https://api-v21.im-in.net/v1/api/ping",
            },
            "api_docs": {
                "name": "API Docs (Swagger)",
                "url": "https://api-v21.im-in.net/api/v1/docs",
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Check types
# ---------------------------------------------------------------------------

class CheckStatus(str, Enum):
    OK = "ok"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    server_id: str
    check_id: str
    status: CheckStatus
    response_ms: float = 0.0
    error: str = ""
    status_code: int = 0
    checked_at: str = ""


@dataclass
class MonitorState:
    results: dict[str, dict[str, CheckResult]] = field(default_factory=dict)
    last_alert_time: float = 0.0
    last_recovery_time: float = 0.0
    was_failing: bool = False
    running: bool = False
    total_checks: int = 0
    total_failures: int = 0


_state = MonitorState()


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

async def _check_tcp(server_id: str, host: str, port: int) -> CheckResult:
    t0 = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(REQUEST_TIMEOUT)
        await loop.run_in_executor(None, sock.connect, (host, port))
        sock.close()
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(server_id, "tcp_connect", CheckStatus.OK,
                           response_ms=round(ms, 1),
                           checked_at=get_now_local().isoformat())
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(server_id, "tcp_connect", CheckStatus.FAIL,
                           response_ms=round(ms, 1), error=str(e)[:200],
                           checked_at=get_now_local().isoformat())


async def _check_http(server_id: str, check_id: str, url: str,
                      expect_json: dict | None = None) -> CheckResult:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=True,
                                     follow_redirects=True) as client:
            resp = await client.get(url)
        ms = (time.monotonic() - t0) * 1000

        if not (200 <= resp.status_code < 400):
            return CheckResult(server_id, check_id, CheckStatus.FAIL,
                               response_ms=round(ms, 1),
                               status_code=resp.status_code,
                               error=f"HTTP {resp.status_code}",
                               checked_at=get_now_local().isoformat())

        if expect_json:
            try:
                body = resp.json()
                for key, expected_val in expect_json.items():
                    actual = body.get(key)
                    if actual != expected_val:
                        return CheckResult(
                            server_id, check_id, CheckStatus.FAIL,
                            response_ms=round(ms, 1),
                            status_code=resp.status_code,
                            error=f"JSON {key}={actual!r}, expected {expected_val!r}",
                            checked_at=get_now_local().isoformat())
            except Exception:
                return CheckResult(server_id, check_id, CheckStatus.FAIL,
                                   response_ms=round(ms, 1),
                                   status_code=resp.status_code,
                                   error="Response is not valid JSON",
                                   checked_at=get_now_local().isoformat())

        return CheckResult(server_id, check_id, CheckStatus.OK,
                           response_ms=round(ms, 1),
                           status_code=resp.status_code,
                           checked_at=get_now_local().isoformat())
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(server_id, check_id, CheckStatus.FAIL,
                           response_ms=round(ms, 1), error=str(e)[:200],
                           checked_at=get_now_local().isoformat())


# ---------------------------------------------------------------------------
# Run all checks across all servers
# ---------------------------------------------------------------------------

async def run_all_checks() -> list[CheckResult]:
    tasks = []
    for server_id, server in SERVERS.items():
        host = server["host"]
        port = server["port"]
        for check_id, info in server["checks"].items():
            if info.get("type") == "tcp":
                tasks.append(_check_tcp(server_id, host, port))
            else:
                tasks.append(_check_http(
                    server_id, check_id, info["url"],
                    expect_json=info.get("expect_json"),
                ))

    raw = await asyncio.gather(*tasks, return_exceptions=True)
    parsed: list[CheckResult] = []
    for r in raw:
        if isinstance(r, Exception):
            parsed.append(CheckResult("unknown", "unknown", CheckStatus.FAIL,
                                      error=str(r)[:200],
                                      checked_at=get_now_local().isoformat()))
        else:
            parsed.append(r)

    _state.total_checks += 1
    for r in parsed:
        _state.results.setdefault(r.server_id, {})[r.check_id] = r

    return parsed


# ---------------------------------------------------------------------------
# Email: combined alert for all servers
# ---------------------------------------------------------------------------

_STATUS_EMOJI = {"ok": "✅", "fail": "❌"}


def _build_server_status_table(server_id: str, server: dict,
                                results: list[CheckResult]) -> tuple[str, bool]:
    """Build HTML table for one server. Returns (html, has_failures)."""
    server_results = [r for r in results if r.server_id == server_id]
    failures = [r for r in server_results if r.status == CheckStatus.FAIL]
    all_ok = len(failures) == 0

    if all_ok:
        header_bg = "#059669"
        header_text = "✅ ПРАЦЮЄ"
        row_border = "#d1fae5"
    else:
        header_bg = "#dc2626"
        header_text = f"❌ {len(failures)} ПОМИЛОК"
        row_border = "#fee2e2"

    rows = ""
    for check_id, info in server["checks"].items():
        r = next((x for x in server_results if x.check_id == check_id), None)
        if not r:
            continue
        emoji = _STATUS_EMOJI.get(r.status.value, "❓")
        err = f'<span style="color:#dc2626;">{r.error}</span>' if r.error else ""
        rows += f"""
        <tr>
            <td style="padding:6px 12px;border-bottom:1px solid {row_border};">{emoji} {info['name']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid {row_border};">{r.response_ms:.0f} ms</td>
            <td style="padding:6px 12px;border-bottom:1px solid {row_border};">{err}</td>
        </tr>"""

    html = f"""
    <div style="margin-bottom:16px;">
        <div style="background:{header_bg};color:white;padding:12px 16px;border-radius:8px 8px 0 0;">
            <strong>{server['label']}</strong> ({server['host']}) — {header_text}
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e7eb;border-top:0;">
            <thead><tr style="background:#f3f4f6;">
                <th style="padding:6px 12px;text-align:left;">Перевірка</th>
                <th style="padding:6px 12px;text-align:left;">Час</th>
                <th style="padding:6px 12px;text-align:left;">Помилка</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""

    return html, len(failures) > 0


async def _send_alert_email(results: list[CheckResult]) -> None:
    if not settings.resend_api_key or not settings.report_email_to:
        logger.warning("Resend not configured — cannot send monitoring alert")
        return

    now = get_now_local()

    server_tables = ""
    failing_servers = []
    for server_id, server in SERVERS.items():
        table_html, has_fail = _build_server_status_table(server_id, server, results)
        server_tables += table_html
        if has_fail:
            failing_servers.append(server["label"])

    if not failing_servers:
        return

    subject_parts = []
    for sid, srv in SERVERS.items():
        srv_results = [r for r in results if r.server_id == sid]
        srv_fails = [r for r in srv_results if r.status == CheckStatus.FAIL]
        emoji = "❌" if srv_fails else "✅"
        subject_parts.append(f"{emoji} {srv['label']}")

    subject = f"🚨 Моніторинг: {' | '.join(subject_parts)}"

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:680px;margin:0 auto;">
        <div style="background:#1e1e2e;color:white;padding:20px 24px;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-size:18px;">🚨 МОНІТОРИНГ СЕРВЕРІВ</h1>
            <p style="margin:8px 0 0;opacity:0.8;font-size:13px;">
                {now.strftime('%Y-%m-%d %H:%M:%S')} (Kyiv) — {len(failing_servers)} сервер(и) з проблемами
            </p>
        </div>
        <div style="padding:20px 24px;background:#fafafa;border:1px solid #e5e7eb;border-top:0;">
            {server_tables}
        </div>
        <div style="background:#fff7ed;padding:12px 24px;border:1px solid #fed7aa;border-top:0;font-size:12px;color:#9a3412;">
            ⏱ Наступний лист через 5 хв якщо проблема не зникне.
            📊 Перевірок: {_state.total_checks} | Збоїв: {_state.total_failures}
        </div>
        <div style="padding:12px 24px;font-size:11px;color:#9ca3af;border-radius:0 0 12px 12px;">
            I'M IN Server Monitor — {len(SERVERS)} servers
        </div>
    </div>
    """

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                RESEND_URL,
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
                logger.info("Alert email sent (id=%s)", resp.json().get("id"))
            else:
                logger.error("Resend API error %d: %s", resp.status_code, resp.text[:300])
    except Exception:
        logger.exception("Failed to send monitoring alert email")


async def _send_recovery_email(results: list[CheckResult]) -> None:
    if not settings.resend_api_key or not settings.report_email_to:
        return

    now = get_now_local()

    server_tables = ""
    for server_id, server in SERVERS.items():
        table_html, _ = _build_server_status_table(server_id, server, results)
        server_tables += table_html

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:680px;margin:0 auto;">
        <div style="background:#059669;color:white;padding:20px 24px;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-size:18px;">✅ ВСІ СЕРВЕРИ ВІДНОВЛЕНО!</h1>
            <p style="margin:8px 0 0;opacity:0.9;font-size:13px;">
                {now.strftime('%Y-%m-%d %H:%M:%S')} (Kyiv) — всі перевірки пройшли успішно
            </p>
        </div>
        <div style="padding:20px 24px;background:#fafafa;border:1px solid #e5e7eb;border-top:0;">
            {server_tables}
        </div>
        <div style="padding:12px 24px;font-size:11px;color:#9ca3af;border-radius:0 0 12px 12px;">
            I'M IN Server Monitor — {len(SERVERS)} servers
        </div>
    </div>
    """

    server_names = " + ".join(s["label"] for s in SERVERS.values())
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.report_email_from,
                    "to": [settings.report_email_to],
                    "subject": f"✅ Сервери відновлено! ({server_names})",
                    "html": html,
                },
            )
            if resp.status_code in (200, 201):
                logger.info("Recovery email sent (id=%s)", resp.json().get("id"))
            else:
                logger.error("Resend API error %d: %s", resp.status_code, resp.text[:300])
    except Exception:
        logger.exception("Failed to send recovery email")


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------

async def _monitor_cycle() -> None:
    results = await run_all_checks()
    failures = [r for r in results if r.status == CheckStatus.FAIL]
    now_ts = time.monotonic()

    if failures:
        _state.total_failures += 1
        fail_summary = []
        for sid, srv in SERVERS.items():
            srv_fails = [f for f in failures if f.server_id == sid]
            if srv_fails:
                names = ", ".join(
                    srv["checks"].get(f.check_id, {}).get("name", f.check_id)
                    for f in srv_fails
                )
                fail_summary.append(f"{srv['label']}: {names}")
        logger.warning("MONITOR FAIL [%d/%d]: %s",
                        len(failures), len(results), " | ".join(fail_summary))

        if now_ts - _state.last_alert_time >= EMAIL_COOLDOWN:
            await _send_alert_email(results)
            _state.last_alert_time = now_ts

        if not _state.was_failing:
            _state.was_failing = True
    else:
        if _state.was_failing:
            logger.info("MONITOR RECOVERED — all checks passed on all servers")
            await _send_recovery_email(results)
            _state.was_failing = False
            _state.last_recovery_time = now_ts

        if _state.total_checks % 720 == 0:
            ms_list = ", ".join(
                f"{r.server_id}/{r.check_id}={r.response_ms:.0f}ms" for r in results
            )
            logger.info("MONITOR OK [check #%d]: %s", _state.total_checks, ms_list)


async def send_test_email() -> str:
    """Run all checks and send a combined status email (works even if all OK)."""
    if not settings.resend_api_key or not settings.report_email_to:
        return "Resend not configured (RESEND_API_KEY or REPORT_EMAIL_TO missing)"

    results = await run_all_checks()
    now = get_now_local()
    failures = [r for r in results if r.status == CheckStatus.FAIL]

    server_tables = ""
    for server_id, server in SERVERS.items():
        table_html, _ = _build_server_status_table(server_id, server, results)
        server_tables += table_html

    if failures:
        header_bg = "#dc2626"
        header_title = f"🚨 {len(failures)} ПЕРЕВІРОК НЕ ПРОЙШЛИ"
    else:
        header_bg = "#059669"
        header_title = "✅ ВСІ СЕРВЕРИ ПРАЦЮЮТЬ"

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:680px;margin:0 auto;">
        <div style="background:{header_bg};color:white;padding:20px 24px;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-size:18px;">{header_title}</h1>
            <p style="margin:8px 0 0;opacity:0.8;font-size:13px;">
                📧 Тестовий звіт моніторингу — {now.strftime('%Y-%m-%d %H:%M:%S')} (Kyiv)
            </p>
        </div>
        <div style="padding:20px 24px;background:#fafafa;border:1px solid #e5e7eb;border-top:0;">
            {server_tables}
        </div>
        <div style="background:#f0f9ff;padding:12px 24px;border:1px solid #bae6fd;border-top:0;font-size:12px;color:#0369a1;">
            ℹ️ Це тестовий лист. Моніторинг перевіряє {len(SERVERS)} серверів кожні {CHECK_INTERVAL_OK}с.
            Алерти надсилаються при збоях з кулдауном {EMAIL_COOLDOWN // 60} хв.
        </div>
        <div style="padding:12px 24px;font-size:11px;color:#9ca3af;border-radius:0 0 12px 12px;">
            I'M IN Server Monitor — {len(SERVERS)} servers
        </div>
    </div>
    """

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.report_email_from,
                    "to": [settings.report_email_to],
                    "subject": f"📧 Тест моніторингу: {header_title}",
                    "html": html,
                },
            )
            if resp.status_code in (200, 201):
                email_id = resp.json().get("id", "")
                logger.info("Test monitoring email sent (id=%s)", email_id)
                return f"Email sent (id={email_id})"
            else:
                err = f"Resend API error {resp.status_code}: {resp.text[:300]}"
                logger.error(err)
                return err
    except Exception as e:
        logger.exception("Failed to send test monitoring email")
        return f"Error: {e}"


async def start_monitor_loop() -> None:
    _state.running = True
    server_names = ", ".join(s["host"] for s in SERVERS.values())
    logger.info("Server monitor started: checking [%s] every %ds (alert cooldown: %ds)",
                server_names, CHECK_INTERVAL_OK, EMAIL_COOLDOWN)

    while _state.running:
        try:
            await _monitor_cycle()
        except Exception:
            logger.exception("Monitor cycle error")

        interval = CHECK_INTERVAL_FAIL if _state.was_failing else CHECK_INTERVAL_OK
        await asyncio.sleep(interval)


def stop_monitor() -> None:
    _state.running = False
    logger.info("Server monitor stopping...")


def get_monitor_status() -> dict:
    now = get_now_local()
    servers = {}

    for server_id, server in SERVERS.items():
        checks = {}
        server_results = _state.results.get(server_id, {})
        for check_id, info in server["checks"].items():
            r = server_results.get(check_id)
            checks[check_id] = {
                "name": info["name"],
                "url": info.get("url", f"tcp://{server['host']}:{server['port']}"),
                "status": r.status.value if r else "unknown",
                "response_ms": r.response_ms if r else 0,
                "error": r.error if r else "",
                "status_code": r.status_code if r else 0,
                "last_check": r.checked_at if r else "",
            }
        all_ok = all(
            r.status == CheckStatus.OK for r in server_results.values()
        ) if server_results else False

        servers[server_id] = {
            "label": server["label"],
            "host": server["host"],
            "status": "ok" if all_ok else ("fail" if server_results else "starting"),
            "checks": checks,
        }

    global_ok = all(s["status"] == "ok" for s in servers.values()) if servers else False

    return {
        "status": "ok" if global_ok else ("fail" if _state.results else "starting"),
        "servers": servers,
        "check_interval_sec": CHECK_INTERVAL_FAIL if _state.was_failing else CHECK_INTERVAL_OK,
        "email_cooldown_sec": EMAIL_COOLDOWN,
        "total_checks": _state.total_checks,
        "total_failures": _state.total_failures,
        "is_failing": _state.was_failing,
        "monitor_running": _state.running,
        "checked_at": now.isoformat(),
    }
