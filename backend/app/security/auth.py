"""
Operator authentication.

Signed session cookies over ``APP_SECRET_KEY``; Argon2id for password storage.
There are no payer accounts here -- a "user" is someone who operates this
benchmarking platform, and every one of them is an admin unless explicitly
demoted.

The cookie carries only the user id and an issue timestamp. It is signed, not
encrypted, and is checked against the database on every request, so
deactivating a user takes effect immediately rather than at cookie expiry.
"""

from __future__ import annotations

from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import User
from app.timeutil import utcnow

SESSION_COOKIE = "burapay_session"
#: Sessions expire after 12 hours. Long enough for a working day of sandbox
#: testing, short enough that a forgotten browser tab is not a standing key to
#: the credential store.
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60

_hasher = PasswordHasher()


class AuthNotConfigured(RuntimeError):
    """APP_SECRET_KEY is absent, so sessions cannot be signed."""


def _signer() -> TimestampSigner:
    secret = get_settings().APP_SECRET_KEY.strip()
    if not secret:
        raise AuthNotConfigured(
            "APP_SECRET_KEY is not set, so session cookies cannot be signed. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return TimestampSigner(secret, salt="burapay-session")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def issue_session(response: Response, user: User) -> None:
    token = _signer().sign(str(user.id)).decode()
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        # Secure whenever the public origin is https. Left off for a plain-http
        # local run, otherwise the browser silently drops the cookie and login
        # appears to succeed while every later request is anonymous.
        secure=settings.public_base_is_https,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    burapay_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> Optional[User]:
    """Resolve the signed-in operator, or ``None``. Never raises."""
    if not burapay_session:
        return None
    try:
        raw = _signer().unsign(burapay_session, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired, AuthNotConfigured):
        return None
    try:
        user_id = int(raw.decode())
    except (ValueError, AttributeError):
        return None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def require_user(
    user: Optional[User] = Depends(get_current_user),
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
        )
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required."
        )
    return user


async def touch_login(session: AsyncSession, user: User) -> None:
    user.last_login_at = utcnow()
    await session.flush()


async def find_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    result = await session.execute(
        select(User).where(User.email == email.strip().lower())
    )
    return result.scalar_one_or_none()


def client_ip(request: Request) -> str:
    """
    Best-effort client IP for audit logging.

    ``X-Forwarded-For`` is trusted only because this service is never exposed
    directly -- Caddy or Traefik always terminates in front of it and rewrites
    the header. It is used for logging only, never for authorisation.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
