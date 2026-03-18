from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.platforms import Platform, PLATFORM_LIMITS
from config.settings import settings
from content.generator import generate_post_text
from content.media import get_image_for_post, create_slideshow_video
from content.rss_parser import parse_all_sources
from db.database import async_session
from db.models import Post, Publication, PostStatus

logger = logging.getLogger(__name__)

ALL_PLATFORMS = list(Platform)

MAX_RETRIES = 3


def get_platform_instance(platform: Platform):
    from platforms.telegram import TelegramPlatform
    from platforms.facebook import FacebookPlatform
    from platforms.twitter import TwitterPlatform
    from platforms.instagram import InstagramPlatform
    from platforms.tiktok import TikTokPlatform

    _registry = {
        Platform.TELEGRAM: TelegramPlatform,
        Platform.FACEBOOK: FacebookPlatform,
        Platform.TWITTER: TwitterPlatform,
        Platform.INSTAGRAM: InstagramPlatform,
        Platform.TIKTOK: TikTokPlatform,
    }
    return _registry[platform]()


async def create_daily_posts() -> None:
    """Generate 3 posts for today: 1 from RSS (if available) + 2 AI-generated."""
    async with async_session() as session:
        rss_entries = await parse_all_sources(session)

        topics = [
            f"New feature or update about {settings.app_name}",
            f"Industry tip related to {settings.app_description}",
            f"Engaging question or poll about {settings.app_description}",
        ]

        posts_to_create = 3
        created = 0

        # Use 1 RSS entry if available
        if rss_entries and created < posts_to_create:
            entry = rss_entries[0]
            post = Post(
                title=entry["title"],
                content_raw=entry["summary"] or entry["title"],
                source="rss",
                source_url=entry["link"],
            )
            session.add(post)
            await session.flush()

            for platform in ALL_PLATFORMS:
                pub = Publication(post_id=post.id, platform=platform.value)
                session.add(pub)
            created += 1

        # Fill remaining with AI-generated
        for i in range(created, posts_to_create):
            topic = topics[i % len(topics)]
            post = Post(
                title=topic,
                content_raw=topic,
                source="ai",
            )
            session.add(post)
            await session.flush()

            for platform in ALL_PLATFORMS:
                pub = Publication(post_id=post.id, platform=platform.value)
                session.add(pub)

        await session.commit()
        logger.info("Created %d posts for today", posts_to_create)


async def publish_scheduled_post(time_slot: int) -> None:
    """Publish the post for a specific time slot (0, 1, or 2).

    Picks the next queued post and publishes to all platforms.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Post)
            .join(Publication)
            .where(Publication.status == PostStatus.QUEUED)
            .order_by(Post.created_at)
            .limit(1)
        )
        post = result.scalar_one_or_none()
        if not post:
            logger.warning("No queued posts for time slot %d", time_slot)
            return

        pubs_result = await session.execute(
            select(Publication)
            .where(Publication.post_id == post.id, Publication.status == PostStatus.QUEUED)
        )
        publications = pubs_result.scalars().all()

        image_path = post.image_path
        if not image_path:
            image_path = await get_image_for_post(post.content_raw[:100])
            if image_path:
                post.image_path = image_path
                await session.commit()

        for pub in publications:
            platform = Platform(pub.platform)
            await _publish_single(session, post, pub, platform, image_path)

        await session.commit()


async def _publish_single(
    session: AsyncSession,
    post: Post,
    pub: Publication,
    platform: Platform,
    image_path: Optional[str],
) -> None:
    try:
        pub.status = PostStatus.PUBLISHING

        if not pub.content_adapted:
            if post.source == "rss":
                pub.content_adapted = await generate_post_text(
                    topic="", platform=platform, source_text=post.content_raw
                )
            else:
                pub.content_adapted = await generate_post_text(
                    topic=post.content_raw, platform=platform
                )

        adapter = get_platform_instance(platform)

        if platform == Platform.TIKTOK:
            if post.video_path:
                result = await adapter.publish_video(pub.content_adapted, post.video_path)
            elif image_path:
                video_path = await create_slideshow_video(
                    [image_path], text_overlay=pub.content_adapted[:100]
                )
                if video_path:
                    result = await adapter.publish_video(pub.content_adapted, video_path)
                else:
                    result = await adapter.publish_text(pub.content_adapted)
            else:
                pub.status = PostStatus.FAILED
                pub.error_message = "TikTok requires video; no media available"
                return
        else:
            result = await adapter.publish_text(pub.content_adapted, image_path)

        if result.success:
            pub.status = PostStatus.PUBLISHED
            pub.platform_post_id = result.platform_post_id
            pub.published_at = datetime.now(timezone.utc)
            logger.info("Published to %s: post_id=%s", platform.value, result.platform_post_id)
        else:
            pub.retry_count += 1
            if pub.retry_count >= MAX_RETRIES:
                pub.status = PostStatus.FAILED
            else:
                pub.status = PostStatus.QUEUED
            pub.error_message = result.error
            logger.error("Failed to publish to %s: %s", platform.value, result.error)

    except Exception as e:
        pub.retry_count += 1
        if pub.retry_count >= MAX_RETRIES:
            pub.status = PostStatus.FAILED
        else:
            pub.status = PostStatus.QUEUED
        pub.error_message = str(e)
        logger.exception("Exception publishing to %s", platform.value)


async def retry_failed_publications() -> None:
    """Retry publications that failed but haven't exceeded max retries."""
    async with async_session() as session:
        result = await session.execute(
            select(Publication)
            .where(
                Publication.status == PostStatus.FAILED,
                Publication.retry_count < MAX_RETRIES,
            )
        )
        pubs = result.scalars().all()

        for pub in pubs:
            pub.status = PostStatus.QUEUED
            pub.error_message = None

        await session.commit()
        if pubs:
            logger.info("Reset %d failed publications for retry", len(pubs))
