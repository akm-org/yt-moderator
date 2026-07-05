from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key = Column(String(120), primary_key=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class BotState(Base):
    __tablename__ = "bot_state"

    key = Column(String(120), primary_key=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class ChatUser(Base):
    __tablename__ = "chat_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False, index=True)
    channel_id = Column(String(255), unique=True, nullable=False, index=True)
    warnings = Column(Integer, default=0, nullable=False)
    timeouts = Column(Integer, default=0, nullable=False)
    bans = Column(Integer, default=0, nullable=False)
    last_message = Column(Text, nullable=True)
    last_spam_score = Column(Float, default=0, nullable=False)
    spam_score_history = Column(JSON, default=list, nullable=False)
    ai_history = Column(JSON, default=list, nullable=False)
    first_seen = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_whitelisted = Column(Boolean, default=False, nullable=False)
    is_blacklisted = Column(Boolean, default=False, nullable=False)
    notes = Column(Text, nullable=True)

    messages = relationship("ChatMessage", back_populates="user")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("youtube_message_id", name="uq_youtube_message_id"),
    )

    id = Column(Integer, primary_key=True)
    youtube_message_id = Column(String(255), nullable=False, index=True)
    live_chat_id = Column(String(255), nullable=True, index=True)
    video_id = Column(String(255), nullable=True, index=True)
    author_channel_id = Column(
        String(255),
        ForeignKey("chat_users.channel_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    username = Column(String(255), nullable=False, index=True)
    message = Column(Text, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    received_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    spam_score = Column(Float, default=0, nullable=False)
    spam_reasons = Column(JSON, default=list, nullable=False)
    suspicious = Column(Boolean, default=False, nullable=False)
    ai_action = Column(String(40), nullable=True)
    ai_reason = Column(Text, nullable=True)
    ai_severity = Column(Integer, nullable=True)
    ai_categories = Column(JSON, default=list, nullable=False)
    final_action = Column(String(40), default="allow", nullable=False)
    action_status = Column(String(40), default="pending", nullable=False)
    raw_payload = Column(JSON, default=dict, nullable=False)

    user = relationship("ChatUser", back_populates="messages")
    moderation_actions = relationship("ModerationAction", back_populates="message")


class ModerationAction(Base):
    __tablename__ = "moderation_actions"

    id = Column(Integer, primary_key=True)
    message_id = Column(
        Integer,
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_channel_id = Column(String(255), nullable=True, index=True)
    username = Column(String(255), nullable=False, index=True)
    action = Column(String(40), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    severity = Column(Integer, default=1, nullable=False)
    spam_score = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    executed = Column(Boolean, default=False, nullable=False)
    admin_override = Column(Boolean, default=False, nullable=False)
    youtube_response = Column(JSON, default=dict, nullable=False)

    message = relationship("ChatMessage", back_populates="moderation_actions")


class AppLog(Base):
    __tablename__ = "app_logs"

    id = Column(Integer, primary_key=True)
    level = Column(String(20), default="INFO", nullable=False, index=True)
    event = Column(String(120), nullable=False, index=True)
    message = Column(Text, nullable=False)
    context = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, nullable=True, index=True)
    username = Column(String(80), nullable=True)
    action = Column(String(120), nullable=False, index=True)
    ip_address = Column(String(80), nullable=True)
    user_agent = Column(Text, nullable=True)
    details = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id = Column(Integer, primary_key=True)
    provider = Column(String(80), nullable=False, index=True)
    operation = Column(String(120), nullable=False, index=True)
    count = Column(Integer, default=1, nullable=False)
    tokens = Column(Integer, default=0, nullable=False)
    status = Column(String(40), default="ok", nullable=False)
    latency_ms = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

