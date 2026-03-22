from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform
from config.settings import settings
from db.database import get_session
from db.models import Post, Publication, PostStatus, Message, MessageDirection, RSSSource
from api.auth import require_admin, rate_limit_chat


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class PostOut(BaseModel):
    id: int
    title: Optional[str]
    content_raw: str
    source: str
    image_path: Optional[str]
    scheduled_at: Optional[datetime]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class PublicationOut(BaseModel):
    id: int
    post_id: int
    platform: str
    status: str
    platform_post_id: Optional[str]
    content_adapted: Optional[str]
    error_message: Optional[str]
    retry_count: int
    published_at: Optional[datetime]

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    platform: str
    sender_name: Optional[str]
    direction: str
    text: Optional[str]
    category: Optional[str]
    replied: bool
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class CreatePostRequest(BaseModel):
    title: Optional[str] = None
    content: str
    platforms: list[str] = ["telegram", "facebook", "twitter", "instagram", "tiktok"]
    scheduled_at: Optional[datetime] = None


class ChatRequest(BaseModel):
    message: str
    sender_name: str = "visitor"


class ChatResponse(BaseModel):
    reply: str


class AddRSSSourceRequest(BaseModel):
    name: str
    url: str


class BlogPostOut(BaseModel):
    id: int
    title: Optional[str]
    content_raw: str
    source: str
    source_url: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    place_name: Optional[str]
    image_url: Optional[str]
    published_at: Optional[datetime]
    created_at: Optional[datetime]
    translations: Optional[dict] = None

    model_config = {"from_attributes": True}


class StatsOut(BaseModel):
    total_posts: int
    published: int
    failed: int
    queued: int
    total_messages_in: int
    total_messages_out: int
    messages_unanswered: int


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC router — no auth required
# ══════════════════════════════════════════════════════════════════════════════

public_router = APIRouter(prefix="/api", tags=["public"])


@public_router.get("/debug/comment-check")
async def public_comment_check():
    """Temporary public diagnostic for comment system."""
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


@public_router.get("/debug/test-geo")
async def public_test_geo():
    """Test geo extraction on known locations."""
    from content.generator import extract_location_coordinates
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


@public_router.get("/debug/fb-poll-test")
async def public_fb_poll_test():
    """Temporary: directly poll Facebook comments and show what the API returns + DB status."""
    import httpx
    from db.database import async_session as _async_session
    from db.models import Message
    from sqlalchemy import select
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


@public_router.get("/debug/fb-reply-check")
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


@public_router.get("/debug/test-ig-reply")
async def public_test_ig_reply():
    """Temporary: try replying to the newest unreplied Instagram comment and return raw API response."""
    import httpx
    from db.database import async_session as _async_session
    from db.models import Message, MessageDirection
    from sqlalchemy import select, desc
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


@public_router.get("/debug/messages-status")
async def public_messages_status():
    """Temporary: check messages in DB and their reply status."""
    from db.database import async_session
    from db.models import Message, MessageDirection
    from sqlalchemy import select, desc

    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .order_by(desc(Message.created_at))
            .limit(20)
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


