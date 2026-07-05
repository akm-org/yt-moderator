import logging
import shutil
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from app.auth import (
    authenticate_admin,
    create_login_user,
    create_or_update_initial_admin,
    current_admin_optional,
    ensure_csrf_token,
    login_admin,
    login_limiter,
    logout_admin,
    require_admin,
    require_super_admin,
    update_login_user_password,
    validate_csrf,
)
from app.config import BASE_DIR, Settings, get_settings
from app.database import engine, get_db, init_db
from app.models import (
    AdminUser,
    ApiUsage,
    AppLog,
    AuditLog,
    ChatMessage,
    ChatUser,
    ModerationAction,
    RuntimeSetting,
)
from app.moderation import (
    DEFAULT_RUNTIME_SETTINGS,
    ModerationService,
    ensure_runtime_settings,
    get_bot_state,
    get_runtime_settings,
    messages_per_minute,
    recent_actions,
    recent_app_logs,
    set_bot_state,
    set_runtime_setting,
)
from app.scheduler import LiveChatWorker
from app.utils import (
    audit_event,
    compact_json,
    configure_logging,
    export_rows_csv,
    log_event,
    notify_discord,
    parse_textarea_list,
    render_textarea_list,
    resolve_database_file,
    system_metrics,
)
from app.websocket import ConnectionManager
from app.youtube import YouTubeClient, YouTubeNotConfigured


settings = get_settings()
configure_logging(settings)
LOGGER = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["dt"] = lambda value: format_datetime(value)
templates.env.filters["json"] = lambda value: compact_json(value)
templates.env.filters["textarea"] = lambda value: render_textarea_list(value)

manager = ConnectionManager()
moderation_service = ModerationService(settings)


class RequestRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


request_limiter = RequestRateLimiter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = next(get_db())
    try:
        ensure_runtime_settings(db)
        create_or_update_initial_admin(db, settings.admin_username, settings.admin_password)
        log_event(db, "INFO", "startup", "Application startup complete", {"env": settings.app_env})
        set_bot_state(db, "started_at", datetime.now(timezone.utc).isoformat())
        db.commit()
        if settings.admin_password == "change-me-now":
            LOGGER.warning("ADMIN_PASSWORD is using the local default. Change it before deployment.")
    finally:
        db.close()

    app.state.started_at = datetime.now(timezone.utc)
    app.state.worker = LiveChatWorker(settings, manager, moderation_service)
    if settings.worker_enabled:
        await app.state.worker.start()
    await notify_discord(settings.discord_webhook, "Moderator service started", settings.app_name)
    try:
        yield
    finally:
        worker = getattr(app.state, "worker", None)
        if worker:
            await worker.stop()
        db = next(get_db())
        try:
            log_event(db, "INFO", "shutdown", "Application shutdown complete", {})
            db.commit()
        finally:
            db.close()
        await notify_discord(settings.discord_webhook, "Moderator service stopped", settings.app_name)


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

if settings.allowed_hosts.strip() != "*":
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[host.strip() for host in settings.allowed_hosts.split(",") if host.strip()],
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie_name,
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)
    key = request.client.host if request.client else "unknown"
    if not request_limiter.allowed(key, settings.rate_limit_per_minute):
        return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    return await call_next(request)


