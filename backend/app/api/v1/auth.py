"""
Authentication routes (specification section 9).

The login endpoint answers with exactly one failure message whatever went wrong —
unknown handle, wrong password, disabled account, locked account — and spends
comparable time on each, so neither the body nor the clock reveals which. What
actually happened is recorded in the audit log, where only an administrator can read
it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import (CSRF_COOKIE, SESSION_COOKIE, create_access_token,
                               hash_password, new_csrf_token, use_secure_cookies,
                               verify_password)
from app.db.session import get_session
from app.models import AuditEvent, User
from app.schemas import LoginRequest, Message, PasswordChange, TokenResponse, UserOut
from app.services import audit
from app.services.auth import GENERIC_FAILURE, authenticate, login_limiter

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    """Install the session and CSRF cookies (section 9).

    ``HttpOnly`` on the session cookie keeps script from reading it, so an XSS bug
    cannot exfiltrate a session. ``Secure`` keeps it off plain HTTP wherever the
    deployment is HTTPS. ``SameSite=Lax`` is the strongest setting compatible with the
    flows this application needs: a gateway returning the browser from a hosted
    payment page is a top-level cross-site GET, and ``Strict`` would drop the session
    exactly there. Lax withholds the cookie from cross-site *writes*, and the
    double-submit CSRF token covers what remains.
    """
    max_age = settings.access_token_expire_minutes * 60
    secure = use_secure_cookies()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=secure, max_age=max_age, path="/")
    # Readable by our own script on purpose: that is what lets it be echoed back in a
    # header a cross-site page cannot forge.
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax",
                        secure=secure, max_age=max_age, path="/")


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, response: Response,
                session: AsyncSession = Depends(get_session)) -> TokenResponse:
    address = audit.client_address(request) or "unknown"

    allowed, retry_after = login_limiter.check(address)
    if not allowed:
        audit.record(session, AuditEvent.LOGIN_FAILED, request=request,
                     subject_label=payload.username[:255],
                     detail={"reason": "rate limited"})
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Try again shortly.",
            headers={"Retry-After": str(retry_after)})

    outcome = await authenticate(session, payload.username, payload.password)

    if not outcome.ok:
        login_limiter.hit(address)
        audit.record(session, AuditEvent.LOGIN_FAILED, request=request,
                     subject=outcome.user, subject_label=payload.username[:255],
                     detail={"reason": outcome.reason})
        if outcome.locked_now and outcome.user is not None:
            audit.record(session, AuditEvent.USER_LOCKED, request=request,
                         subject=outcome.user,
                         detail={"reason": "consecutive failed sign-in attempts",
                                 "attempts": outcome.user.failed_login_count,
                                 "unlocks_after_minutes": settings.login_lockout_minutes})
        # The failure accounting on the user row is part of the defence, so it is
        # committed even though the request failed.
        await session.commit()
        # No username, no address, no password — nothing here identifies the attempt
        # beyond the fact that one failed. The audit log holds the detail.
        logger.warning("failed login", extra={"operation": "login", "status": "denied"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=GENERIC_FAILURE)

    user = outcome.user
    assert user is not None                      # ok=True always carries the account
    login_limiter.clear(address)
    user.last_login_at = datetime.now(timezone.utc)
    user.last_login_ip = audit.client_address(request)

    token = create_access_token(user.id, role=user.role, email=user.email,
                                username=user.username)
    csrf = new_csrf_token()
    audit.record(session, AuditEvent.LOGIN_SUCCESS, request=request, subject=user,
                 performed_by=user, detail={"role": user.role})
    await session.commit()
    await session.refresh(user)

    _set_session_cookies(response, token, csrf)
    logger.info("login", extra={"operation": "login", "status": "ok",
                                "user_id": user.id, "role": user.role})
    return TokenResponse(access_token=token,
                         expires_in=settings.access_token_expire_minutes * 60,
                         csrf_token=csrf,
                         user=UserOut.model_validate(user))


@router.post("/logout", response_model=Message)
async def logout(request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)) -> Message:
    """End the session.

    Deliberately not behind ``get_current_user``: signing out must work even when the
    token has already expired or the account has just been disabled, and a sign-out
    that fails leaves a cookie in the browser. The audit row is written when the
    caller can still be identified, and skipped when it cannot.
    """
    from jwt import PyJWTError

    from app.core.security import decode_access_token

    header = request.headers.get("authorization", "")
    token = (header.split(" ", 1)[1] if header.lower().startswith("bearer ")
             else request.cookies.get(SESSION_COOKIE))
    if token:
        try:
            payload = decode_access_token(token)
        except PyJWTError:
            payload = {}
        user = await session.get(User, payload.get("sub", "")) if payload else None
        if user is not None:
            audit.record(session, AuditEvent.LOGOUT, request=request, subject=user,
                         performed_by=user)
            await session.commit()

    _clear_session_cookies(response)
    return Message(message="Signed out.")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/change-password", response_model=Message)
async def change_password(payload: PasswordChange, request: Request,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)) -> Message:
    """Change your own password. The current one is required; neither is ever logged."""
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The current password is incorrect.")
    user.hashed_password = hash_password(payload.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    audit.record(session, AuditEvent.USER_PASSWORD_CHANGED, request=request,
                 subject=user, performed_by=user)
    await session.commit()
    return Message(message="Password changed.")
