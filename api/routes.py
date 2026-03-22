from __future__ import annotations

import os
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

router = APIRouter(prefix="/api", tags=["admin"])


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

    model_config = {"from_attributes": True}


class StatsOut(BaseModel):
    total_posts: int
    published: int
    failed: int
    queued: int
    total_messages_in: int
    total_messages_out: int
    messages_unanswered: int


# ── Blog (public) ────────────────────────────────────────────────────────────

@router.get("/blog/posts", response_model=list[BlogPostOut])
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
            )
        )
    return items


@router.get("/media/{filename}")
async def serve_media(filename: str):
    """Serve images from media_cache so the website can display them."""
    safe_name = Path(filename).name
    file_path = Path(settings.media_cache_dir) / safe_name
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, headers={"Cache-Control": "public, max-age=86400"})


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


# ── Web Chat ──────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def web_chat(body: ChatRequest):
    """Public chat endpoint for the website widget."""
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