@public_router.get("/blog/posts", response_model=list[BlogPostOut])
async def blog_posts(
    limit: int = Query(10, le=50),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Public endpoint: returns only posts published on at least one platform."""
    published_ids = (
        select(Publication.post_id)
        .where(Publication.status == PostStatus.PUBLISHED)
        .group_by(Publication.post_id)
        .subquery()
    )

    pub_date = (
        select(
            Publication.post_id,
            func.max(Publication.published_at).label("published_at"),
        )
        .where(Publication.status == PostStatus.PUBLISHED)
        .group_by(Publication.post_id)
        .subquery()
    )

    result = await session.execute(
        select(Post, pub_date.c.published_at)
        .join(published_ids, Post.id == published_ids.c.post_id)
        .outerjoin(pub_date, Post.id == pub_date.c.post_id)
        .order_by(desc(pub_date.c.published_at))
        .offset(offset)
        .limit(limit)
    )

    base_url = settings.webhook_base_url.rstrip("/")
    items: list[dict] = []
    for post, published_at in result.all():
        image_url = None
        if post.image_path:
            fname = Path(post.image_path).name
            image_url = f"{base_url}/api/media/{fname}"
        import json as _json
        tr = {}
        if post.translations:
            try:
                tr = _json.loads(post.translations)
            except Exception:
                pass
        items.append(
            BlogPostOut(
                id=post.id,
                title=post.title,
                content_raw=post.content_raw,
                source=post.source,
                source_url=post.source_url,
                latitude=post.latitude,
                longitude=post.longitude,
                place_name=post.place_name,
                image_url=image_url,
                published_at=published_at,
                created_at=post.created_at,
                translations=tr or None,
            )
        )
    return items


@public_router.get("/media/{filename}")
async def serve_media(filename: str):
    """Serve images from media_cache so the website can display them."""
    safe_name = Path(filename).name
    file_path = Path(settings.media_cache_dir) / safe_name
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, headers={"Cache-Control": "public, max-age=86400"})


@public_router.get("/blog/page/{post_id}")
async def serve_blog_page(post_id: int):
    """Serve a generated static HTML blog page (fallback when VPS is down)."""
    blog_dir = Path(settings.data_dir) / "blog"
    page = blog_dir / f"post-{post_id}.html"
    if not page.is_file():
        raise HTTPException(404, "Blog page not found")
    return FileResponse(page, media_type="text/html", headers={"Cache-Control": "public, max-age=3600"})


@public_router.get("/blog/index.json")
async def serve_blog_index():
    """Serve the posts.json index (fallback for blog listing)."""
    blog_dir = Path(settings.data_dir) / "blog"
    idx = blog_dir / "posts.json"
    if not idx.is_file():
        raise HTTPException(404, "Blog index not found")
    return FileResponse(idx, media_type="application/json", headers={"Cache-Control": "public, max-age=300"})


@public_router.post("/chat", response_model=ChatResponse, dependencies=[Depends(rate_limit_chat)])
async def web_chat(body: ChatRequest):
    """Public chat endpoint for the website widget (rate-limited)."""
    from content.generator import generate_auto_reply

    try:
        reply_text, category = await generate_auto_reply(
            incoming_message=body.message,
            platform=Platform.TELEGRAM,
            sender_name=body.sender_name,
        )
        if category == "spam":
            return ChatResponse(reply="Дякую за повідомлення!")
        return ChatResponse(reply=reply_text)
    except Exception:
        return ChatResponse(
            reply="Дякую за повідомлення! Наша команда скоро відповість. 🙏"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN router — requires X-API-Key header
# ══════════════════════════════════════════════════════════════════════════════

router = APIRouter(
    prefix="/api",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ── Posts ─────────────────────────────────────────────────────────────────────

@router.get("/posts", response_model=list[PostOut])
async def list_posts(
    limit: int = Query(20, le=100),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Post).order_by(desc(Post.created_at)).offset(offset).limit(limit)
    )
    return result.scalars().all()


@router.post("/posts", response_model=PostOut, status_code=201)
async def create_post(body: CreatePostRequest, session: AsyncSession = Depends(get_session)):
    post = Post(
        title=body.title,
        content_raw=body.content,
        source="manual",
        scheduled_at=body.scheduled_at,
    )
    session.add(post)
    await session.flush()

    for p in body.platforms:
        try:
            Platform(p)
        except ValueError:
            raise HTTPException(400, f"Unknown platform: {p}")
        pub = Publication(post_id=post.id, platform=p, status=PostStatus.QUEUED)
        session.add(pub)

    await session.commit()
    await session.refresh(post)
    return post


@router.get("/posts/{post_id}/publications", response_model=list[PublicationOut])
async def get_publications(post_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Publication).where(Publication.post_id == post_id)
    )
    return result.scalars().all()


# ── Publications queue ────────────────────────────────────────────────────────

@router.get("/queue", response_model=list[PublicationOut])
async def get_queue(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Publication)
        .where(Publication.status.in_([PostStatus.QUEUED, PostStatus.PUBLISHING]))
        .order_by(Publication.created_at)
    )
    return result.scalars().all()


# ── Messages ──────────────────────────────────────────────────────────────────

@router.get("/messages", response_model=list[MessageOut])
async def list_messages(
    platform: Optional[str] = None,
    unanswered: bool = False,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    q = select(Message).order_by(desc(Message.created_at)).limit(limit)
    if platform:
        q = q.where(Message.platform == platform)
    if unanswered:
        q = q.where(
            Message.direction == MessageDirection.INCOMING,
            Message.replied == False,
        )
    result = await session.execute(q)
    return result.scalars().all()


# ── RSS Sources ───────────────────────────────────────────────────────────────

@router.get("/rss", response_model=list[dict])
async def list_rss_sources(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(RSSSource))
    sources = result.scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "enabled": s.enabled,
            "last_fetched_at": s.last_fetched_at,
        }
        for s in sources
    ]


@router.post("/rss", status_code=201)
async def add_rss_source(body: AddRSSSourceRequest, session: AsyncSession = Depends(get_session)):
    source = RSSSource(name=body.name, url=body.url)
    session.add(source)
    await session.commit()
    return {"id": source.id, "name": source.name, "url": source.url}


@router.delete("/rss/{source_id}")
async def delete_rss_source(source_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(RSSSource).where(RSSSource.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(404, "RSS source not found")
    await session.delete(source)
    await session.commit()
    return {"deleted": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsOut)
async def get_stats(session: AsyncSession = Depends(get_session)):
    total_posts = (await session.execute(select(func.count(Post.id)))).scalar() or 0

    published = (
        await session.execute(
            select(func.count(Publication.id)).where(Publication.status == PostStatus.PUBLISHED)
        )
    ).scalar() or 0

    failed = (
        await session.execute(
            select(func.count(Publication.id)).where(Publication.status == PostStatus.FAILED)
        )
    ).scalar() or 0

    queued = (
        await session.execute(
            select(func.count(Publication.id)).where(Publication.status == PostStatus.QUEUED)
        )
    ).scalar() or 0

    msgs_in = (
        await session.execute(
            select(func.count(Message.id)).where(Message.direction == MessageDirection.INCOMING)
        )
    ).scalar() or 0

    msgs_out = (
        await session.execute(
            select(func.count(Message.id)).where(Message.direction == MessageDirection.OUTGOING)
        )
    ).scalar() or 0

    unanswered = (
        await session.execute(
            select(func.count(Message.id)).where(
                Message.direction == MessageDirection.INCOMING,
                Message.replied == False,
            )
        )
    ).scalar() or 0

    return StatsOut(
        total_posts=total_posts,
        published=published,
        failed=failed,
        queued=queued,
        total_messages_in=msgs_in,
        total_messages_out=msgs_out,
        messages_unanswered=unanswered,
    )
