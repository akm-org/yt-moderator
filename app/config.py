from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "YouTube AI Moderator"
    app_env: str = "development"
    debug: bool = False
    base_url: str = "http://localhost:8000"
    secret_key: Annotated[str, Field(min_length=32)] = (
        "dev-only-change-this-secret-key-before-deploying"
    )
    session_cookie_name: str = "yt_ai_moderator"
    secure_cookies: bool = False
    allowed_hosts: str = "*"

    database_url: str = "sqlite:///./moderator.db"

    admin_username: str = "admin"
    admin_password: str = "change-me-now"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_timeout_seconds: float = 20.0
    gemini_max_retries: int = 2

    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    channel_id: str = ""
    youtube_setup_token: str = ""
    youtube_scopes: str = (
        "https://www.googleapis.com/auth/youtube.force-ssl "
        "https://www.googleapis.com/auth/youtube.readonly"
    )
    youtube_request_timeout_seconds: float = 15.0
    youtube_max_retries: int = 3
    max_messages_per_poll: int = 2000
    live_chat_min_poll_seconds: float = 1.0

    worker_enabled: bool = True
    auto_moderate: bool = True
    send_warning_messages: bool = False
    default_timeout_seconds: int = 300

    discord_webhook: str = ""

    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    log_max_bytes: int = 5_242_880
    log_backup_count: int = 5

    rate_limit_per_minute: int = 240
    login_rate_limit_per_minute: int = 8

    @field_validator("app_env")
    @classmethod
    def normalize_env(cls, value: str) -> str:
        return value.lower().strip()

    @field_validator("base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("secret_key")
    @classmethod
    def reject_default_secret_in_production(cls, value: str, info):
        env = info.data.get("app_env", "development")
        if env == "production" and value.startswith("dev-only"):
            raise ValueError("SECRET_KEY must be changed in production")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def youtube_scope_list(self) -> list[str]:
        return [scope for scope in self.youtube_scopes.split() if scope]

    @property
    def log_path(self) -> Path:
        path = Path(self.log_file)
        if not path.is_absolute():
            return BASE_DIR / path
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