def format_datetime(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def template_context(
    request: Request,
    db: Session,
    admin: AdminUser | None,
    **extra: Any,
) -> dict[str, Any]:
    runtime = get_runtime_settings(db)
    connection = get_bot_state(db, "connection", {"connected": False, "message": "Not connected"})
    connected_channel = get_connected_channel(db)
    if connected_channel:
        connection = dict(connection or {})
        connection["channel"] = connected_channel
    context = {
        "request": request,
        "app_name": settings.app_name,
        "admin": admin,
        "csrf_token": ensure_csrf_token(request),
        "runtime": runtime,
        "connection": connection,
        "settings": settings,
        "now": datetime.now(timezone.utc),
    }
    context.update(extra)
    return context


def user_refresh_token(user: AdminUser | None, db: Session) -> str | None:
    if user and user.youtube_refresh_token:
        return user.youtube_refresh_token
    return get_bot_state(db, "youtube_refresh_token") or settings.google_refresh_token or None


def user_channel(user: AdminUser | None, db: Session) -> dict[str, Any] | None:
    if user and user.youtube_channel:
        return user.youtube_channel
    return get_bot_state(db, "youtube_channel")


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def get_refresh_token(db: Session) -> str | None:
    return get_bot_state(db, "youtube_refresh_token") or settings.google_refresh_token or None


def get_connected_channel(db: Session) -> dict[str, Any] | None:
    return get_bot_state(db, "youtube_channel")


def sync_global_youtube_from_user(db: Session, user: AdminUser, channel: dict[str, Any] | None) -> None:
    if user.youtube_refresh_token:
        set_bot_state(db, "youtube_refresh_token", user.youtube_refresh_token)
    if channel:
        set_bot_state(db, "youtube_channel", channel)
        if channel.get("id"):
            set_bot_state(db, "youtube_channel_id", channel["id"])
    set_bot_state(db, "active_account_id", user.id)


def stats_payload(request: Request, db: Session) -> dict[str, Any]:
    actions = {
        name: db.query(ModerationAction).filter(ModerationAction.action == name).count()
        for name in ["warn", "delete", "timeout", "ban"]
    }
    total_messages = db.query(ChatMessage).count()
    spam_detected = db.query(ChatMessage).filter(ChatMessage.spam_score >= 20).count()
    gemini_requests = db.query(ApiUsage).filter(ApiUsage.provider == "gemini").count()
    youtube_requests = db.query(ApiUsage).filter(ApiUsage.provider == "youtube").count()
    started_at = getattr(request.app.state, "started_at", datetime.now(timezone.utc))
    uptime_seconds = int((datetime.now(timezone.utc) - started_at).total_seconds())
    minute_rows = (
        db.query(ChatMessage.received_at)
        .filter(ChatMessage.received_at >= datetime.now(timezone.utc) - timedelta(hours=2))
        .all()
    )
    minute_buckets: dict[datetime, int] = {}
    for (received_at,) in minute_rows:
        bucket = normalize_datetime(received_at).replace(second=0, microsecond=0)
        minute_buckets[bucket] = minute_buckets.get(bucket, 0) + 1
    return {
        "messages_total": total_messages,
        "messages_per_minute": messages_per_minute(db),
        "spam_detected": spam_detected,
        "warnings": actions["warn"],
        "deletes": actions["delete"],
        "timeouts": actions["timeout"],
        "bans": actions["ban"],
        "gemini_requests": gemini_requests,
        "api_usage": gemini_requests + youtube_requests,
        "youtube_requests": youtube_requests,
        "system": system_metrics(),
        "uptime_seconds": uptime_seconds,
        "uptime": human_duration(uptime_seconds),
        "connection": get_bot_state(db, "connection", {"connected": False, "message": "Not connected"}),
        "chart": {
            "labels": [bucket.astimezone().strftime("%H:%M") for bucket in sorted(minute_buckets)],
            "messages": [minute_buckets[bucket] for bucket in sorted(minute_buckets)],
        },
    }


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def human_duration(seconds: int) -> str:
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


@app.get("/", response_class=HTMLResponse)
async def root(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser | None, Depends(current_admin_optional)],
):
    if admin:
        return redirect("/dashboard")
    return templates.TemplateResponse(request, "index.html", template_context(request, db, None))


