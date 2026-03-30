"""Server & API uptime monitor with email alerting.

Checks every few seconds:
  1. TCP connectivity to www.im-in.net (ping substitute)
  2. API health — GET /compass-app-node/v3/api-docs
  3. Website — GET /index.html
  4. Swagger UI — GET /webjars/swagger-ui/index.html

On failure: sends alert email via Resend (cooldown = 5 min between emails).
On recovery: sends recovery email.
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
EMAIL_COOLDOWN = 300  # 5 minutes between alert emails
REQUEST_TIMEOUT = 10

TARGET_HOST = "www.im-in.net"
TARGET_PORT = 443

CHECKS = {
    "tcp_connect": {
        "name": "TCP з'єднання (ping)",
        "description": f"TCP connect to {TARGET_HOST}:{TARGET_PORT}",
    },
    "api_docs": {
        "name": "API (api-docs)",
        "url": f"https://{TARGET_HOST}/compass-app-node/v3/api-docs",
    },
    "website": {
        "name": "Веб-сайт (index.html)",
        "url": f"https://{TARGET_HOST}/index.html",
    },
    "swagger_ui": {
        "name": "Swagger UI",
        "url": f"https://{TARGET_HOST}/webjars/swagger-ui/index.html",
    },
}


class CheckStatus(str, Enum):
    OK = "ok"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    check_id: str
    status: CheckStatus
    response_ms: float = 0.0
    error: str = ""
    status_code: int = 0
    checked_at: str = ""


@dataclass
class MonitorState:
    results: dict[str, CheckResult] = field(default_factory=dict)
    last_alert_time: float = 0.0
    last_recovery_time: float = 0.0
    was_failing: bool = False
    running: bool = False
    total_checks: int = 0
    total_failures: int = 0


_state = MonitorState()


async def _check_tcp() -> CheckResult:
    """TCP socket connect — tests basic network reachability."""
    t0 = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(REQUEST_TIMEOUT)
        await loop.run_in_executor(None, sock.connect, (TARGET_HOST, TARGET_PORT))
        sock.close()
        ms = (time.monotonic() - t0) * 1000
        return CheckResult("tcp_connect", CheckStatus.OK, response_ms=round(ms, 1),
                           checked_at=get_now_local().isoformat())
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return CheckResult("tcp_connect", CheckStatus.FAIL, response_ms=round(ms, 1),
                           error=str(e)[:200], checked_at=get_now_local().isoformat())


async def _check_http(check_id: str, url: str) -> CheckResult:
    """HTTP GET — checks that URL returns 2xx."""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=True,
                                     follow_redirects=True) as client:
            resp = await client.get(url)
        ms = (time.monotonic() - t0) * 1000
        if 200 <= resp.status_code < 400:
            return CheckResult(check_id, CheckStatus.OK, response_ms=round(ms, 1),
                               status_code=resp.status_code,
                               checked_at=get_now_local().isoformat())
        else:
            return CheckResult(check_id, CheckStatus.FAIL, response_ms=round(ms, 1),
                               status_code=resp.status_code,
                               error=f"HTTP {resp.status_code}",
                               checked_at=get_now_local().isoformat())
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return CheckResult(check_id, CheckStatus.FAIL, response_ms=round(ms, 1),
                           error=str(e)[:200], checked_at=get_now_local().isoformat())


async def run_all_checks() -> list[CheckResult]:
    """Execute all checks concurrently and return results."""
    tasks = [_check_tcp()]
    for check_id, info in CHECKS.items():
        if check_id == "tcp_connect":
            continue
        tasks.append(_check_http(check_id, info["url"]))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    parsed: list[CheckResult] = []
    for r in results:
        if isinstance(r, Exception):
            parsed.append(CheckResult("unknown", CheckStatus.FAIL, error=str(r)[:200],
                                      checked_at=get_now_local().isoformat()))
        else:
            parsed.append(r)

    _state.total_checks += 1
    for r in parsed:
        _state.results[r.check_id] = r

    return parsed


async def _send_alert_email(failures: list[CheckResult]) -> None:
    """Send alert email via Resend API."""
    if not settings.resend_api_key or not settings.report_email_to:
        logger.warning("Resend not configured — cannot send monitoring alert")
        return

    now = get_now_local()
    fail_rows = ""
    for f in failures:
        name = CHECKS.get(f.check_id, {}).get("name", f.check_id)
        url = CHECKS.get(f.check_id, {}).get("url", f"TCP {TARGET_HOST}:{TARGET_PORT}")
        fail_rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #fee2e2;font-weight:600;">{name}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #fee2e2;color:#dc2626;">{f.error or 'No response'}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #fee2e2;">{f.response_ms:.0f} ms</td>
            <td style="padding:8px 12px;border-bottom:1px solid #fee2e2;font-size:12px;color:#6b7280;">{url}</td>
        </tr>"""

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;">
        <div style="background:#dc2626;color:white;padding:20px 24px;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-size:20px;">🚨 СЕРВЕР НЕ ВІДПОВІДАЄ!</h1>
            <p style="margin:8px 0 0;opacity:0.9;font-size:14px;">
                {now.strftime('%Y-%m-%d %H:%M:%S')} (Kyiv) — {len(failures)} перевірок НЕ пройшли
            </p>
        </div>
        <div style="background:#fef2f2;padding:20px 24px;border:1px solid #fee2e2;">
            <table style="width:100%;border-collapse:collapse;font-size:14px;">
                <thead>
                    <tr style="background:#fecaca;">
                        <th style="padding:8px 12px;text-align:left;">Перевірка</th>
                        <th style="padding:8px 12px;text-align:left;">Помилка</th>
                        <th style="padding:8px 12px;text-align:left;">Час</th>
                        <th style="padding:8px 12px;text-align:left;">URL</th>
                    </tr>
                </thead>
                <tbody>{fail_rows}</tbody>
            </table>
        </div>
        <div style="background:#fff7ed;padding:16px 24px;border:1px solid #fed7aa;border-top:0;">
            <p style="margin:0;font-size:13px;color:#9a3412;">
                ⏱ Наступний лист буде через 5 хвилин, якщо проблема не зникне.<br>
                📊 Всього перевірок: {_state.total_checks} | Збоїв: {_state.total_failures}
            </p>
        </div>
        <div style="padding:16px 24px;font-size:12px;color:#6b7280;border-radius:0 0 12px 12px;">
            <p style="margin:0;">Моніторинг: <strong>{TARGET_HOST}</strong> | I'M IN Server Monitor</p>
        </div>
    </div>
    """

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
                    "to": [settings.report_email_to],
                    "subject": f"🚨 СЕРВЕР {TARGET_HOST} НЕ ВІДПОВІДАЄ! ({len(failures)} помилок)",
                    "html": html,
                },
            )
            if resp.status_code in (200, 201):
                logger.info("Alert email sent (id=%s)", resp.json().get("id"))
            else:
                logger.error("Resend API error %d: %s", resp.status_code, resp.text[:300])
    except Exception:
        logger.exception("Failed to send monitoring alert email")


async def _send_recovery_email() -> None:
    """Send recovery notification when all checks pass again."""
    if not settings.resend_api_key or not settings.report_email_to:
        return

    now = get_now_local()
    rows = ""
    for check_id, info in CHECKS.items():
        r = _state.results.get(check_id)
        if r:
            rows += f"""
            <tr>
                <td style="padding:8px 12px;border-bottom:1px solid #d1fae5;">{info['name']}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #d1fae5;color:#059669;">✅ OK</td>
                <td style="padding:8px 12px;border-bottom:1px solid #d1fae5;">{r.response_ms:.0f} ms</td>
            </tr>"""

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;">
        <div style="background:#059669;color:white;padding:20px 24px;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-size:20px;">✅ СЕРВЕР ВІДНОВЛЕНО!</h1>
            <p style="margin:8px 0 0;opacity:0.9;font-size:14px;">
                {now.strftime('%Y-%m-%d %H:%M:%S')} (Kyiv) — всі перевірки пройшли успішно
            </p>
        </div>
        <div style="background:#ecfdf5;padding:20px 24px;border:1px solid #d1fae5;">
            <table style="width:100%;border-collapse:collapse;font-size:14px;">
                <thead>
                    <tr style="background:#a7f3d0;">
                        <th style="padding:8px 12px;text-align:left;">Перевірка</th>
                        <th style="padding:8px 12px;text-align:left;">Статус</th>
                        <th style="padding:8px 12px;text-align:left;">Час відповіді</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        <div style="padding:16px 24px;font-size:12px;color:#6b7280;">
            <p style="margin:0;">Моніторинг: <strong>{TARGET_HOST}</strong> | I'M IN Server Monitor</p>
        </div>
    </div>
    """

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
                    "to": [settings.report_email_to],
                    "subject": f"✅ Сервер {TARGET_HOST} відновлено!",
                    "html": html,
                },
            )
            if resp.status_code in (200, 201):
                logger.info("Recovery email sent (id=%s)", resp.json().get("id"))
            else:
                logger.error("Resend API error %d: %s", resp.status_code, resp.text[:300])
    except Exception:
        logger.exception("Failed to send recovery email")


