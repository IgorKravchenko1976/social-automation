"""Admin API endpoints (require X-API-Key authentication)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform
from config.settings import settings
from db.database import get_session
from db.models import Post, Publication, PostStatus, Message, MessageDirection, RSSSource
from api.auth import require_admin
from api.schemas import (
    PostOut, PublicationOut, MessageOut,
    CreatePostRequest, AddRSSSourceRequest, StatsOut,
)

admin_router = APIRouter(
    prefix="/api",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ── Posts ─────────────────────────────────────────────────────────────────────

@admin_router.get("/posts", response_model=list[PostOut])
async def list_posts(
    limit: int = Query(20, le=100),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Post).order_by(desc(Post.created_at)).offset(offset).limit(limit)
    )
    return result.scalars().all()


@admin_router.post("/posts", response_model=PostOut, status_code=201)
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


@admin_router.get("/posts/{post_id}/publications", response_model=list[PublicationOut])
async def get_publications(post_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Publication).where(Publication.post_id == post_id)
    )
    return result.scalars().all()


# ── Publications queue ────────────────────────────────────────────────────────

@admin_router.get("/queue", response_model=list[PublicationOut])
async def get_queue(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Publication)
        .where(Publication.status.in_([PostStatus.QUEUED, PostStatus.PUBLISHING]))
        .order_by(Publication.created_at)
    )
    return result.scalars().all()


# ── Messages ──────────────────────────────────────────────────────────────────

@admin_router.get("/messages", response_model=list[MessageOut])
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

@admin_router.get("/rss", response_model=list[dict])
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


@admin_router.post("/rss", status_code=201)
async def add_rss_source(body: AddRSSSourceRequest, session: AsyncSession = Depends(get_session)):
    source = RSSSource(name=body.name, url=body.url)
    session.add(source)
    await session.commit()
    return {"id": source.id, "name": source.name, "url": source.url}


@admin_router.delete("/rss/{source_id}")
async def delete_rss_source(source_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(RSSSource).where(RSSSource.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(404, "RSS source not found")
    await session.delete(source)
    await session.commit()
    return {"deleted": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@admin_router.get("/stats", response_model=StatsOut)
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


# ── Territory safety audit ────────────────────────────────────────────────────

@admin_router.get("/debug/territory-audit")
async def territory_audit(session: AsyncSession = Depends(get_session)):
    """Scan all published posts for blocked territory mentions."""
    from content.tourism_topics import contains_blocked_territory

    result = await session.execute(
        select(Post, Publication)
        .join(Publication)
        .where(Publication.status == PostStatus.PUBLISHED)
        .order_by(desc(Post.created_at))
        .limit(200)
    )
    rows = result.all()

    flagged = []
    for post, pub in rows:
        text = (post.title or "") + " " + (post.content_raw or "") + " " + (pub.content_adapted or "")
        blocked = contains_blocked_territory(text)
        if blocked:
            flagged.append({
                "post_id": post.id,
                "title": (post.title or "")[:120],
                "platform": pub.platform,
                "platform_post_id": pub.platform_post_id,
                "published_at": str(pub.published_at) if pub.published_at else None,
                "blocked_keyword": blocked,
            })

    return {
        "scanned": len(rows),
        "flagged": len(flagged),
        "posts": flagged,
        "action": "Use /api/emergency-delete to remove flagged posts",
    }


# ── Emergency post deletion ──────────────────────────────────────────────────

@admin_router.get("/emergency-delete")
async def emergency_delete_page():
    """Interactive page for emergency post deletion."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Emergency Delete</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;min-height:100vh;display:flex;align-items:center;justify-content:center}