@app.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser | None, Depends(current_admin_optional)],
):
    if admin:
        return redirect("/dashboard")
    return templates.TemplateResponse(request, "login.html", template_context(request, db, None, error=None))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Annotated[Session, Depends(get_db)],
):
    await validate_csrf(request)
    client_key = request.client.host if request.client else "unknown"
    if not login_limiter.check(client_key, settings.login_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    admin = authenticate_admin(db, username, password)
    if not admin:
        audit_event(
            db,
            admin_id=None,
            username=username,
            action="login_failed",
            ip_address=client_key,
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(request, db, None, error="Invalid username or password"),
            status_code=401,
        )
    login_admin(request, db, admin)
    return redirect("/dashboard")


@app.post("/logout")
async def logout(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    await validate_csrf(request)
    logout_admin(request, db, admin)
    return redirect("/login")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    stats = stats_payload(request, db)
    messages = db.query(ChatMessage).order_by(desc(ChatMessage.received_at)).limit(25).all()
    actions = recent_actions(db, 12)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        template_context(request, db, admin, stats=stats, messages=messages, actions=actions),
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    messages = db.query(ChatMessage).order_by(desc(ChatMessage.received_at)).limit(100).all()
    return templates.TemplateResponse(
        request,
        "chat.html",
        template_context(request, db, admin, messages=list(reversed(messages))),
    )


@app.get("/moderation", response_class=HTMLResponse)
async def moderation_queue(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    queue = (
        db.query(ChatMessage)
        .filter(ChatMessage.final_action.in_(["warn", "delete", "timeout", "ban"]))
        .order_by(desc(ChatMessage.received_at))
        .limit(100)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "moderation_queue.html",
        template_context(request, db, admin, queue=queue),
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
    q: str | None = Query(default=None),
):
    logs = recent_app_logs(db, 250, search=q)
    audits = db.query(AuditLog).order_by(desc(AuditLog.created_at)).limit(50).all()
    return templates.TemplateResponse(
        request,
        "logs.html",
        template_context(request, db, admin, logs=logs, audits=audits, q=q or ""),
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
    saved: bool = Query(default=False),
):
    return templates.TemplateResponse(
        request,
        "settings.html",
        template_context(request, db, admin, saved=saved, defaults=DEFAULT_RUNTIME_SETTINGS),
    )


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_super_admin)],
):
    accounts = db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        template_context(request, db, admin, accounts=accounts, error=None),
    )


