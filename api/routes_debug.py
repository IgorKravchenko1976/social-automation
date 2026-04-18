"""Debug and diagnostic API endpoints (public, no auth required)."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select, desc

from config.settings import settings
from db.database import get_session

debug_router = APIRouter(prefix="/api", tags=["debug"])

_telethon_state: dict = {}


@debug_router.get("/debug/poi-post-mapping")
async def poi_post_mapping():
    """Return mapping of post_id -> poi_point_id for all POI posts (for backfill)."""
    from db.database import async_session
    from db.models import Post
    async with async_session() as session:
        rows = (await session.execute(
            select(Post.id, Post.poi_point_id, Post.backend_event_id).where(
                Post.source == "poi",
                Post.poi_point_id.isnot(None),
            ).order_by(Post.id)
        )).all()
    return [
        {"post_id": r[0], "poi_point_id": r[1], "backend_event_id": r[2]}
        for r in rows
    ]


@debug_router.get("/debug/comment-check")
async def public_comment_check():
    """Diagnostic: Facebook & Instagram comment system check."""
    import httpx
    from stats.token_renewer import get_active_token
    from config.platforms import FACEBOOK_GRAPH_API

    report = {"facebook": {}, "instagram": {}}
    fb_token = await get_active_token("facebook") or settings.facebook_page_access_token
    page_id = settings.facebook_page_id

    if not fb_token:
        report["facebook"] = {"error": "No token"}
    else:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{FACEBOOK_GRAPH_API}/debug_token",
                    params={"input_token": fb_token, "access_token": fb_token},
                )
                debug_data = r.json().get("data", {})
                scopes = debug_data.get("scopes", [])
                report["facebook"]["valid"] = debug_data.get("is_valid", False)
                report["facebook"]["scopes"] = scopes
                needed = ["pages_read_engagement", "pages_manage_engagement"]
                report["facebook"]["missing"] = [s for s in needed if s not in scopes]

                r2 = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{page_id}/feed",
                    params={
                        "access_token": fb_token,
                        "fields": "id,comments.limit(2){id,from,message}",
                        "limit": 3,
                    },
                )
                feed = r2.json()
                if "error" in feed:
                    report["facebook"]["feed_error"] = feed["error"].get("message")
                else:
                    posts = feed.get("data", [])
                    comments = []
                    for p in posts:
                        for c in p.get("comments", {}).get("data", []):
                            comments.append({"from": c.get("from", {}).get("name"), "text": c.get("message", "")[:60]})
                    report["facebook"]["posts_checked"] = len(posts)
                    report["facebook"]["comments_found"] = len(comments)
                    report["facebook"]["sample_comments"] = comments[:5]

                ig_r = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{page_id}",
                    params={"access_token": fb_token, "fields": "instagram_business_account"},
                )
                ig_id = ig_r.json().get("instagram_business_account", {}).get("id")
                if ig_id:
                    report["instagram"]["ig_id"] = ig_id
                    ig_media = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{ig_id}/media",
                        params={
                            "access_token": fb_token,
                            "fields": "id,comments.limit(2){id,from,text}",
                            "limit": 3,
                        },
                    )
                    ig_data = ig_media.json()
                    if "error" in ig_data:
                        report["instagram"]["error"] = ig_data["error"].get("message")
                    else:
                        items = ig_data.get("data", [])
                        ig_comments = []
                        for m in items:
                            for c in m.get("comments", {}).get("data", []):
                                ig_comments.append({"from": c.get("from", {}).get("username", "?"), "text": c.get("text", "")[:60]})
                        report["instagram"]["media_checked"] = len(items)
                        report["instagram"]["comments_found"] = len(ig_comments)
                        report["instagram"]["sample_comments"] = ig_comments[:5]
                else:
                    report["instagram"]["error"] = "No IG Business Account linked"
        except Exception as e:
            report["facebook"]["error"] = str(e)

    return report


@debug_router.get("/debug/test-geo")
async def public_test_geo():
    """Test geo extraction on known locations."""
    from content.geo import extract_location_coordinates

    tests = [
        "Драгобрат, Карпати — найвищий гірськолижний курорт України",
        "Львів — місто кави та шоколаду",
        "Італія послаблює правила в'їзду",
    ]
    results = []
    for topic in tests:
        try:
            geo = await extract_location_coordinates(topic)
            results.append({"topic": topic[:60], "geo": geo, "error": None})
        except Exception as e:
            results.append({"topic": topic[:60], "geo": None, "error": str(e)})
    return results


@debug_router.get("/debug/fix-geo")
async def public_fix_geo():
    """Re-enrich all posts that have no geo data."""
    from db.database import async_session as _async_session
    from db.models import Post
    from content.geo import extract_location_coordinates

    fixed = []
    async with _async_session() as session:
        result = await session.execute(
            select(Post).where(Post.latitude.is_(None), Post.title.isnot(None))
        )
        posts = result.scalars().all()

        for post in posts:
            try:
                geo = await extract_location_coordinates(post.title or post.content_raw[:300])
                if geo and geo.get("lat") and geo.get("lon"):
                    post.latitude = geo["lat"]
                    post.longitude = geo["lon"]
                    post.place_name = (geo.get("name") or "")[:500]
                    fixed.append({"id": post.id, "title": (post.title or "")[:40], "place": post.place_name})
            except Exception as e:
                fixed.append({"id": post.id, "title": (post.title or "")[:40], "error": str(e)})

        await session.commit()

    return {"total_without_geo": len(posts), "fixed": len([f for f in fixed if "place" in f]), "details": fixed}


@debug_router.get("/debug/blog-sync")
async def public_blog_sync():
    """Trigger blog regeneration + SFTP sync to VPS."""
    from scheduler.blog_sync import sync_blog_to_vps
    try:
        count = await sync_blog_to_vps()
        return {"status": "ok", "synced_files": count}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@debug_router.get("/debug/blog-delete-vps/{post_id}")
async def blog_delete_from_vps(post_id: int):
    """Delete a specific blog post from VPS and local Railway storage."""
    from scheduler.emergency_delete import _sftp_delete_post
    from pathlib import Path
    import json as _json

    blog_dir = Path(settings.data_dir) / "blog"
    local_actions = []

    for fname in [f"post-{post_id}.html", f"thumb-{post_id}.jpg"]:
        fp = blog_dir / fname
        if fp.is_file():
            fp.unlink()
            local_actions.append(f"local: deleted {fname}")
        else:
            local_actions.append(f"local: {fname} not found")

    posts_json = blog_dir / "posts.json"
    if posts_json.is_file():
        try:
            posts = _json.loads(posts_json.read_text(encoding="utf-8"))
            before = len(posts)
            posts = [p for p in posts if p.get("id") != post_id]
            if len(posts) < before:
                posts_json.write_text(_json.dumps(posts, ensure_ascii=False, default=str), encoding="utf-8")
                local_actions.append("local: posts.json updated")
        except Exception as e:
            local_actions.append(f"local: posts.json error: {e}")

    try:
        vps_detail = _sftp_delete_post(post_id, posts_json if posts_json.is_file() else None)
    except Exception as e:
        vps_detail = f"error: {e}"

    return {"status": "ok", "post_id": post_id, "local": local_actions, "vps": vps_detail}


@debug_router.get("/debug/vps-files")
async def vps_list_files():
    """List files on VPS blog directory via SFTP for diagnostics."""
    import io
    try:
        import paramiko
    except ImportError:
        return {"error": "paramiko not installed"}

    host = settings.vps_ssh_host
    port = settings.vps_ssh_port
    user = settings.vps_ssh_user
    password = settings.vps_ssh_password
    key_data = settings.vps_ssh_key
    remote_dir = settings.vps_blog_path

    if not host:
        return {"error": "VPS_SSH_HOST not configured"}

    pkey = None
    if key_data:
        try:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(key_data))
        except Exception:
            try:
                pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(key_data))
            except Exception:
                pass

    try:
        transport = paramiko.Transport((host, port))
        if pkey:
            transport.connect(username=user, pkey=pkey)
        else:
            transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        files = sorted(sftp.listdir(remote_dir))
        post_files = [f for f in files if f.startswith("post-") and f.endswith(".html")]
        thumb_files = [f for f in files if f.startswith("thumb-")]
        other_files = [f for f in files if f not in post_files and f not in thumb_files]
        sftp.close()
        transport.close()
        return {
            "remote_dir": remote_dir,
            "total": len(files),
            "post_files": post_files,
            "thumb_files": thumb_files,
            "other_files": other_files,

        }
    except Exception as e:
        return {"error": str(e), "remote_dir": remote_dir}


@debug_router.get("/debug/regenerate-content")
async def public_regenerate_content(limit: int = 3, offset: int = 0):
    """Re-generate full text for posts with short content_raw."""
    from db.database import async_session as _async_session
    from db.models import Post
    from content.generator import generate_post_text, translate_post
    from config.platforms import Platform
    from sqlalchemy import func
    import json as _json

    updated = []
    skipped = 0
    async with _async_session() as session:
        total_q = await session.execute(
            select(func.count(Post.id)).where(Post.title.isnot(None))
        )
        total = total_q.scalar() or 0

        result = await session.execute(
            select(Post).where(Post.title.isnot(None))
            .order_by(Post.id.desc()).offset(offset).limit(limit + 10)
        )
        posts = result.scalars().all()

        _RAW_POI_MARKERS = ("=== ДАНІ ПРО КОНКРЕТНУ ТОЧКУ", "=== КІНЕЦЬ ДАНИХ")

        processed = 0
        for post in posts:
            if processed >= limit:
                break
            raw = post.content_raw or ""
            is_raw_poi = any(m in raw for m in _RAW_POI_MARKERS)

            if len(raw) > 500 and not is_raw_poi:
                skipped += 1
                continue

            processed += 1
            try:
                if is_raw_poi:
                    full_text = await generate_post_text(
                        topic="", platform=Platform.TELEGRAM,
                        source_text=raw, content_type="poi_spotlight",
                    )
                elif post.source == "rss":
                    full_text = await generate_post_text(
                        topic="", platform=Platform.TELEGRAM,
                        source_text=raw, content_type="tourism_news",
                    )
                else:
                    ct = "feature" if "i'm in" in raw.lower() or "карт" in raw.lower() else "leisure_travel"
                    full_text = await generate_post_text(
                        topic=raw, platform=Platform.TELEGRAM, content_type=ct,
                    )

                if full_text and (is_raw_poi or len(full_text) > len(raw)):
                    post.content_raw = full_text
                    tr = await translate_post(post.title or "", full_text)
                    if tr:
                        post.translations = _json.dumps(tr, ensure_ascii=False)
                    updated.append({"id": post.id, "title": (post.title or "")[:60], "len": len(full_text)})
                else:
                    updated.append({"id": post.id, "title": (post.title or "")[:60], "note": "no improvement"})
            except Exception as e:
                updated.append({"id": post.id, "title": (post.title or "")[:60], "error": str(e)})

        await session.commit()

    next_offset = offset + limit + skipped
    return {
        "total_posts": total,
        "processed": len(updated),
        "skipped_already_long": skipped,
        "next_call": f"/api/debug/regenerate-content?limit={limit}&offset={next_offset}" if next_offset < total else None,
        "details": updated,
    }


@debug_router.get("/debug/fb-poll-test")
async def public_fb_poll_test():
    """Directly poll Facebook comments and show what the API returns + DB status."""
    import httpx
    from db.database import async_session as _async_session
    from db.models import Message
    from stats.token_renewer import get_active_token
    from config.platforms import FACEBOOK_GRAPH_API

    fb_token = await get_active_token("facebook") or settings.facebook_page_access_token
    page_id = settings.facebook_page_id

    report = {"raw_comments": [], "db_status": [], "errors": []}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{page_id}/feed",
                params={
                    "access_token": fb_token,
                    "fields": "id,message,comments{id,from,message,created_time}",
                    "limit": 5,
                },
            )
            data = resp.json()
            if "error" in data:
                report["errors"].append(data["error"])
                return report

            for post in data.get("data", []):
                post_id = post.get("id")
                post_text = (post.get("message") or "")[:60]
                for comment in post.get("comments", {}).get("data", []):
                    cid = comment.get("id", "")
                    from_data = comment.get("from")
                    report["raw_comments"].append({
                        "post_id": post_id,
                        "post_text": post_text,
                        "comment_id": cid,
                        "from_raw": from_data,
                        "from_id": from_data.get("id", "") if isinstance(from_data, dict) else None,
                        "from_name": from_data.get("name", "") if isinstance(from_data, dict) else None,
                        "message": comment.get("message", ""),
                        "created_time": comment.get("created_time", ""),
                    })
    except Exception as e:
        report["errors"].append(str(e))

    async with _async_session() as session:
        for c in report["raw_comments"]:
            result = await session.execute(
                select(Message).where(
                    Message.platform == "facebook",
                    Message.platform_message_id == c["comment_id"],
                ).limit(1)
            )
            msg = result.scalar_one_or_none()
            c["in_db"] = msg is not None
            c["db_replied"] = msg.replied if msg else None

    return report


@debug_router.get("/debug/fb-reply-check")
async def public_fb_reply_check():
    """Check if bot replies actually exist on Facebook comments."""
    import httpx
    from stats.token_renewer import get_active_token
    from config.platforms import FACEBOOK_GRAPH_API

    fb_token = await get_active_token("facebook") or settings.facebook_page_access_token
    page_id = settings.facebook_page_id

    results = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{FACEBOOK_GRAPH_API}/{page_id}/feed",
                params={
                    "access_token": fb_token,
                    "fields": "id,message,comments{id,from,message,comments{id,from,message}}",
                    "limit": 5,
                },
            )
            data = resp.json()
            if "error" in data:
                return {"error": data["error"]}

            for post in data.get("data", []):
                post_info = {
                    "post_id": post.get("id"),
                    "post_text": (post.get("message") or "")[:60],
                    "comments": [],
                }
                for comment in post.get("comments", {}).get("data", []):
                    c_info = {
                        "id": comment.get("id"),
                        "from": comment.get("from"),
                        "message": comment.get("message", ""),
                        "replies": [],
                    }
                    for reply in comment.get("comments", {}).get("data", []):
                        c_info["replies"].append({
                            "id": reply.get("id"),
                            "from": reply.get("from"),
                            "message": reply.get("message", ""),
                        })
                    post_info["comments"].append(c_info)
                results.append(post_info)

            test_comment_id = None
            for p in results:
                for c in p["comments"]:
                    if c["message"] and not c["replies"]:
                        test_comment_id = c["id"]
                        break
                if test_comment_id:
                    break

            test_result = None
            if test_comment_id:
                test_resp = await client.post(
                    f"{FACEBOOK_GRAPH_API}/{test_comment_id}/comments",
                    params={"access_token": fb_token},
                    json={"message": "Дякуємо за ваш коментар! Слідкуйте за оновленнями 🌍"},
                )
                test_result = {
                    "comment_id": test_comment_id,
                    "response": test_resp.json(),
                    "status_code": test_resp.status_code,
                }

    except Exception as e:
        return {"error": str(e)}

    return {"posts": results, "test_reply": test_result}


@debug_router.get("/debug/test-ig-reply")
async def public_test_ig_reply():
    """Try replying to the newest unreplied Instagram comment."""
    import httpx
    from db.database import async_session as _async_session
    from db.models import Message, MessageDirection
    from stats.token_renewer import get_active_token
    from config.platforms import FACEBOOK_GRAPH_API

    async with _async_session() as session:
        result = await session.execute(
            select(Message).where(
                Message.platform == "instagram",
                Message.direction == MessageDirection.INCOMING,
                Message.replied == False,
            ).order_by(desc(Message.created_at)).limit(1)
        )
        msg = result.scalar_one_or_none()

    if not msg:
        return {"error": "No unreplied Instagram comments in DB"}

    fb_token = await get_active_token("facebook") or settings.facebook_page_access_token
    if not fb_token:
        return {"error": "No Facebook token"}

    page_id = settings.facebook_page_id

    report = {
        "msg_id": msg.id,
        "platform_message_id": msg.platform_message_id,
        "sender": msg.sender_name,
        "text": msg.text,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            ig_r = await client.get(
                f"{FACEBOOK_GRAPH_API}/{page_id}",
                params={"access_token": fb_token, "fields": "instagram_business_account"},
            )
            ig_id = ig_r.json().get("instagram_business_account", {}).get("id")
            report["ig_business_id"] = ig_id

            comment_check = await client.get(
                f"{FACEBOOK_GRAPH_API}/{msg.platform_message_id}",
                params={"access_token": fb_token, "fields": "id,text,from,timestamp"},
            )
            report["comment_lookup"] = comment_check.json()

            reply_text = "Дякуємо за коментар! Слідкуйте за оновленнями 🌍"
            resp = await client.post(
                f"{FACEBOOK_GRAPH_API}/{msg.platform_message_id}/replies",
                params={"access_token": fb_token},
                data={"message": reply_text},
            )
            report["reply_response"] = resp.json()
            report["reply_status_code"] = resp.status_code
    except Exception as e:
        report["exception"] = str(e)

    return report


@debug_router.get("/debug/messages-status")
async def public_messages_status():
    """Check messages in DB and their reply status."""
    from db.database import async_session
    from db.models import Message

    async with async_session() as session:
        result = await session.execute(
            select(Message).order_by(desc(Message.created_at)).limit(20)
        )
        msgs = result.scalars().all()

    return [
        {
            "id": m.id,
            "platform": m.platform,
            "direction": m.direction.value if m.direction else None,
            "sender": m.sender_name,
            "text": (m.text or "")[:80],
            "replied": m.replied,
            "category": m.category,
            "thread_id": m.thread_id,
            "created_at": str(m.created_at),
        }
        for m in msgs
    ]


# ── Telethon session setup ───────────────────────────────────────────────────

@debug_router.get("/telethon/setup")
async def telethon_setup_page():
    """Interactive HTML page for Telethon session generation."""
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Telethon Setup</title>
    <style>body{font-family:sans-serif;max-width:500px;margin:40px auto;padding:20px}
    input,button{font-size:16px;padding:8px 12px;margin:5px 0}input{width:100%;box-sizing:border-box}
    button{background:#2AABEE;color:#fff;border:none;border-radius:4px;cursor:pointer;width:100%}
    button:hover{background:#1a9ada}.msg{padding:10px;margin:10px 0;border-radius:4px}
    .ok{background:#d4edda;color:#155724}.err{background:#f8d7da;color:#721c24}
    .session{word-break:break-all;background:#f0f0f0;padding:10px;font-family:monospace;font-size:12px}
    h1{color:#2AABEE}#step2{display:none}</style></head><body>
    <h1>Telethon Session Setup</h1>
    <div id="step1">
    <p>Крок 1: Введіть номер телефону у форматі +380XXXXXXXXX</p>
    <input id="phone" placeholder="+380504401477" value="+380504401477">
    <button onclick="sendCode()">Надіслати код</button>
    <div id="msg1"></div></div>
    <div id="step2">
    <p>Крок 2: Введіть 5-значний код з Telegram</p>
    <input id="code" placeholder="12345" maxlength="10">
    <input id="password" placeholder="2FA пароль (якщо є)" style="display:none">
    <button onclick="signIn()">Увійти</button>
    <div id="msg2"></div></div>
    <script>
    async function sendCode(){
      const phone=document.getElementById('phone').value.trim();
      document.getElementById('msg1').innerHTML='<div class="msg">Надсилаю код...</div>';
      try{
        const r=await fetch('/api/telethon/send-code?phone='+encodeURIComponent(phone));
        const d=await r.json();
        if(d.ok){
          document.getElementById('msg1').innerHTML='<div class="msg ok">Код надіслано! Перевірте Telegram.</div>';
          document.getElementById('step2').style.display='block';
        }else{
          document.getElementById('msg1').innerHTML='<div class="msg err">Помилка: '+d.error+'</div>';
        }
      }catch(e){document.getElementById('msg1').innerHTML='<div class="msg err">'+e+'</div>';}
    }
    async function signIn(){
      const phone=document.getElementById('phone').value.trim();
      const code=document.getElementById('code').value.trim();
      const pw=document.getElementById('password').value.trim();
      document.getElementById('msg2').innerHTML='<div class="msg">Перевіряю код...</div>';
      try{
        let url='/api/telethon/sign-in?phone='+encodeURIComponent(phone)+'&code='+encodeURIComponent(code);
        if(pw)url+='&password='+encodeURIComponent(pw);
        const r=await fetch(url);
        const d=await r.json();
        if(d.ok){
          document.getElementById('msg2').innerHTML='<div class="msg ok">Сесію створено і збережено! Перегляди Telegram тепер будуть працювати.<br><br>Session:<div class="session">'+d.session+'</div></div>';
        }else if(d.need_2fa){
          document.getElementById('password').style.display='block';
          document.getElementById('msg2').innerHTML='<div class="msg err">Потрібен 2FA пароль. Введіть його вище.</div>';
        }else{
          document.getElementById('msg2').innerHTML='<div class="msg err">Помилка: '+d.error+'</div>';
        }
      }catch(e){document.getElementById('msg2').innerHTML='<div class="msg err">'+e+'</div>';}
    }
    </script></body></html>"""
    return HTMLResponse(html)


