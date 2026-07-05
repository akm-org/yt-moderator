import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminUser, utcnow
from app.utils import audit_event, hash_password, verify_password


class LoginRateLimiter:
    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        attempts = self._attempts[key]
        while attempts and now - attempts[0] > window_seconds:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        return True


login_limiter = LoginRateLimiter()


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def validate_csrf(request: Request) -> None:
    expected = request.session.get("csrf_token")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        try:
            form = await request.form()
            supplied = str(form.get("csrf_token", ""))
        except Exception:
            supplied = ""
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def create_or_update_initial_admin(db: Session, username: str, password: str) -> AdminUser:
    admin = db.query(AdminUser).filter(AdminUser.username == username).first()
    if admin:
        if admin.role != "admin":
            admin.role = "admin"
            db.add(admin)
            db.commit()
        return admin
    admin = AdminUser(username=username, password_hash=hash_password(password), role="admin", is_active=True)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def authenticate_admin(db: Session, username: str, password: str) -> AdminUser | None:
    admin = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not admin or not admin.is_active:
        return None
    if not verify_password(password, admin.password_hash):
        return None
    admin.last_login = utcnow()
    db.add(admin)
    db.commit()
    return admin


def login_admin(request: Request, db: Session, admin: AdminUser) -> None:
    request.session["admin_id"] = admin.id
    request.session["admin_username"] = admin.username
    request.session["admin_role"] = admin.role
    request.session["login_at"] = utcnow().isoformat()
    ensure_csrf_token(request)
    audit_event(
        db,
        admin_id=admin.id,
        username=admin.username,
        action="login",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()


def logout_admin(request: Request, db: Session, admin: AdminUser | None) -> None:
    if admin:
        audit_event(
            db,
            admin_id=admin.id,
            username=admin.username,
            action="logout",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
    request.session.clear()


def current_admin_optional(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> AdminUser | None:
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    admin = db.get(AdminUser, int(admin_id))
    if not admin or not admin.is_active:
        request.session.clear()
        return None
    return admin


def require_admin(
    request: Request,
    admin: Annotated[AdminUser | None, Depends(current_admin_optional)],
) -> AdminUser:
    if not admin:
        accepts_html = "text/html" in request.headers.get("accept", "")
        if accepts_html:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return admin


def require_super_admin(
    admin: Annotated[AdminUser, Depends(require_admin)],
) -> AdminUser:
    if admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return admin


def create_login_user(db: Session, *, username: str, password: str, role: str = "user") -> AdminUser:
    username = username.strip()
    if not username:
        raise ValueError("Username is required")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if role not in {"admin", "user"}:
        raise ValueError("Invalid role")
    existing = db.query(AdminUser).filter(AdminUser.username == username).first()
    if existing:
        raise ValueError("Username already exists")
    row = AdminUser(
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def update_login_user_password(db: Session, user: AdminUser, password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    user.password_hash = hash_password(password)
    db.add(user)
