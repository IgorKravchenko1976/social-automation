from __future__ import annotations

import logging
from typing import Optional

import tweepy

from config.settings import settings
from config.platforms import Platform
from platforms.base import BasePlatform, PublishResult

logger = logging.getLogger(__name__)


class TwitterPlatform(BasePlatform):
    platform = Platform.TWITTER

    def __init__(self):
        self._client: Optional[tweepy.Client] = None
        self._auth: Optional[tweepy.OAuth1UserHandler] = None
        self._api_v1: Optional[tweepy.API] = None

    @property
    def client(self) -> tweepy.Client:
        if self._client is None:
            self._client = tweepy.Client(
                bearer_token=settings.twitter_bearer_token,
                consumer_key=settings.twitter_api_key,
                consumer_secret=settings.twitter_api_secret,
                access_token=settings.twitter_access_token,
                access_token_secret=settings.twitter_access_token_secret,
            )
        return self._client

    @property
    def api_v1(self) -> tweepy.API:
        """V1.1 API needed for media upload."""
        if self._api_v1 is None:
            auth = tweepy.OAuth1UserHandler(
                settings.twitter_api_key,
                settings.twitter_api_secret,
                settings.twitter_access_token,
                settings.twitter_access_token_secret,
            )
            self._api_v1 = tweepy.API(auth)
        return self._api_v1

    async def publish_text(self, text: str, image_path: Optional[str] = None) -> PublishResult:
        try:
            media_ids = None
            if image_path:
                media = self.api_v1.media_upload(filename=image_path)
                media_ids = [media.media_id]

            response = self.client.create_tweet(text=text[:280], media_ids=media_ids)
            tweet_id = response.data.get("id") if response.data else None
            return PublishResult(success=True, platform_post_id=str(tweet_id))
        except Exception as e:
            logger.exception("Twitter publish failed")
            return PublishResult(success=False, error=str(e))

    async def publish_video(self, text: str, video_path: str) -> PublishResult:
        try:
            media = self.api_v1.media_upload(
                filename=video_path,
                media_category="tweet_video",
                chunked=True,
            )
            response = self.client.create_tweet(text=text[:280], media_ids=[media.media_id])
            tweet_id = response.data.get("id") if response.data else None
            return PublishResult(success=True, platform_post_id=str(tweet_id))
        except Exception as e:
            logger.exception("Twitter video publish failed")
            return PublishResult(success=False, error=str(e))

    async def get_new_messages(self) -> list[dict]:
        """Fetch recent mentions."""
        try:
            me = self.client.get_me()
            if not me.data:
                return []

            mentions = self.client.get_users_mentions(
                id=me.data.id,
                max_results=10,
                tweet_fields=["author_id", "created_at"],
            )
            messages = []
            if mentions.data:
                for tweet in mentions.data:
                    messages.append({
                        "platform_message_id": str(tweet.id),
                        "sender_id": str(tweet.author_id),
                        "sender_name": "",
                        "text": tweet.text,
                    })
            return messages
        except Exception:
            logger.exception("Twitter fetch mentions failed")
            return []

    async def send_reply(self, platform_message_id: str, text: str) -> bool:
        try:
            self.client.create_tweet(
                text=text[:280],
                in_reply_to_tweet_id=int(platform_message_id),
            )
            return True
        except Exception:
            logger.exception("Twitter reply failed")
            return False
