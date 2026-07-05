import csv
import hmac
import json
import logging
import logging.handlers
import os
import secrets
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import psutil
from sqlalchemy.orm import Session

from app.config import BASE_DIR, Settings
from app.models import AppLog, AuditLog, ApiUsage


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored_hash.split("$", 3)
    except ValueError:
        return False
    if scheme != PASSWORD_SCHEME:
        return False
    test_digest = pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations),
    ).hex()
    return hmac.compare_digest(test_digest, digest)


def configure_logging(settings: Settings) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        "%Y-%m-%dT%H:%M:%S%z",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def compact_json(value: Any) -> str:
    return json.dumps(value, default=json_default, separators=(",", ":"))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_textarea_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    items: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        item = line.strip().lower()
        if item and item not in items:
            items.append(item)
    return items


def render_textarea_list(items: list[str] | None) -> str:
    return "\n".join(items or [])


def log_event(
    db: Session,
    level: str,
    event: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> AppLog:
    row = AppLog(
        level=level.upper(),
        event=event,
        message=message,
        context=context or {},
    )
    db.add(row)
    return row


def audit_event(
    db: Session,
    admin_id: int | None,
    username: str | None,
    action: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        admin_id=admin_id,
        username=username,
        action=action,
        ip_address=ip_address,
        user_agent=user_agent,
        details=details or {},
    )
    db.add(row)
    return row


def record_api_usage(
    db: Session,
    provider: str,
    operation: str,
    *,
    status: str = "ok",
    latency_ms: float = 0,
    tokens: int = 0,
) -> ApiUsage:
    row = ApiUsage(
        provider=provider,
        operation=operation,
        status=status,
        latency_ms=latency_ms,
        tokens=tokens,
    )
    db.add(row)
    return row


async def notify_discord(webhook: str, title: str, message: str, fields: dict[str, Any] | None = None) -> None:
    if not webhook:
        return
    embed = {
        "title": title,
        "description": message[:3900],
        "color": 15_096_235,
        "timestamp": utcnow().isoformat(),
        "fields": [
            {"name": str(k), "value": str(v)[:1000], "inline": True}
            for k, v in (fields or {}).items()
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(webhook, json={"embeds": [embed]})
    except Exception:
        logging.getLogger(__name__).exception("Discord webhook failed")


def system_metrics() -> dict[str, Any]:
    process = psutil.Process(os.getpid())
    memory = process.memory_info()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": psutil.virtual_memory().percent,
        "process_rss_mb": round(memory.rss / 1024 / 1024, 2),
        "process_vms_mb": round(memory.vms / 1024 / 1024, 2),
    }


def export_rows_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def resolve_database_file(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.replace("sqlite:///", "", 1)
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path

