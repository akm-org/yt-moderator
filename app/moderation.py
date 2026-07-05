import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.gemini import GeminiDecision, GeminiModerator
from app.models import (
    AppLog,
    BotState,
    ChatMessage,
    ChatUser,
    ModerationAction,
    RuntimeSetting,
    utcnow,
)
from app.spam import SpamDetector
from app.utils import log_event, notify_discord, record_api_usage
from app.youtube import YouTubeClient, YouTubeError


LOGGER = logging.getLogger(__name__)

DEFAULT_RUNTIME_SETTINGS: dict[str, Any] = {
    "spam_sensitivity": 1.0,
    "warning_limit": 2,
    "timeout_duration": 300,
    "ban_duration": 0,
    "ai_enabled": True,
    "ai_min_score": 20,
    "profanity_filter": True,
    "emoji_limit": 8,
    "caps_limit": 0.72,
    "link_filter": True,
    "whitelist": [],
    "blacklist": [],
    "temporary_whitelist": [],
    "keyword_alerts": ["dox", "swat", "address leak"],
    "raid_protection": True,
    "slow_mode_suggestion_threshold": 120,
    "delete_threshold": 40,
    "timeout_threshold": 70,
}

ACTION_RANK = {"allow": 0, "warn": 1, "delete": 2, "timeout": 3, "ban": 4}


def ensure_runtime_settings(db: Session) -> dict[str, Any]:
    for key, value in DEFAULT_RUNTIME_SETTINGS.items():
        if db.get(RuntimeSetting, key) is None:
            db.add(RuntimeSetting(key=key, value=value))
    db.commit()
    return get_runtime_settings(db)


def get_runtime_settings(db: Session) -> dict[str, Any]:
    values = dict(DEFAULT_RUNTIME_SETTINGS)
    for row in db.query(RuntimeSetting).all():
        values[row.key] = row.value
    return values


def set_runtime_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(RuntimeSetting, key)
    if row:
        row.value = value
    else:
        row = RuntimeSetting(key=key, value=value)
    db.add(row)


