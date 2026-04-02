"""Pydantic request/response schemas for the API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class PostOut(BaseModel):
    id: int
    title: Optional[str]
    content_raw: str
    source: str
    image_path: Optional[str]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    place_name: Optional[str] = None
    pipeline_log: Optional[list[dict]] = None
    scheduled_at: Optional[datetime]
    created_at: Optional[datetime]

    @field_validator("pipeline_log", mode="before")
    @classmethod
    def _parse_pipeline_log(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

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


# -----------  Geo Research Agent  -----------

class GeoResearchRequest(BaseModel):
    latitude: float
    longitude: float
    name: Optional[str] = None
    language: str = "uk"


class GeoResearchSubmitResponse(BaseModel):
    request_id: str
    status: str
    received_at: datetime
    message: str


class HistoryEntry(BaseModel):
    period: str
    description: str


class PlaceEntry(BaseModel):
    name: str
    type: str
    description: str
    url: Optional[str] = None


class NewsEntry(BaseModel):
    title: str
    description: str
    source: Optional[str] = None


class GeoResearchResult(BaseModel):
    summary: str
    history: list[HistoryEntry] = []
    places: list[PlaceEntry] = []
    news: list[NewsEntry] = []


class GeoResearchQueueStatus(BaseModel):
    can_accept: bool
    queue_size: int
    processing: int
    processed_24h: int
    daily_limit: int
    completed_pending_pickup: int


class GeoResearchTaskOut(BaseModel):
    request_id: str
    status: str
    latitude: float
    longitude: float
    name: Optional[str] = None
    language: str
    received_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[GeoResearchResult] = None
    error_message: Optional[str] = None

    @field_validator("result", mode="before")
    @classmethod
    def _parse_result(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    model_config = {"from_attributes": True}