async def _monitor_cycle() -> None:
    """Single monitoring iteration: check → alert if needed."""
    results = await run_all_checks()
    failures = [r for r in results if r.status == CheckStatus.FAIL]
    now_ts = time.monotonic()

    if failures:
        _state.total_failures += 1
        names = ", ".join(CHECKS.get(f.check_id, {}).get("name", f.check_id) for f in failures)
        logger.warning("MONITOR FAIL [%d/%d]: %s", len(failures), len(results), names)

        if now_ts - _state.last_alert_time >= EMAIL_COOLDOWN:
            await _send_alert_email(failures)
            _state.last_alert_time = now_ts

        if not _state.was_failing:
            _state.was_failing = True
    else:
        if _state.was_failing:
            logger.info("MONITOR RECOVERED — all checks passed")
            await _send_recovery_email()
            _state.was_failing = False
            _state.last_recovery_time = now_ts

        if _state.total_checks % 720 == 0:  # ~every hour at 5s interval
            ms_list = ", ".join(f"{r.check_id}={r.response_ms:.0f}ms" for r in results)
            logger.info("MONITOR OK [check #%d]: %s", _state.total_checks, ms_list)


async def start_monitor_loop() -> None:
    """Background loop — runs until cancelled."""
    _state.running = True
    logger.info("Server monitor started: checking %s every %ds (alert cooldown: %ds)",
                TARGET_HOST, CHECK_INTERVAL_OK, EMAIL_COOLDOWN)

    while _state.running:
        try:
            await _monitor_cycle()
        except Exception:
            logger.exception("Monitor cycle error")

        interval = CHECK_INTERVAL_FAIL if _state.was_failing else CHECK_INTERVAL_OK
        await asyncio.sleep(interval)


