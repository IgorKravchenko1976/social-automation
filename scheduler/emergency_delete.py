"""Emergency post deletion — find by text, delete from all platforms + blog, email report."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from config.platforms import Platform, configured_platforms, get_platform_instance, PLATFORM_LABELS
from config.settings import settings, get_now_local
from db.database import async_session
from db.models import Post, Publication, PostStatus

logger = logging.getLogger(__name__)


async def emergency_delete(search_text: str) -> dict:
    """Search for posts matching text, delete from all platforms and blog.

    Returns a detailed report dict.
    """
    logger.warning("🚨 EMERGENCY DELETE initiated — search: '%s'", search_text[:100])
    report = {
        "search_text": search_text[:200],
        "timestamp": get_now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "posts_found": 0,
        "results": [],
    }

    matching_posts = await _find_posts(search_text)
    report["posts_found"] = len(matching_posts)

    if not matching_posts:
        report["summary"] = "Пости не знайдено в базі даних"
        await _send_report_email(report)
        return report

    for post, publications in matching_posts:
        post_report = {
            "post_id": post.id,
            "title": (post.title or "")[:150],
            "content_preview": (post.content_raw or "")[:200],
            "created_at": str(post.created_at),
            "platforms": [],
        }

        for pub in publications:
            platform_result = await _delete_from_platform(pub)
            post_report["platforms"].append(platform_result)

        await _mark_post_deleted(post.id)

        blog_result = await _delete_from_blog(post.id)
        post_report["blog"] = blog_result

        report["results"].append(post_report)

    deleted_count = sum(
        1 for r in report["results"]
        for p in r["platforms"] if p["deleted"]
    )
    failed_count = sum(
        1 for r in report["results"]
        for p in r["platforms"] if not p["deleted"] and p["platform_post_id"]
    )
    report["summary"] = (
        f"Знайдено {report['posts_found']} пост(ів). "
        f"Видалено з {deleted_count} платформ(и). "
        f"Не вдалось видалити з {failed_count}."
    )

    await _send_report_email(report)
    logger.warning("🚨 EMERGENCY DELETE complete: %s", report["summary"])
    return report


async def _find_posts(search_text: str) -> list[tuple[Post, list[Publication]]]:
    """Find posts matching the search text (substring match in title, content, adapted text)."""
    search_lower = search_text.lower().strip()
    words = [w for w in search_lower.split() if len(w) >= 3]

    async with async_session() as session:
        query = (
            select(Post)
            .join(Publication)
            .where(Publication.status == PostStatus.PUBLISHED)
            .order_by(Post.created_at.desc())
            .limit(500)
        )
        result = await session.execute(query)
        all_posts = result.scalars().unique().all()

        matches = []
        for post in all_posts:
            text_blob = " ".join(filter(None, [
                post.title, post.content_raw, post.place_name,
            ])).lower()
            if search_lower in text_blob or all(w in text_blob for w in words[:5]):
                pubs_result = await session.execute(
                    select(Publication).where(Publication.post_id == post.id)
                )
                pubs = pubs_result.scalars().all()
                pub_texts = " ".join((p.content_adapted or "") for p in pubs).lower()
                if search_lower in text_blob or search_lower in pub_texts or all(w in (text_blob + " " + pub_texts) for w in words[:5]):
                    matches.append((post, list(pubs)))

        return matches


async def _delete_from_platform(pub: Publication) -> dict:
    """Delete a single publication from its platform."""
    result = {
        "platform": pub.platform,
        "platform_label": PLATFORM_LABELS.get(pub.platform, pub.platform),
        "platform_post_id": pub.platform_post_id,
        "status": pub.status.value if pub.status else "unknown",
        "deleted": False,
        "detail": "",
    }

    if not pub.platform_post_id:
        if pub.platform == "facebook":
            found_id = await _search_facebook_post(pub)
            if found_id:
                pub.platform_post_id = found_id
                result["platform_post_id"] = found_id
                result["detail"] = f"Знайдено через пошук: {found_id}"
            else:
                result["detail"] = "Немає platform_post_id і не знайдено через API пошук — видаліть вручну"
                return result
        else:
            result["detail"] = "Немає platform_post_id — видаліть вручну з платформи"
            return result

    try:
        platform_enum = Platform(pub.platform)
        active = set(p.value for p in configured_platforms())
        if pub.platform not in active:
            result["detail"] = f"Платформа {pub.platform} не активна (немає токена)"
            return result

        adapter = get_platform_instance(platform_enum)
        if hasattr(adapter, "delete_post"):
            success, detail = await adapter.delete_post(pub.platform_post_id)
            result["deleted"] = success
            result["detail"] = detail
        else:
            result["detail"] = f"Адаптер {pub.platform} не підтримує видалення"
    except Exception as e:
        result["detail"] = f"Помилка: {e}"

    return result


async def _search_facebook_post(pub: Publication) -> str | None:
    """Try to find a Facebook post by searching recent page feed."""
    try:
        from stats.token_renewer import get_active_token
        token = await get_active_token("facebook") or settings.facebook_page_access_token
        if not token or not settings.facebook_page_id:
            return None

        from config.platforms import FACEBOOK_GRAPH_API
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}/feed",
                params={"access_token": token, "fields": "id,message", "limit": 25},
            )
            data = resp.json()
            post_obj = await _get_post_by_pub(pub)
            if not post_obj:
                return None

            search_text = (post_obj.title or "").lower()[:50]
            for item in data.get("data", []):
                msg = (item.get("message") or "").lower()
                if search_text and search_text[:30] in msg:
                    logger.info("Found FB post by text search: %s", item["id"])
                    return item["id"]
    except Exception as e:
        logger.warning("Facebook post search failed: %s", e)
    return None


async def _get_post_by_pub(pub: Publication) -> Post | None:
    async with async_session() as session:
        result = await session.execute(select(Post).where(Post.id == pub.post_id))
        return result.scalar_one_or_none()


async def _delete_from_blog(post_id: int) -> dict:
    """Delete blog post HTML file and remove from posts.json index.

    Uses targeted SFTP deletion instead of full regeneration to avoid
    re-creating the post from DB records that may still be transitioning.
    """
    result = {"deleted": False, "detail": ""}

    blog_dir = Path(settings.data_dir) / "blog"
    post_file = blog_dir / f"post-{post_id}.html"
    thumb_file = blog_dir / f"thumb-{post_id}.jpg"
    posts_json = blog_dir / "posts.json"

    files_deleted = []

    if post_file.is_file():
        post_file.unlink()
        files_deleted.append(post_file.name)

    if thumb_file.is_file():
        thumb_file.unlink()
        files_deleted.append(thumb_file.name)

    if posts_json.is_file():
        try:
            posts = json.loads(posts_json.read_text(encoding="utf-8"))
            original_count = len(posts)
            posts = [p for p in posts if p.get("id") != post_id]
            if len(posts) < original_count:
                posts_json.write_text(
                    json.dumps(posts, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                files_deleted.append("posts.json (updated)")
        except Exception as e:
            result["detail"] = f"Помилка оновлення posts.json: {e}"

    if files_deleted:
        result["deleted"] = True
        result["detail"] = f"Видалено: {', '.join(files_deleted)}"

        try:
            vps_detail = _sftp_delete_post(post_id, posts_json if posts_json.is_file() else None)
            result["detail"] += f" | VPS: {vps_detail}"
        except Exception as e:
            result["detail"] += f" | VPS sync failed: {e}"
    else:
        result["detail"] = "Файли блогу не знайдено (можливо вже видалено)"

    return result


def _sftp_delete_post(post_id: int, updated_posts_json: Path | None) -> str:
    """Delete specific post files from VPS via SFTP and upload updated posts.json."""
    try:
        import paramiko
    except ImportError:
        return "paramiko not installed"

    import io

    host = settings.vps_ssh_host
    port = settings.vps_ssh_port
    user = settings.vps_ssh_user
    password = settings.vps_ssh_password
    key_data = settings.vps_ssh_key
    remote_dir = settings.vps_blog_path

    if not host or (not password and not key_data):
        return "VPS SSH not configured"

    pkey = None
    if key_data:
        try:
            import paramiko as _p
            pkey = _p.RSAKey.from_private_key(io.StringIO(key_data))
        except Exception:
            try:
                pkey = _p.Ed25519Key.from_private_key(io.StringIO(key_data))
            except Exception:
                pass

    if not pkey and not password:
        return "No valid SSH credentials"

    actions = []
    try:
        transport = paramiko.Transport((host, port))
        if pkey:
            transport.connect(username=user, pkey=pkey)
        else:
            transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        for fname in [f"post-{post_id}.html", f"thumb-{post_id}.jpg"]:
            remote_path = f"{remote_dir}/{fname}"
            try:
                sftp.remove(remote_path)
                actions.append(f"deleted {fname}")
            except FileNotFoundError:
                actions.append(f"{fname} not found on VPS")
            except Exception as e:
                actions.append(f"error deleting {fname}: {e}")

        if updated_posts_json and updated_posts_json.is_file():
            remote_json = f"{remote_dir}/posts.json"
            sftp.put(str(updated_posts_json), remote_json)
            actions.append("updated posts.json")

        sftp.close()
        transport.close()
    except Exception as e:
        actions.append(f"SFTP error: {e}")

    return "; ".join(actions) if actions else "no actions"


async def _mark_post_deleted(post_id: int) -> None:
    """Mark all publications for this post as FAILED with deletion note."""
    async with async_session() as session:
        result = await session.execute(
            select(Publication).where(Publication.post_id == post_id)
        )
        pubs = result.scalars().all()
        for pub in pubs:
            pub.status = PostStatus.FAILED
            pub.error_message = f"EMERGENCY DELETE at {datetime.now(timezone.utc).isoformat()}"
        await session.commit()


async def _send_report_email(report: dict) -> None:
    """Send emergency deletion report via email."""
    if not settings.resend_api_key or not settings.report_email_to:
        logger.warning("Resend not configured — cannot send emergency report email")
        return

    html = _build_report_html(report)
    subject = f"🚨 ЕКСТРЕНЕ ВИДАЛЕННЯ — {report.get('summary', 'Звіт')}"

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
                    "subject": subject,
                    "html": html,
                },
            )
            if resp.status_code in (200, 201):
                logger.info("Emergency report email sent: %s", resp.json().get("id"))
            else:
                logger.error("Failed to send emergency email: %s %s", resp.status_code, resp.text)
    except Exception:
        logger.exception("Emergency report email failed")


def _build_report_html(report: dict) -> str:
    posts_found = report.get("posts_found", 0)
    timestamp = report.get("timestamp", "")
    search_text = report.get("search_text", "")
    summary = report.get("summary", "")

    rows_html = ""
    for post_report in report.get("results", []):
        post_id = post_report.get("post_id", "?")
        title = post_report.get("title", "—")
        content_preview = post_report.get("content_preview", "")

        for p in post_report.get("platforms", []):
            color = "#2ecc71" if p["deleted"] else ("#e74c3c" if p["platform_post_id"] else "#95a5a6")
            status_icon = "✅" if p["deleted"] else ("❌" if p["platform_post_id"] else "⚪")
            rows_html += f"""
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">{post_id}</td>
                <td style="padding:8px;border:1px solid #ddd;">{p['platform_label']}</td>
                <td style="padding:8px;border:1px solid #ddd;font-family:monospace;font-size:11px;">{p.get('platform_post_id') or '—'}</td>
                <td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold;">{status_icon} {'Видалено' if p['deleted'] else 'Не видалено'}</td>
                <td style="padding:8px;border:1px solid #ddd;font-size:12px;">{p['detail']}</td>
            </tr>"""

        blog = post_report.get("blog", {})
        blog_color = "#2ecc71" if blog.get("deleted") else "#95a5a6"
        blog_icon = "✅" if blog.get("deleted") else "⚪"
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;">{post_id}</td>
            <td style="padding:8px;border:1px solid #ddd;">📝 Блог (сайт)</td>
            <td style="padding:8px;border:1px solid #ddd;">post-{post_id}.html</td>
            <td style="padding:8px;border:1px solid #ddd;color:{blog_color};font-weight:bold;">{blog_icon} {'Видалено' if blog.get('deleted') else 'Не знайдено'}</td>
            <td style="padding:8px;border:1px solid #ddd;font-size:12px;">{blog.get('detail', '')}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f8f9fa;padding:20px;">
<div style="max-width:800px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
    <div style="background:#e74c3c;color:white;padding:20px 30px;">
        <h1 style="margin:0;font-size:24px;">🚨 ЕКСТРЕНЕ ВИДАЛЕННЯ ПОСТА</h1>
        <p style="margin:8px 0 0;opacity:0.9;">{timestamp}</p>
    </div>
    <div style="padding:20px 30px;">
        <div style="background:#ffeaa7;border-left:4px solid #fdcb6e;padding:12px 16px;border-radius:4px;margin-bottom:20px;">
            <strong>Пошуковий запит:</strong> {search_text}
        </div>
        <div style="background:#dfe6e9;padding:12px 16px;border-radius:4px;margin-bottom:20px;">
            <strong>Результат:</strong> {summary}
        </div>
        <h2 style="color:#2d3436;margin-top:24px;">Деталі по платформах</h2>
        <table style="width:100%;border-collapse:collapse;margin-top:12px;">
            <thead>
                <tr style="background:#74b9ff;">
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Post ID</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Платформа</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Platform Post ID</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Статус</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Деталі</th>
                </tr>
            </thead>
            <tbody>{rows_html if rows_html else '<tr><td colspan="5" style="padding:16px;text-align:center;color:#636e72;">Пости не знайдено</td></tr>'}</tbody>
        </table>
        {"".join(f'<div style="margin-top:16px;background:#fafafa;padding:12px;border-radius:4px;border:1px solid #eee;"><strong>Post #{r["post_id"]}:</strong> {r["title"]}<br><span style="color:#636e72;font-size:13px;">{r["content_preview"]}...</span></div>' for r in report.get("results", []))}
    </div>
    <div style="background:#dfe6e9;padding:12px 30px;text-align:center;color:#636e72;font-size:12px;">
        I'M IN Social Automation — Emergency Delete System
    </div>
</div>
</body></html>"""
