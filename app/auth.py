"""Session-based authentication for the IT helpdesk.

Flow:
  POST /auth/login  → validates credentials → creates AuthSessionRecord
                    → sets HttpOnly cookie `session_token`
  GET  /auth/me     → reads cookie → looks up session → returns UserRead
  POST /auth/logout → deletes session → clears cookie

FastAPI dependency:
  get_current_user  → required auth (raises 401 if not logged in)
  get_optional_user → optional auth (returns None if not logged in)
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import (
    UserRecord,
    create_auth_session,
    create_user,
    delete_auth_session,
    get_auth_session,
    get_session,
    get_user_by_email,
    get_user_by_id,
    record_login,
)
from app.schemas import UserClearance, UserRead, UserRole

SESSION_COOKIE = "session_token"
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
TOKEN_BYTES = 32  # 256-bit opaque token


# ── Password helpers ───────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── Session helpers ────────────────────────────────────────────────────────────

def _new_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def _expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)


def _record_to_read(record: UserRecord) -> UserRead:
    return UserRead(
        id=record.id,
        email=record.email,
        display_name=record.display_name,
        role=UserRole(record.role),
        clearance=UserClearance(record.clearance),
        is_active=record.is_active,
        created_at=record.created_at,
        last_login_at=record.last_login_at,
    )


# ── Public auth functions ──────────────────────────────────────────────────────

def login(db: Session, email: str, password: str, *,
          user_agent: str = "", ip_address: str = "") -> tuple[UserRead, str]:
    """Validate credentials and create a session. Returns (UserRead, token)."""
    record = get_user_by_email(db, email)
    if not record or not record.is_active or not verify_password(password, record.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    token = _new_token()
    create_auth_session(db, record.id, token, _expires_at(),
                        user_agent=user_agent, ip_address=ip_address)
    record_login(db, record)
    return _record_to_read(record), token


def register(db: Session, email: str, display_name: str, password: str,
             role: str = UserRole.USER.value,
             clearance: str = UserClearance.PUBLIC.value) -> UserRead:
    """Create a new user. Raises 409 if email already exists."""
    existing = get_user_by_email(db, email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        )
    record = create_user(db, email, display_name, hash_password(password),
                         role=role, clearance=clearance)
    return _record_to_read(record)


def logout(db: Session, token: str) -> None:
    delete_auth_session(db, token)


def change_password(db: Session, user_id: int, current_password: str, new_password: str) -> None:
    """Change password for the authenticated user. Raises 401 if current password is wrong."""
    record = get_user_by_id(db, user_id)
    if not record or not verify_password(current_password, record.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    record.hashed_password = hash_password(new_password)
    db.commit()


def admin_reset_password(db: Session, email: str, new_password: str) -> None:
    """Admin-only: forcibly set a user's password by email."""
    record = get_user_by_email(db, email)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No user found with that email.",
        )
    record.hashed_password = hash_password(new_password)
    db.commit()


# ── FastAPI dependencies ───────────────────────────────────────────────────────

def _resolve_token(request: Request,
                   session_token: Optional[str] = Cookie(default=None)) -> str | None:
    # Also accept Bearer token in Authorization header for API clients.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.removeprefix("Bearer ").strip()
    return session_token


def get_optional_user(
    request: Request,
    db: Session = Depends(get_session),
    session_token: Optional[str] = Cookie(default=None),
) -> UserRead | None:
    token = _resolve_token(request, session_token)
    if not token:
        return None
    session = get_auth_session(db, token)
    if not session:
        return None
    record = get_user_by_id(db, session.user_id)
    if not record or not record.is_active:
        return None
    return _record_to_read(record)


def get_current_user(
    request: Request,
    db: Session = Depends(get_session),
    session_token: Optional[str] = Cookie(default=None),
) -> UserRead:
    user = get_optional_user(request, db, session_token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(*roles: UserRole):
    """Dependency factory — e.g. Depends(require_role(UserRole.ADMIN))."""
    def _check(current_user: UserRead = Depends(get_current_user)) -> UserRead:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to perform this action.",
            )
        return current_user
    return _check