def stop_monitor() -> None:
    """Signal the monitor loop to stop."""
    _state.running = False
    logger.info("Server monitor stopping...")


def get_monitor_status() -> dict:
    """Return current monitor state for API."""
    now = get_now_local()
    checks = {}
    for check_id, info in CHECKS.items():
        r = _state.results.get(check_id)
        checks[check_id] = {
            "name": info["name"],
            "url": info.get("url", f"tcp://{TARGET_HOST}:{TARGET_PORT}"),
            "status": r.status.value if r else "unknown",
            "response_ms": r.response_ms if r else 0,
            "error": r.error if r else "",
            "status_code": r.status_code if r else 0,
            "last_check": r.checked_at if r else "",
        }

    all_ok = all(
        r.status == CheckStatus.OK
        for r in _state.results.values()
    ) if _state.results else False

    return {
        "status": "ok" if all_ok else ("fail" if _state.results else "starting"),
        "target": TARGET_HOST,
        "check_interval_sec": CHECK_INTERVAL_FAIL if _state.was_failing else CHECK_INTERVAL_OK,
        "email_cooldown_sec": EMAIL_COOLDOWN,
        "total_checks": _state.total_checks,
        "total_failures": _state.total_failures,
        "is_failing": _state.was_failing,
        "monitor_running": _state.running,
        "checked_at": now.isoformat(),
        "checks": checks,
    }
