from __future__ import annotations

import datetime as dt
from enum import Enum as PyEnum

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, Enum, Float, ForeignKey,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class PostStatus(str, PyEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class MessageDirection(str, PyEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=True)
    content_raw = Column(Text, nullable=False)
    source = Column(String(50), default="ai")  # ai / rss / manual
    source_url = Column(String(2000), nullable=True)
    image_path = Column(String(1000), nullable=True)
    video_path = Column(String(1000), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    place_name = Column(String(500), nullable=True)
    translations = Column(Text, nullable=True)  # JSON: {"en": {"title":"...","content":"..."}, ...}
    scheduled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    publications = relationship("Publication", back_populates="post", cascade="all, delete-orphan")


class Publication(Base):
    __tablename__ = "publications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)
    platform = Column(String(20), nullable=False)
    platform_post_id = Column(String(500), nullable=True)
    content_adapted = Column(Text, nullable=True)
    status = Column(Enum(PostStatus), default=PostStatus.QUEUED)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    post = relationship("Post", back_populates="publications")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    platform_message_id = Column(String(500), nullable=True)
    sender_id = Column(String(500), nullable=True)
    sender_name = Column(String(500), nullable=True)
    direction = Column(Enum(MessageDirection), nullable=False)
    text = Column(Text, nullable=True)
    thread_id = Column(String(500), nullable=True)
    category = Column(String(50), nullable=True)  # faq, support, spam, human_needed
    replied = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class RSSSource(Base):
    __tablename__ = "rss_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    url = Column(String(2000), nullable=False)
    enabled = Column(Boolean, default=True)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ReactionSnapshot(Base):
    """Latest reaction counts per message+emoji, updated on each Telegram event."""
    __tablename__ = "reaction_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    message_id = Column(String(500), nullable=False)
    emoji = Column(String(20), nullable=False)
    category = Column(String(20), nullable=False)   # "positive" or "negative"
    total_count = Column(Integer, default=0)
    message_date = Column(String(10), nullable=True)  # YYYY-MM-DD of the message
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, server_default=func.now())


class TokenStore(Base):
    __tablename__ = "token_store"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False, unique=True)
    token = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, server_default=func.now())


class KVStore(Base):
    """Simple key-value store for persistent counters (pool indices, etc.)."""
    __tablename__ = "kv_store"

    key = Column(String(100), primary_key=True)
    value = Column(String(500), nullable=False, default="0")


class DailyStats(Base):
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False)       # YYYY-MM-DD
    platform = Column(String(20), nullable=False)
    subscribers = Column(Integer, default=0)
    posts = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)              # positive + neutral reactions
    dislikes = Column(Integer, default=0)           # negative reactions
    collected_at = Column(DateTime, server_default=func.now())
