"""Public blog, media, and chat API endpoints."""
from __future__ import annotations

import json as _json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform
from config.settings import settings
from db.database import get_session
from db.models import Post, Publication, PostStatus
from api.auth import rate_limit_chat
from api.schemas import BlogPostOut, ChatRequest, ChatResponse

blog_router = APIRouter(prefix="/api", tags=["blog"])

_RAW_POI_MARKERS = ("=== ДАНІ ПРО КОНКРЕТНУ ТОЧКУ", "=== КІНЕЦЬ ДАНИХ", "--- ДЖЕРЕЛО ДАНИХ")


def _is_raw_poi(text: str) -> bool:
    return any(m in text for m in _RAW_POI_MARKERS)


@blog_router.get("/blog/posts", response_model=list[BlogPostOut])
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

    rows = result.all()
    post_ids = [post.id for post, _ in rows]

    adapted_texts: dict[int, str] = {}
    if post_ids:
        pubs_result = await session.execute(
            select(Publication.post_id, Publication.platform, Publication.content_adapted)
            .where(
                Publication.post_id.in_(post_ids),
                Publication.status == PostStatus.PUBLISHED,
                Publication.content_adapted.isnot(None),
            )
        )
        for pid, platform, text in pubs_result.all():
            if not text:
                continue
            if pid not in adapted_texts or platform == Platform.TELEGRAM.value:
                adapted_texts[pid] = text

    base_url = settings.webhook_base_url.rstrip("/")
    items: list[dict] = []
    for post, published_at in rows:
        image_url = None
        if post.image_path:
            fname = Path(post.image_path).name
            image_url = f"{base_url}/api/media/{fname}"
        tr = {}
        if post.translations:
            try:
                tr = _json.loads(post.translations)
            except Exception:
                pass

        content = post.content_raw or ""
        if _is_raw_poi(content) and post.id in adapted_texts:
            content = adapted_texts[post.id]

        items.append(
            BlogPostOut(
                id=post.id,
                title=post.title,
                content_raw=content,
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


@blog_router.get("/media/{filename}")
async def serve_media(filename: str):
    """Serve images from media_cache so the website can display them."""
    safe_name = Path(filename).name
    file_path = Path(settings.media_cache_dir) / safe_name
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, headers={"Cache-Control": "public, max-age=86400"})


@blog_router.get("/blog/page/{post_id}")
async def serve_blog_page(post_id: int):
    """Serve a generated static HTML blog page (fallback when VPS is down)."""
    blog_dir = Path(settings.data_dir) / "blog"
    page = blog_dir / f"post-{post_id}.html"
    if not page.is_file():
        raise HTTPException(404, "Blog page not found")
    return FileResponse(page, media_type="text/html", headers={"Cache-Control": "public, max-age=3600"})


@blog_router.get("/blog/index.json")
async def serve_blog_index():
    """Serve the posts.json index (fallback for blog listing)."""
    blog_dir = Path(settings.data_dir) / "blog"
    idx = blog_dir / "posts.json"
    if not idx.is_file():
        raise HTTPException(404, "Blog index not found")
    return FileResponse(idx, media_type="application/json", headers={"Cache-Control": "public, max-age=300"})


@blog_router.post("/chat", response_model=ChatResponse, dependencies=[Depends(rate_limit_chat)])
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
