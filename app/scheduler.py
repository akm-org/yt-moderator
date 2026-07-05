import asyncio
import logging
from dataclasses import asdict
from typing import Any

from app.config import Settings
from app.database import new_session
from app.models import AdminUser
from app.moderation import ModerationService, get_bot_state, set_bot_state
from app.utils import log_event, notify_discord, record_api_usage
from app.websocket import ConnectionManager
from app.youtube import LiveBroadcast, YouTubeClient, YouTubeNotConfigured


LOGGER = logging.getLogger(__name__)


class LiveChatWorker:
    def __init__(
        self,
        settings: Settings,
        manager: ConnectionManager,
        moderation_service: ModerationService,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.moderation_service = moderation_service
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._current_broadcast: LiveBroadcast | None = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="youtube-live-chat-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)

    async def _run(self) -> None:
        await notify_discord(self.settings.discord_webhook, "Moderator started", "Live chat worker started")
        backoff = 5
        while not self._stop_event.is_set():
            db = new_session()
            try:
                active_account_id = get_bot_state(db, "active_account_id")
                active_account = db.get(AdminUser, int(active_account_id)) if active_account_id else None
                refresh_token = (
                    (active_account.youtube_refresh_token if active_account else None)
                    or get_bot_state(db, "youtube_refresh_token")
                    or self.settings.google_refresh_token
                )
                client = YouTubeClient(self.settings, refresh_token=refresh_token)
                if not client.configured:
                    set_bot_state(db, "connection", {"connected": False, "message": "YouTube OAuth not configured"})
                    db.commit()
                    await self.manager.broadcast("status", {"connected": False, "message": "YouTube OAuth not configured"})
                    await asyncio.sleep(30)
                    continue

                broadcast = await self._find_or_refresh_broadcast(client, db)
                if not broadcast:
                    set_bot_state(db, "connection", {"connected": False, "message": "No active livestream found"})
                    db.commit()
                    await self.manager.broadcast("status", {"connected": False, "message": "No active livestream found"})
                    await asyncio.sleep(20)
                    continue

                await self._poll_chat(client, broadcast)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except YouTubeNotConfigured as exc:
                LOGGER.info("YouTube worker waiting for credentials: %s", exc)
                await asyncio.sleep(30)
            except Exception as exc:
                LOGGER.exception("Live chat worker crashed")
                try:
                    log_event(db, "ERROR", "worker_error", str(exc), {})
                    set_bot_state(db, "connection", {"connected": False, "message": str(exc)})
                    db.commit()
                    await self.manager.broadcast("status", {"connected": False, "message": str(exc)})
                    await notify_discord(self.settings.discord_webhook, "Moderator error", str(exc))
                except Exception:
                    LOGGER.exception("Failed to record worker error")
                await asyncio.sleep(backoff)
                backoff = min(120, backoff * 2)
            finally:
                db.close()

        await notify_discord(self.settings.discord_webhook, "Moderator stopped", "Live chat worker stopped")

    async def _find_or_refresh_broadcast(self, client: YouTubeClient, db) -> LiveBroadcast | None:
        if self._current_broadcast:
            return self._current_broadcast
        active_account_id = get_bot_state(db, "active_account_id")
        active_account = db.get(AdminUser, int(active_account_id)) if active_account_id else None
        channel_id = (
            (active_account.youtube_channel_id if active_account else None)
            or get_bot_state(db, "youtube_channel_id")
            or self.settings.channel_id
        )
        broadcast = await client.find_active_livestream(channel_id=channel_id)
        if broadcast:
            self._current_broadcast = broadcast
            set_bot_state(
                db,
                "connection",
                {
                    "connected": True,
                    "broadcast": asdict(broadcast),
                    "message": "Connected",
                },
            )
            db.commit()
            await self.manager.broadcast("status", {"connected": True, "broadcast": asdict(broadcast)})
        return broadcast

    async def _poll_chat(self, client: YouTubeClient, broadcast: LiveBroadcast) -> None:
        page_token: str | None = None
        while not self._stop_event.is_set():
            db = new_session()
            try:
                response = await client.get_live_chat_messages(
                    broadcast.live_chat_id,
                    page_token=page_token,
                )
                record_api_usage(db, "youtube", "liveChatMessages.list", status="ok")
                page_token = response.get("nextPageToken")
                interval = max(
                    self.settings.live_chat_min_poll_seconds,
                    (response.get("pollingIntervalMillis") or 2000) / 1000,
                )
                items = response.get("items", [])
                for item in items:
                    result = await self.moderation_service.process_youtube_message(
                        db,
                        item,
                        live_chat_id=broadcast.live_chat_id,
                        video_id=broadcast.video_id,
                        youtube_client=client,
                    )
                    if not result.get("duplicate"):
                        await self.manager.broadcast("message", result["message"])
                        if result.get("action"):
                            await self.manager.broadcast("action", result["action"])
                db.commit()
                await asyncio.sleep(interval)
            except Exception:
                self._current_broadcast = None
                raise
            finally:
                db.close()