@app.post("/accounts")
async def accounts_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_super_admin)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "user",
):
    await validate_csrf(request)
    try:
        account = create_login_user(db, username=username, password=password, role=role)
        audit_event(
            db,
            admin.id,
            admin.username,
            "account_create",
            request.client.host if request.client else None,
            request.headers.get("user-agent"),
            {"created_user": account.username, "role": account.role},
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        accounts = db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
        return templates.TemplateResponse(
            request,
            "accounts.html",
            template_context(request, db, admin, accounts=accounts, error=str(exc)),
            status_code=400,
        )
    return redirect("/accounts")


@app.post("/accounts/{account_id}")
async def accounts_update(
    request: Request,
    account_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_super_admin)],
):
    await validate_csrf(request)
    account = db.get(AdminUser, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    form = await request.form()
    action = str(form.get("action", ""))
    if action == "toggle":
        if account.id == admin.id:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        account.is_active = not account.is_active
    elif action == "role":
        role = str(form.get("role", "user"))
        if role not in {"admin", "user"}:
            raise HTTPException(status_code=400, detail="Invalid role")
        account.role = role
    elif action == "password":
        update_login_user_password(db, account, str(form.get("password", "")))
    elif action == "clear_youtube":
        account.youtube_refresh_token = None
        account.youtube_channel_id = None
        account.youtube_channel = {}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    db.add(account)
    audit_event(
        db,
        admin.id,
        admin.username,
        "account_update",
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
        {"target": account.username, "action": action},
    )
    db.commit()
    return redirect("/accounts")


@app.get("/my-youtube", response_class=HTMLResponse)
async def my_youtube_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    return templates.TemplateResponse(
        request,
        "my_youtube.html",
        template_context(request, db, admin, channel=user_channel(admin, db)),
    )


@app.post("/settings")
async def settings_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    await validate_csrf(request)
    form = await request.form()
    numeric_float = ["spam_sensitivity", "caps_limit"]
    numeric_int = ["warning_limit", "timeout_duration", "ban_duration", "emoji_limit", "ai_min_score", "delete_threshold", "timeout_threshold", "slow_mode_suggestion_threshold"]
    toggles = ["ai_enabled", "profanity_filter", "link_filter", "raid_protection"]
    lists = ["whitelist", "blacklist", "temporary_whitelist", "keyword_alerts"]

    for key in numeric_float:
        set_runtime_setting(db, key, float(form.get(key, DEFAULT_RUNTIME_SETTINGS[key])))
    for key in numeric_int:
        set_runtime_setting(db, key, int(form.get(key, DEFAULT_RUNTIME_SETTINGS[key])))
    for key in toggles:
        set_runtime_setting(db, key, key in form)
    for key in lists:
        set_runtime_setting(db, key, parse_textarea_list(str(form.get(key, ""))))

    audit_event(
        db,
        admin.id,
        admin.username,
        "settings_update",
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
    )
    log_event(db, "INFO", "settings_update", f"{admin.username} updated runtime settings", {})
    db.commit()
    return redirect("/settings?saved=true")


@app.post("/moderate")
async def moderate(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    await validate_csrf(request)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)
    message_id = int(payload.get("message_id"))
    action = str(payload.get("action", "allow")).lower()
    reason = str(payload.get("reason", f"Manual action by {admin.username}"))
    refresh_token = get_refresh_token(db)
    youtube_client = YouTubeClient(settings, refresh_token=refresh_token) if refresh_token else None
    row = await moderation_service.manual_action(
        db,
        message_id=message_id,
        action=action,
        reason=reason,
        youtube_client=youtube_client,
        admin_username=admin.username,
    )
    await manager.broadcast(
        "action",
        {
            "id": row.id,
            "message_id": row.message_id,
            "username": row.username,
            "action": row.action,
            "reason": row.reason,
            "executed": row.executed,
        },
    )
    return JSONResponse({"ok": True, "action_id": row.id, "executed": row.executed})


@app.get("/statistics", response_class=HTMLResponse)
async def statistics_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    stats = stats_payload(request, db)
    category_rows = (
        db.query(ChatMessage.ai_categories)
        .filter(ChatMessage.ai_categories.is_not(None))
        .order_by(desc(ChatMessage.received_at))
        .limit(1000)
        .all()
    )
    category_counts: dict[str, int] = {}
    for (categories,) in category_rows:
        for category in categories or []:
            category_counts[category] = category_counts.get(category, 0) + 1
    action_counts = {
        action: db.query(ModerationAction).filter(ModerationAction.action == action).count()
        for action in ["allow", "warn", "delete", "timeout", "ban"]
    }
    return templates.TemplateResponse(
        request,
        "statistics.html",
        template_context(
            request,
            db,
            admin,
            stats=stats,
            category_counts=category_counts,
            action_counts=action_counts,
        ),
    )


@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
    q: str | None = Query(default=None),
):
    query = db.query(ChatUser)
    if q:
        like = f"%{q}%"
        query = query.filter((ChatUser.username.ilike(like)) | (ChatUser.channel_id.ilike(like)))
    users = query.order_by(desc(ChatUser.last_seen)).limit(200).all()
    return templates.TemplateResponse(
        request,
        "users.html",
        template_context(request, db, admin, users=users, q=q or ""),
    )