def get_bot_state(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(BotState, key)
    return row.value if row else default


def set_bot_state(db: Session, key: str, value: Any) -> None:
    row = db.get(BotState, key)
    if row:
        row.value = value
    else:
        row = BotState(key=key, value=value)
    db.add(row)


def action_from_score(score: int, runtime: dict[str, Any]) -> str:
    if score < 20:
        return "allow"
    if score < int(runtime.get("delete_threshold", 40)):
        return "warn"
    if score <= int(runtime.get("timeout_threshold", 70)):
        return "delete"
    return "timeout"


def stronger_action(*actions: str) -> str:
    return max(actions, key=lambda item: ACTION_RANK.get(item, 0))


class ModerationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.spam_detector = SpamDetector()
        self.gemini = GeminiModerator(settings)

    async def process_youtube_message(
        self,
        db: Session,
        raw_item: dict[str, Any],
        *,
        live_chat_id: str | None,
        video_id: str | None,
        youtube_client: YouTubeClient | None = None,
    ) -> dict[str, Any]:
        message_id = raw_item.get("id")
        if not message_id:
            raise ValueError("YouTube message item has no id")

        existing = db.query(ChatMessage).filter(ChatMessage.youtube_message_id == message_id).first()
        if existing:
            return {"duplicate": True, "message": self._message_payload(existing), "action": None}

        snippet = raw_item.get("snippet") or {}
        author = raw_item.get("authorDetails") or {}
        text = (
            ((snippet.get("textMessageDetails") or {}).get("messageText"))
            or snippet.get("displayMessage")
            or ""
        )
        username = author.get("displayName") or "Unknown user"
        author_channel_id = author.get("channelId") or f"unknown:{username}"
        is_privileged = bool(author.get("isChatOwner") or author.get("isChatModerator") or author.get("isChatSponsor"))
        published_at = self._parse_datetime(snippet.get("publishedAt"))

        runtime = get_runtime_settings(db)
        user = self._upsert_user(db, username=username, channel_id=author_channel_id)
        user.last_message = text
        user.last_seen = utcnow()

        user.is_whitelisted = user.is_whitelisted or author_channel_id in set(runtime.get("whitelist", []) or [])
        user.is_blacklisted = user.is_blacklisted or author_channel_id in set(runtime.get("blacklist", []) or [])

        if is_privileged or user.is_whitelisted:
            spam_result = self.spam_detector.score(
                text,
                channel_id=author_channel_id,
                username=username,
                settings=runtime,
            )
            spam_result.score = 0
            spam_result.reasons.append("privileged or whitelisted user")
        else:
            spam_result = self.spam_detector.score(
                text,
                channel_id=author_channel_id,
                username=username,
                settings=runtime,
            )

        ai_decision = GeminiDecision()
        should_ask_ai = (
            bool(runtime.get("ai_enabled", True))
            and not is_privileged
            and not user.is_whitelisted
            and (
                spam_result.score >= int(runtime.get("ai_min_score", 20))
                or bool(spam_result.keyword_alerts)
                or user.is_blacklisted
            )
        )
        if should_ask_ai:
            ai_decision = await self.gemini.classify(
                username=username,
                message=text,
                spam_score=spam_result.score,
                spam_reasons=spam_result.reasons,
            )
            record_api_usage(
                db,
                "gemini",
                "moderate",
                status=ai_decision.status,
                latency_ms=ai_decision.latency_ms,
            )

        final_action = self._decide_action(
            spam_score=spam_result.score,
            ai=ai_decision,
            user=user,
            runtime=runtime,
            privileged=is_privileged,
        )
        reason = self._reason(spam_result.reasons, ai_decision)

        chat_message = ChatMessage(
            youtube_message_id=message_id,
            live_chat_id=live_chat_id,
            video_id=video_id,
            author_channel_id=author_channel_id,
            username=username,
            message=text,
            published_at=published_at,
            spam_score=spam_result.score,
            spam_reasons=spam_result.reasons,
            suspicious=spam_result.score >= 20 or final_action != "allow",
            ai_action=ai_decision.action,
            ai_reason=ai_decision.reason,
            ai_severity=ai_decision.severity,
            ai_categories=ai_decision.categories,
            final_action=final_action,
            action_status="pending",
            raw_payload=raw_item,
        )
        db.add(chat_message)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = db.query(ChatMessage).filter(ChatMessage.youtube_message_id == message_id).first()
            return {"duplicate": True, "message": self._message_payload(existing), "action": None}

        action_row = ModerationAction(
            message_id=chat_message.id,
            user_channel_id=author_channel_id,
            username=username,
            action=final_action,
            reason=reason,
            severity=ai_decision.severity,
            spam_score=spam_result.score,
            executed=final_action == "allow" or not self.settings.auto_moderate,
        )
        db.add(action_row)

        self._update_user_counters(user, final_action, spam_result.score, ai_decision)
        log_event(
            db,
            "INFO",
            "moderation_decision",
            f"{final_action.upper()} {username}: {reason}",
            {
                "message_id": message_id,
                "spam_score": spam_result.score,
                "ai_action": ai_decision.action,
                "categories": ai_decision.categories,
            },
        )
        db.flush()

        if self.settings.auto_moderate and final_action != "allow" and youtube_client:
            await self._execute_action(
                db,
                action_row,
                chat_message,
                runtime,
                youtube_client,
            )

        db.commit()
        if final_action in {"warn", "timeout", "ban"}:
            await notify_discord(
                self.settings.discord_webhook,
                f"YouTube moderation: {final_action}",
                reason,
                {
                    "user": username,
                    "spam score": spam_result.score,
                    "message": text[:250],
                },
            )
        return {
            "duplicate": False,
            "message": self._message_payload(chat_message),
            "action": self._action_payload(action_row),
        }

    async def manual_action(
        self,
        db: Session,
        *,
        message_id: int,
        action: str,
        reason: str,
        youtube_client: YouTubeClient | None,
        admin_username: str,
    ) -> ModerationAction:
        if action not in ACTION_RANK:
            raise ValueError("Invalid moderation action")
        message = db.get(ChatMessage, message_id)
        if not message:
            raise ValueError("Message not found")
        row = ModerationAction(
            message_id=message.id,
            user_channel_id=message.author_channel_id,
            username=message.username,
            action=action,
            reason=reason or f"Manual action by {admin_username}",
            severity=3 if action in {"delete", "timeout"} else 5 if action == "ban" else 1,
            spam_score=message.spam_score,
            admin_override=True,
            executed=action == "allow" or not youtube_client,
        )
        db.add(row)
        message.final_action = action
        db.flush()
        if youtube_client and action != "allow":
            await self._execute_action(db, row, message, get_runtime_settings(db), youtube_client)
        log_event(
            db,
            "INFO",
            "manual_moderation",
            f"{admin_username} applied {action} to {message.username}",
            {"message_id": message.id, "reason": row.reason},
        )
        db.commit()
        return row

    def _decide_action(
        self,
        *,
        spam_score: int,
        ai: GeminiDecision,
        user: ChatUser,
        runtime: dict[str, Any],
        privileged: bool,
    ) -> str:
        if privileged or user.is_whitelisted:
            return "allow"
        if user.is_blacklisted:
            return "ban"

        action = action_from_score(spam_score, runtime)
        if ai.status == "ok":
            action = stronger_action(action, ai.action)
        if ai.severity >= 4 and {"Hate Speech", "Threats", "Phishing", "Scam"}.intersection(ai.categories):
            action = stronger_action(action, "timeout")
        if ai.severity >= 5 and {"Threats", "Phishing"}.intersection(ai.categories):
            action = stronger_action(action, "ban")

        warning_limit = int(runtime.get("warning_limit", 2))
        if action == "warn" and user.warnings >= warning_limit:
            action = "delete"
        if action in {"warn", "delete"} and user.timeouts >= 2 and spam_score >= 40:
            action = "timeout"
        if action == "timeout" and user.timeouts >= 3:
            action = "ban"
        return action

    @staticmethod
    def _reason(spam_reasons: list[str], ai: GeminiDecision) -> str:
        pieces = []
        if spam_reasons:
            pieces.append("Local: " + ", ".join(spam_reasons))
        if ai.status == "ok":
            pieces.append(f"Gemini: {ai.reason}")
        elif ai.status == "error":
            pieces.append(ai.reason)
        return " | ".join(pieces) or "No moderation issue detected"

    @staticmethod
    def _upsert_user(db: Session, *, username: str, channel_id: str) -> ChatUser:
        user = db.query(ChatUser).filter(ChatUser.channel_id == channel_id).first()
        if user:
            user.username = username
            return user
        user = ChatUser(username=username, channel_id=channel_id)
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def _update_user_counters(
        user: ChatUser,
        action: str,
        spam_score: int,
        ai: GeminiDecision,
    ) -> None:
        if action == "warn":
            user.warnings += 1
        elif action == "timeout":
            user.timeouts += 1
        elif action == "ban":
            user.bans += 1
        user.last_spam_score = spam_score
        spam_history = list(user.spam_score_history or [])[-49:]
        spam_history.append({"score": spam_score, "at": utcnow().isoformat()})
        user.spam_score_history = spam_history
        if ai.status == "ok":
            ai_history = list(user.ai_history or [])[-49:]
            ai_history.append(
                {
                    "action": ai.action,
                    "reason": ai.reason,
                    "severity": ai.severity,
                    "categories": ai.categories,
                    "at": utcnow().isoformat(),
                }
            )
            user.ai_history = ai_history

    async def _execute_action(
        self,
        db: Session,
        action_row: ModerationAction,
        message: ChatMessage,
        runtime: dict[str, Any],
        youtube_client: YouTubeClient,
    ) -> None:
        try:
            response: dict[str, Any] = {}
            if action_row.action == "warn":
                if self.settings.send_warning_messages and message.live_chat_id:
                    response = await youtube_client.send_chat_message(
                        live_chat_id=message.live_chat_id,
                        text=f"@{message.username} please keep chat on-topic and respectful.",
                    )
                action_row.executed = True
            elif action_row.action == "delete":
                response = await youtube_client.delete_message(message.youtube_message_id)
                action_row.executed = True
            elif action_row.action == "timeout":
                if message.live_chat_id and message.author_channel_id:
                    try:
                        await youtube_client.delete_message(message.youtube_message_id)
                    except YouTubeError:
                        LOGGER.info("Delete before timeout failed", exc_info=True)
                    response = await youtube_client.timeout_user(
                        live_chat_id=message.live_chat_id,
                        channel_id=message.author_channel_id,
                        duration_seconds=int(runtime.get("timeout_duration", self.settings.default_timeout_seconds)),
                    )
                    action_row.executed = True
            elif action_row.action == "ban":
                if message.live_chat_id and message.author_channel_id:
                    response = await youtube_client.ban_user(
                        live_chat_id=message.live_chat_id,
                        channel_id=message.author_channel_id,
                    )
                    action_row.executed = True
            action_row.youtube_response = response or {}
            message.action_status = "executed" if action_row.executed else "pending"
        except Exception as exc:
            action_row.executed = False
            action_row.youtube_response = {"error": str(exc)}
            message.action_status = "error"
            log_event(
                db,
                "ERROR",
                "youtube_action_failed",
                str(exc),
                {"message_id": message.id, "action": action_row.action},
            )

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _message_payload(message: ChatMessage | None) -> dict[str, Any] | None:
        if not message:
            return None
        return {
            "id": message.id,
            "youtube_message_id": message.youtube_message_id,
            "username": message.username,
            "author_channel_id": message.author_channel_id,
            "message": message.message,
            "spam_score": message.spam_score,
            "spam_reasons": message.spam_reasons,
            "final_action": message.final_action,
            "action_status": message.action_status,
            "received_at": message.received_at.isoformat() if message.received_at else None,
            "ai_categories": message.ai_categories,
        }

    @staticmethod
    def _action_payload(action: ModerationAction | None) -> dict[str, Any] | None:
        if not action:
            return None
        return {
            "id": action.id,
            "message_id": action.message_id,
            "username": action.username,
            "action": action.action,
            "reason": action.reason,
            "severity": action.severity,
            "spam_score": action.spam_score,
            "executed": action.executed,
            "created_at": action.created_at.isoformat() if action.created_at else None,
        }


def recent_actions(db: Session, limit: int = 20) -> list[ModerationAction]:
    return db.query(ModerationAction).order_by(desc(ModerationAction.created_at)).limit(limit).all()


def recent_app_logs(db: Session, limit: int = 100, search: str | None = None) -> list[AppLog]:
    query = db.query(AppLog)
    if search:
        like = f"%{search}%"
        query = query.filter((AppLog.message.ilike(like)) | (AppLog.event.ilike(like)))
    return query.order_by(desc(AppLog.created_at)).limit(limit).all()


def messages_per_minute(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
    return db.query(ChatMessage).filter(ChatMessage.received_at >= cutoff).count()

