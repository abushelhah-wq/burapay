"""Operator login/logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.logging_setup import get_logger
from app.models import User
from app.schemas.settings import LoginRequest, UserOut
from app.security.auth import (
    clear_session, client_ip, find_user_by_email, get_current_user, hash_password,
    issue_session, needs_rehash, require_user, touch_login, verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger(__name__)


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await find_user_by_email(session, payload.email)

    # One message for both "no such user" and "wrong password", so the endpoint
    # cannot be used to enumerate which operator accounts exist.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password.",
    )
    if user is None or not user.is_active:
        # Still spend the time hashing, so a missing user is not distinguishable
        # from a wrong password by response time.
        hash_password(payload.password)
        raise invalid
    if not verify_password(user.password_hash, payload.password):
        logger.info(
            "failed login", extra={"email": payload.email, "ip": client_ip(request)}
        )
        raise invalid

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    await touch_login(session, user)
    issue_session(response, user)
    logger.info("login", extra={"user_id": user.id, "ip": client_ip(request)})
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    clear_session(response)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(require_user)) -> User:
    return user


@router.get("/session")
async def session_state(user: User | None = Depends(get_current_user)) -> dict[str, object]:
    """Unauthenticated probe so the UI can decide whether to show a login form."""
    return {
        "authenticated": user is not None,
        "email": user.email if user else None,
        "is_admin": bool(user and user.is_admin),
    }