@app.get("/users/{channel_id}", response_class=HTMLResponse)
async def user_profile(
    request: Request,
    channel_id: str,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    user = db.query(ChatUser).filter(ChatUser.channel_id == channel_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.author_channel_id == channel_id)
        .order_by(desc(ChatMessage.received_at))
        .limit(200)
        .all()
    )
    actions = (
        db.query(ModerationAction)
        .filter(ModerationAction.user_channel_id == channel_id)
        .order_by(desc(ModerationAction.created_at))
        .limit(100)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "user_profile.html",
        template_context(request, db, admin, user=user, messages=messages, actions=actions),
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    top_users = (
        db.query(ChatUser)
        .order_by(desc(ChatUser.warnings + ChatUser.timeouts + ChatUser.bans), desc(ChatUser.last_spam_score))
        .limit(25)
        .all()
    )
    hourly_rows = (
        db.query(ChatMessage.received_at, ChatMessage.spam_score)
        .filter(ChatMessage.received_at >= datetime.now(timezone.utc) - timedelta(hours=24))
        .all()
    )
    hourly_buckets: dict[datetime, dict[str, float]] = {}
    for received_at, spam_score in hourly_rows:
        bucket = normalize_datetime(received_at).replace(minute=0, second=0, microsecond=0)
        entry = hourly_buckets.setdefault(bucket, {"count": 0, "spam_sum": 0})
        entry["count"] += 1
        entry["spam_sum"] += float(spam_score or 0)
    hourly = [
        (
            bucket.astimezone().strftime("%H:00"),
            int(data["count"]),
            round(data["spam_sum"] / data["count"], 1) if data["count"] else 0,
        )
        for bucket, data in sorted(hourly_buckets.items())
    ]
    return templates.TemplateResponse(
        request,
        "analytics.html",
        template_context(request, db, admin, top_users=top_users, hourly=hourly),
    )


@app.get("/health")
async def health(db: Annotated[Session, Depends(get_db)]):
    try:
        db.query(ChatMessage).count()
        database_ok = True
    except Exception:
        database_ok = False
    refresh_token = get_refresh_token(db)
    youtube = YouTubeClient(settings, refresh_token=refresh_token)
    youtube_health = await youtube.api_health()
    return {
        "ok": database_ok,
        "database": database_ok,
        "youtube": youtube_health,
        "channel": get_connected_channel(db),
        "gemini": {"configured": bool(settings.gemini_api_key), "model": settings.gemini_model},
        "worker_running": getattr(app.state, "worker", None).running if hasattr(app.state, "worker") else False,
    }


@app.get("/stats")
async def stats_api(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    return stats_payload(request, db)


@app.websocket("/ws/livechat")
async def websocket_livechat(websocket: WebSocket):
    session = websocket.scope.get("session") or {}
    if not session.get("admin_id"):
        await websocket.close(code=4401)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)


@app.get("/auth/youtube/start")
async def youtube_auth_start(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    client = YouTubeClient(settings, refresh_token=user_refresh_token(admin, db))
    try:
        url, state = client.build_auth_url()
    except YouTubeNotConfigured as exc:
        log_event(db, "ERROR", "youtube_oauth_start_failed", str(exc), {})
        db.commit()
        return redirect("/settings?oauth=missing")
    request.session["youtube_oauth_state"] = state
    request.session["youtube_oauth_account_id"] = admin.id
    return RedirectResponse(url, status_code=302)


@app.get("/auth/login", response_class=HTMLResponse)
async def youtube_friend_login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: str | None = Query(default=None),
):
    if not settings.youtube_setup_token:
        raise HTTPException(
            status_code=403,
            detail="YOUTUBE_SETUP_TOKEN is not configured. Set it before using friend OAuth.",
        )
    if token != settings.youtube_setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")

    client = YouTubeClient(settings, refresh_token=get_refresh_token(db))
    try:
        url, state = client.build_auth_url(callback_path="/auth/callback")
    except YouTubeNotConfigured as exc:
        log_event(db, "ERROR", "youtube_friend_oauth_start_failed", str(exc), {})
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    request.session["youtube_friend_oauth_state"] = state
    request.session["youtube_friend_setup_token"] = token
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback", response_class=HTMLResponse)
async def youtube_friend_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    expected_state = request.session.get("youtube_friend_oauth_state")
    setup_token = request.session.get("youtube_friend_setup_token")
    if error:
        log_event(db, "ERROR", "youtube_friend_oauth_error", error, {})
        db.commit()
        return templates.TemplateResponse(
            request,
            "youtube_connected.html",
            template_context(request, db, None, success=False, error=error, channel=None),
            status_code=400,
        )
    if not setup_token or setup_token != settings.youtube_setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup session")
    if not code or not state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")

    client = YouTubeClient(settings, refresh_token=get_refresh_token(db))
    token = await client.exchange_code(code, callback_path="/auth/callback")
    guest_account = None
    if request.session.get("admin_id"):
        guest_account = db.get(AdminUser, int(request.session["admin_id"]))
    if token.get("refresh_token"):
        set_bot_state(db, "youtube_refresh_token", token["refresh_token"])
        if guest_account:
            guest_account.youtube_refresh_token = token["refresh_token"]
    channel = await client.get_my_channel()
    if channel:
        set_bot_state(db, "youtube_channel", channel)
        if channel.get("id"):
            set_bot_state(db, "youtube_channel_id", channel["id"])
        if guest_account:
            guest_account.youtube_channel = channel
            guest_account.youtube_channel_id = channel.get("id")
            db.add(guest_account)
    set_bot_state(db, "connection", {"connected": False, "message": "YouTube OAuth connected; waiting for live stream"})
    log_event(
        db,
        "INFO",
        "youtube_friend_oauth_connected",
        "Friend connected YouTube channel",
        {"channel": channel},
    )
    db.commit()
    request.session.pop("youtube_friend_oauth_state", None)
    request.session.pop("youtube_friend_setup_token", None)
    return templates.TemplateResponse(
        request,
        "youtube_connected.html",
        template_context(request, db, None, success=True, error=None, channel=channel),
    )


@app.get("/auth/youtube/callback")
async def youtube_auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    expected_state = request.session.get("youtube_oauth_state")
    account_id = request.session.get("youtube_oauth_account_id") or request.session.get("admin_id")
    if error:
        log_event(db, "ERROR", "youtube_oauth_error", error, {})
        db.commit()
        return redirect("/my-youtube?oauth=error")
    if not code or not state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")
    account = db.get(AdminUser, int(account_id)) if account_id else None
    if not account:
        raise HTTPException(status_code=400, detail="OAuth account session expired")
    client = YouTubeClient(settings, refresh_token=user_refresh_token(account, db))
    token = await client.exchange_code(code)
    if token.get("refresh_token"):
        account.youtube_refresh_token = token["refresh_token"]
    channel = await client.get_my_channel()
    if channel:
        account.youtube_channel = channel
        account.youtube_channel_id = channel.get("id")
    db.add(account)
    sync_global_youtube_from_user(db, account, channel)
    log_event(db, "INFO", "youtube_oauth_connected", "YouTube OAuth connected", {})
    db.commit()
    request.session.pop("youtube_oauth_state", None)
    request.session.pop("youtube_oauth_account_id", None)
    return redirect("/my-youtube?oauth=connected")


@app.get("/logs/export.csv")
async def export_logs(
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    logs = db.query(AppLog).order_by(desc(AppLog.created_at)).limit(10_000).all()
    rows = [
        {
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "level": row.level,
            "event": row.event,
            "message": row.message,
            "context": compact_json(row.context),
        }
        for row in logs
    ]
    data = export_rows_csv(rows)
    return Response(
        data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=moderator-logs.csv"},
    )


@app.get("/backup")
async def backup_database(
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    db_file = resolve_database_file(settings.database_url)
    if not db_file or not db_file.exists():
        raise HTTPException(status_code=400, detail="Backups are only available for SQLite databases")
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup_file = backup_dir / f"moderator-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(db_file, backup_file)
    return FileResponse(
        str(backup_file),
        filename=backup_file.name,
        media_type="application/octet-stream",
    )


@app.post("/restore")
async def restore_database(
    request: Request,
    upload: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[AdminUser, Depends(require_admin)],
):
    await validate_csrf(request)
    db_file = resolve_database_file(settings.database_url)
    if not db_file:
        raise HTTPException(status_code=400, detail="Restore is only available for SQLite databases")
    temp_file = BASE_DIR / "backups" / "restore-upload.tmp"
    temp_file.parent.mkdir(exist_ok=True)
    temp_file.write_bytes(await upload.read())
    try:
        probe = sqlite3.connect(temp_file)
        probe.execute("PRAGMA integrity_check").fetchone()
        probe.close()
    except Exception as exc:
        temp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Invalid SQLite database: {exc}") from exc

    backup_file = BASE_DIR / "backups" / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    if db_file.exists():
        shutil.copy2(db_file, backup_file)
    db.close()
    engine.dispose()
    shutil.copy2(temp_file, db_file)
    temp_file.unlink(missing_ok=True)
    init_db()
    return redirect("/settings?restored=true")