@debug_router.get("/telethon/send-code")
async def telethon_send_code(phone: str):
    """Send Telegram verification code to the phone number."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if not api_id or not api_hash:
        return {"ok": False, "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH not configured on Railway"}

    try:
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        _telethon_state["client"] = client
        _telethon_state["phone"] = phone
        _telethon_state["phone_code_hash"] = result.phone_code_hash
        return {"ok": True, "message": "Code sent to Telegram"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@debug_router.get("/telethon/sign-in")
async def telethon_sign_in(phone: str, code: str, password: str = ""):
    """Complete Telethon sign-in with the verification code."""
    from telethon.errors import SessionPasswordNeededError

    client = _telethon_state.get("client")
    phone_code_hash = _telethon_state.get("phone_code_hash")

    if not client or not phone_code_hash:
        return {"ok": False, "error": "No pending session. Send code first."}

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            return {"ok": False, "need_2fa": True, "error": "2FA password required"}
        try:
            await client.sign_in(password=password)
        except Exception as e:
            return {"ok": False, "error": f"2FA failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    session_string = client.session.save()
    await client.disconnect()
    _telethon_state.clear()

    from db.database import async_session as _async_session
    from db.models import KVStore

    async with _async_session() as session:
        result = await session.execute(
            select(KVStore).where(KVStore.key == "telegram_session")
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = session_string
        else:
            session.add(KVStore(key="telegram_session", value=session_string))
        await session.commit()

    return {"ok": True, "session": session_string, "message": "Session saved to DB!"}


# ── Platform-specific debug endpoints ────────────────────────────────────────

@debug_router.get("/debug/test-ig-subs")
async def debug_test_ig_subs():
    """Test Instagram subscriber collection."""
    import httpx
    from config.platforms import FACEBOOK_GRAPH_API
    from stats.collector import _get_instagram_token

    token = await _get_instagram_token()
    ig_user_id = settings.instagram_user_id
    page_id = settings.facebook_page_id

    report = {
        "instagram_user_id_env": ig_user_id or "(empty)",
        "token_available": bool(token),
    }

    if token and page_id:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{FACEBOOK_GRAPH_API}/{page_id}",
                params={"access_token": token, "fields": "instagram_business_account"},
            )
            discovered = r.json().get("instagram_business_account", {}).get("id")
            report["ig_discovered_from_page"] = discovered

            if ig_user_id:
                r2 = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{ig_user_id}",
                    params={"fields": "followers_count,media_count,username", "access_token": token},
                )
                report["configured_id_result"] = r2.json()

            if discovered:
                r3 = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{discovered}",
                    params={"fields": "followers_count,media_count,username", "access_token": token},
                )
                report["discovered_id_result"] = r3.json()

    return report


@debug_router.get("/debug/test-telethon")
async def debug_test_telethon():
    """Trigger Telethon view refresh and return results."""
    from config.settings import get_now_local
    from stats.collector import _refresh_telegram_views_telethon, _get_telethon_session
    from db.database import async_session as _async_session
    from db.models import Message, Publication, PostStatus
    from sqlalchemy import func as sa_func

    date_str = get_now_local().strftime("%Y-%m-%d")
    session_str = await _get_telethon_session()

    report = {
        "date": date_str,
        "session_available": bool(session_str),
        "api_id_configured": bool(settings.telegram_api_id),
        "api_hash_configured": bool(settings.telegram_api_hash),
    }

    async with _async_session() as session:
        res = await session.execute(
            select(sa_func.count(Publication.id)).where(
                Publication.platform == "telegram",
                Publication.status == PostStatus.PUBLISHED,
                sa_func.date(Publication.published_at) == date_str,
            )
        )
        report["telegram_publications_today"] = res.scalar() or 0

        res2 = await session.execute(
            select(sa_func.count(Message.id)).where(
                Message.platform == "telegram",
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        report["channel_posts_before"] = res2.scalar() or 0

    try:
        await _refresh_telegram_views_telethon(date_str)
        report["refresh_status"] = "ok"
    except Exception as e:
        report["refresh_status"] = f"error: {e}"

    async with _async_session() as session:
        res3 = await session.execute(
            select(Message).where(
                Message.platform == "telegram",
                Message.category == "channel_post",
                sa_func.date(Message.created_at) == date_str,
            )
        )
        posts = res3.scalars().all()
        report["channel_posts_after"] = len(posts)
        report["posts"] = [
            {"msg_id": p.platform_message_id, "views": p.view_count}
            for p in posts
        ]
        report["total_views"] = sum(p.view_count or 0 for p in posts)

    return report


@debug_router.get("/debug/test-views")
async def debug_test_views():
    """Test view/impression collection from all platform APIs."""
    import httpx
    from config.platforms import FACEBOOK_GRAPH_API
    from config.settings import get_now_local
    from db.database import async_session as _async_session
    from db.models import Publication, PostStatus, Message
    from sqlalchemy import func as sa_func

    results = {}
    date_str = get_now_local().strftime("%Y-%m-%d")

    async with httpx.AsyncClient(timeout=30) as client:
        tg_info = {"channel_posts_in_db": 0, "total_view_count": 0, "posts": []}
        if settings.telegram_bot_token and settings.telegram_channel_id:
            async with _async_session() as session:
                res = await session.execute(
                    select(Message).where(
                        Message.platform == "telegram",
                        Message.category == "channel_post",
                        sa_func.date(Message.created_at) == date_str,
                    )
                )
                posts = res.scalars().all()
                tg_info["channel_posts_in_db"] = len(posts)
                tg_info["total_view_count"] = sum(p.view_count or 0 for p in posts)
                for p in posts[:5]:
                    tg_info["posts"].append({
                        "msg_id": p.platform_message_id,
                        "view_count": p.view_count,
                        "text": (p.text or "")[:60],
                    })
        results["telegram"] = tg_info

        fb_info = {"post_ids": [], "insights_responses": [], "page_views_response": None}
        if settings.facebook_page_id and settings.facebook_page_access_token:
            from stats.token_renewer import get_active_token
            token = await get_active_token("facebook") or settings.facebook_page_access_token

            async with _async_session() as session:
                res = await session.execute(
                    select(Publication.platform_post_id).where(
                        Publication.platform == "facebook",
                        Publication.status == PostStatus.PUBLISHED,
                        sa_func.date(Publication.published_at) == date_str,
                        Publication.platform_post_id.isnot(None),
                    )
                )
                raw_ids = [r[0] for r in res.all()]
            page_id = settings.facebook_page_id
            post_ids = []
            for pid in raw_ids:
                if "_" not in pid and page_id:
                    post_ids.append(f"{page_id}_{pid}")
                else:
                    post_ids.append(pid)
            fb_info["post_ids"] = post_ids
            fb_info["raw_ids"] = raw_ids

            for pid in post_ids[:3]:
                try:
                    resp = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{pid}/insights",
                        params={"metric": "post_impressions", "access_token": token},
                    )
                    fb_info["insights_responses"].append({"post_id": pid, "data": resp.json()})
                except Exception as e:
                    fb_info["insights_responses"].append({"post_id": pid, "error": str(e)})

                try:
                    resp2 = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{pid}",
                        params={"fields": "reactions.summary(total_count),comments.summary(total_count)", "access_token": token},
                    )
                    fb_info["insights_responses"].append({"post_id": pid, "engagement": resp2.json()})
                except Exception as e:
                    fb_info["insights_responses"].append({"post_id": pid, "engagement_error": str(e)})

            try:
                resp3 = await client.get(
                    f"{FACEBOOK_GRAPH_API}/{settings.facebook_page_id}/insights",
                    params={"metric": "page_views_total", "period": "day", "access_token": token},
                )
                fb_info["page_views_response"] = resp3.json()
            except Exception as e:
                fb_info["page_views_response"] = {"error": str(e)}

        results["facebook"] = fb_info

        ig_info = {"media_ids": [], "insights_responses": []}
        if settings.instagram_user_id:
            from stats.collector import _get_instagram_token
            token = await _get_instagram_token()

            async with _async_session() as session:
                res = await session.execute(
                    select(Publication.platform_post_id).where(
                        Publication.platform == "instagram",
                        Publication.status == PostStatus.PUBLISHED,
                        sa_func.date(Publication.published_at) == date_str,
                        Publication.platform_post_id.isnot(None),
                    )
                )
                media_ids = [r[0] for r in res.all()]
            ig_info["media_ids"] = media_ids

            for mid in media_ids[:3]:
                try:
                    resp = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{mid}/insights",
                        params={"metric": "reach,total_interactions", "access_token": token},
                    )
                    ig_info["insights_responses"].append({"media_id": mid, "data": resp.json()})
                except Exception as e:
                    ig_info["insights_responses"].append({"media_id": mid, "error": str(e)})

                try:
                    resp2 = await client.get(
                        f"{FACEBOOK_GRAPH_API}/{mid}",
                        params={"fields": "like_count,comments_count,timestamp", "access_token": token},
                    )
                    ig_info["insights_responses"].append({"media_id": mid, "basic": resp2.json()})
                except Exception as e:
                    ig_info["insights_responses"].append({"media_id": mid, "basic_error": str(e)})

        results["instagram"] = ig_info

    return {"date": date_str, "results": results}
