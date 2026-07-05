import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings


LOGGER = logging.getLogger(__name__)
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class YouTubeError(RuntimeError):
    pass


class YouTubeNotConfigured(YouTubeError):
    pass


@dataclass
class LiveBroadcast:
    video_id: str
    live_chat_id: str
    title: str
    channel_title: str
    started_at: str | None = None


class YouTubeClient:
    def __init__(self, settings: Settings, *, refresh_token: str | None = None) -> None:
        self.settings = settings
        self.refresh_token = refresh_token or settings.google_refresh_token
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.google_client_id
            and self.settings.google_client_secret
            and self.refresh_token
        )

    def build_auth_url(self, state: str | None = None, *, callback_path: str = "/callback") -> tuple[str, str]:
        if not self.settings.google_client_id:
            raise YouTubeNotConfigured("GOOGLE_CLIENT_ID is not configured")
        state = state or secrets.token_urlsafe(24)
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": f"{self.settings.base_url}{callback_path}",
            "response_type": "code",
            "scope": " ".join(self.settings.youtube_scope_list),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "include_granted_scopes": "true",
        }
        return f"{AUTH_URL}?{urlencode(params)}", state

    async def exchange_code(self, code: str, *, callback_path: str = "/callback") -> dict[str, Any]:
        if not self.settings.google_client_id or not self.settings.google_client_secret:
            raise YouTubeNotConfigured("Google OAuth client ID/secret are not configured")
        payload = {
            "code": code,
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret,
            "redirect_uri": f"{self.settings.base_url}{callback_path}",
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient(timeout=self.settings.youtube_request_timeout_seconds) as client:
            response = await client.post(TOKEN_URL, data=payload)
        response.raise_for_status()
        token = response.json()
        self.refresh_token = token.get("refresh_token") or self.refresh_token
        self._access_token = token.get("access_token")
        self._access_token_expires_at = time.monotonic() + int(token.get("expires_in", 3600)) - 60
        return token

    async def get_my_channel(self) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            "channels",
            params={"part": "snippet,contentDetails,statistics", "mine": "true", "maxResults": 1},
        )
        items = response.get("items") or []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        return {
            "id": item.get("id"),
            "title": snippet.get("title"),
            "custom_url": snippet.get("customUrl"),
            "description": snippet.get("description"),
            "thumbnail": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url"),
            "subscriber_count": stats.get("subscriberCount"),
            "video_count": stats.get("videoCount"),
            "view_count": stats.get("viewCount"),
        }

    async def get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at:
            return self._access_token
        if not self.configured:
            raise YouTubeNotConfigured("YouTube OAuth credentials are not configured")
        payload = {
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=self.settings.youtube_request_timeout_seconds) as client:
            response = await client.post(TOKEN_URL, data=payload)
        response.raise_for_status()
        token = response.json()
        self._access_token = token["access_token"]
        self._access_token_expires_at = time.monotonic() + int(token.get("expires_in", 3600)) - 60
        return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self.get_access_token()
        url = path if path.startswith("http") else f"{YOUTUBE_API}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}"}
        last_error: Exception | None = None
        for attempt in range(self.settings.youtube_max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.settings.youtube_request_timeout_seconds) as client:
                    response = await client.request(method, url, params=params, json=json_body, headers=headers)
                if response.status_code == 401 and attempt == 0:
                    self._access_token = None
                    token = await self.get_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
            except Exception as exc:
                last_error = exc
                LOGGER.warning("YouTube API attempt %s failed: %s", attempt + 1, exc)
                await self._sleep_backoff(attempt)
        raise YouTubeError(f"YouTube API request failed: {last_error}")

    async def find_active_livestream(self, *, channel_id: str | None = None) -> LiveBroadcast | None:
        """Find the active live chat using liveBroadcasts first, then channel search."""
        if not self.configured:
            raise YouTubeNotConfigured("YouTube OAuth credentials are not configured")

        # Use Search API instead of liveBroadcasts because
        # Google rejects mine=true with broadcastStatus=active.
        fallback_channel_id = channel_id or self.settings.channel_id
        if not fallback_channel_id:
            return None

        search = await self._request(
            "GET",
            "search",
            params={
                "part": "snippet",
                "channelId": fallback_channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 5,
            },
        )
        video_ids = [
            (item.get("id") or {}).get("videoId")
            for item in search.get("items", [])
            if (item.get("id") or {}).get("videoId")
        ]
        if not video_ids:
            return None

        videos = await self._request(
            "GET",
            "videos",
            params={
                "part": "snippet,liveStreamingDetails",
                "id": ",".join(video_ids),
            },
        )
        for item in videos.get("items", []):
            details = item.get("liveStreamingDetails") or {}
            live_chat_id = details.get("activeLiveChatId")
            if live_chat_id:
                snippet = item.get("snippet") or {}
                return LiveBroadcast(
                    video_id=item.get("id", ""),
                    live_chat_id=live_chat_id,
                    title=snippet.get("title", "Untitled live stream"),
                    channel_title=snippet.get("channelTitle", ""),
                    started_at=details.get("actualStartTime") or details.get("scheduledStartTime"),
                )
        return None

    async def get_live_chat_messages(
        self,
        live_chat_id: str,
        *,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "liveChatId": live_chat_id,
            "part": "id,snippet,authorDetails",
            "maxResults": self.settings.max_messages_per_poll,
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", "liveChat/messages", params=params)

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        return await self._request("DELETE", "liveChat/messages", params={"id": message_id})

    async def timeout_user(
        self,
        *,
        live_chat_id: str,
        channel_id: str,
        duration_seconds: int,
    ) -> dict[str, Any]:
        body = {
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "temporary",
                "banDurationSeconds": duration_seconds,
                "bannedUserDetails": {"channelId": channel_id},
            }
        }
        return await self._request("POST", "liveChat/bans", params={"part": "snippet"}, json_body=body)

    async def ban_user(self, *, live_chat_id: str, channel_id: str) -> dict[str, Any]:
        body = {
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "permanent",
                "bannedUserDetails": {"channelId": channel_id},
            }
        }
        return await self._request("POST", "liveChat/bans", params={"part": "snippet"}, json_body=body)

    async def send_chat_message(self, *, live_chat_id: str, text: str) -> dict[str, Any]:
        body = {
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": text[:200]},
            }
        }
        return await self._request("POST", "liveChat/messages", params={"part": "snippet"}, json_body=body)

    async def api_health(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "ok": False, "message": "YouTube OAuth is not configured"}
        try:
            await self.get_access_token()
            return {"configured": True, "ok": True, "message": "OAuth token refresh succeeded"}
        except Exception as exc:
            return {"configured": True, "ok": False, "message": str(exc)}

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        import asyncio

        await asyncio.sleep(min(8, 2**attempt))