.container{background:#16213e;border-radius:16px;padding:40px;max-width:600px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,0.3)}
h1{color:#e74c3c;margin-bottom:8px;font-size:28px}
.subtitle{color:#a0a0a0;margin-bottom:24px}
textarea{width:100%;height:120px;background:#0f3460;border:2px solid #e74c3c;border-radius:8px;color:#fff;padding:12px;font-size:15px;resize:vertical}
textarea:focus{outline:none;border-color:#ff6b6b}
button{width:100%;margin-top:16px;padding:14px;background:#e74c3c;color:white;border:none;border-radius:8px;font-size:18px;font-weight:bold;cursor:pointer;transition:all .2s}
button:hover{background:#c0392b;transform:translateY(-1px)}
button:disabled{background:#555;cursor:wait}
#result{margin-top:20px;background:#0f3460;border-radius:8px;padding:16px;display:none;max-height:400px;overflow-y:auto}
.found{color:#2ecc71;font-weight:bold}
.not-found{color:#e74c3c;font-weight:bold}
.platform-row{padding:6px 0;border-bottom:1px solid #1a1a2e;font-size:14px}
.ok{color:#2ecc71}.fail{color:#e74c3c}.skip{color:#95a5a6}
</style></head>
<body><div class="container">
<h1>Ekstrenne vydalennya</h1>
<p class="subtitle">Paste post text (or part of it) — the system will find and delete from all platforms</p>
<textarea id="text" placeholder="Paste post text to search and delete..."></textarea>
<button onclick="doDelete()" id="btn">FIND AND DELETE NOW</button>
<div id="result"></div>
</div>
<script>
async function doDelete(){
  const text=document.getElementById('text').value.trim();
  if(!text){alert('Enter post text');return}
  if(!confirm('WARNING! Post will be deleted from all platforms and blog. Continue?'))return;
  const btn=document.getElementById('btn');
  const res=document.getElementById('result');
  btn.disabled=true;btn.textContent='Deleting...';
  res.style.display='block';res.innerHTML='<p>Searching post...</p>';
  try{
    const r=await fetch('/api/emergency-delete',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-API-Key':new URLSearchParams(location.search).get('key')||''},
      body:JSON.stringify({search_text:text})
    });
    const data=await r.json();
    let html='<p><strong>'+data.summary+'</strong></p>';
    if(data.posts_found===0){html+='<p class="not-found">No posts found in DB</p>';}
    else{
      for(const post of data.results||[]){
        html+='<div style="margin:12px 0;padding:8px;background:#1a1a2e;border-radius:6px">';
        html+='<p><strong>Post #'+post.post_id+':</strong> '+post.title+'</p>';
        for(const p of post.platforms||[]){
          const cls=p.deleted?'ok':(p.platform_post_id?'fail':'skip');
          const icon=p.deleted?'ok':(p.platform_post_id?'fail':'skip');
          html+='<div class="platform-row"><span class="'+cls+'">'+icon+' '+p.platform_label+'</span> - '+p.detail+'</div>';
        }
        if(post.blog){
          const cls=post.blog.deleted?'ok':'skip';
          const icon=post.blog.deleted?'ok':'skip';
          html+='<div class="platform-row"><span class="'+cls+'">'+icon+' Blog</span> - '+post.blog.detail+'</div>';
        }
        html+='</div>';
      }
    }
    html+='<p style="color:#a0a0a0;font-size:12px;margin-top:12px">Report sent to email</p>';
    res.innerHTML=html;
  }catch(e){res.innerHTML='<p class="not-found">Error: '+e.message+'</p>';}
  btn.disabled=false;btn.textContent='FIND AND DELETE NOW';
}
</script></body></html>""")


@admin_router.post("/emergency-delete")
async def emergency_delete_action(body: dict):
    """Execute emergency deletion. Body: {"search_text": "..."}"""
    from scheduler.emergency_delete import emergency_delete

    search_text = body.get("search_text", "").strip()
    if not search_text:
        raise HTTPException(400, "search_text is required")
    if len(search_text) < 5:
        raise HTTPException(400, "search_text must be at least 5 characters")

    result = await emergency_delete(search_text)
    return result
