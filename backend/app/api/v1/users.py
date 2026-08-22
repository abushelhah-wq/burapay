"""
User management (specification section 10).

Administrator-only, enforced by ``require_admin`` on every route in this module — the
front end also hides the section, but that is presentation, not permission. Every
mutation writes an audit row in the same transaction as the change, so the log cannot
drift from the data.

Accounts are never deleted. Disabling sets ``INACTIVE``, which preserves the audit
history and the transaction ownership that point at the row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import get_session
from app.models import AuditEvent, User, UserRole, UserStatus
from app.schemas import (AuditLogOut, Message, Page, PasswordReset, UserCreate,
                         UserOut, UserRoleOut, UserUpdate)
from app.services import audit

router = APIRouter(prefix="/users", tags=["users"])
logger = get_logger(__name__)

#: What each role may do, shown next to the choice on the Create User form so the
#: decision is made with the consequences visible.
ROLE_PERMISSIONS: Dict[str, List[str]] = {
    UserRole.ADMIN.value: [
        "Create, edit and disable users", "Reset user passwords",
        "View gateway configuration", "Create and update gateway credentials",
        "Run payment tests", "Refund, capture and void transactions",
        "Run CIT and MIT payments", "View transactions, tokens and logs",
        "View benchmark results and webhook events", "Export reports",
        "Change system configuration",
    ],
    UserRole.USER.value: [
        "Sign in", "Run payment tests", "View transactions",
        "Perform permitted transaction operations", "View tokens",
        "Run CIT and MIT where permitted", "View logs",
        "View benchmark results", "Export reports",
    ],
}


async def _with_creator(session: AsyncSession, users: List[User]) -> List[UserOut]:
    """Attach each account's creator username in one extra query, not N."""
    creator_ids = {user.created_by_user_id for user in users if user.created_by_user_id}
    names: Dict[str, str] = {}
    if creator_ids:
        rows = await session.execute(
            select(User.id, User.username).where(User.id.in_(creator_ids)))
        names = {row[0]: row[1] for row in rows}
    out = []
    for user in users:
        item = UserOut.model_validate(user)
        item.created_by_username = names.get(user.created_by_user_id or "")
        out.append(item)
    return out


async def _get_or_404(session: AsyncSession, user_id: str) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such user.")
    return user


@router.get("/roles", response_model=List[UserRoleOut])
async def list_roles(_: User = Depends(require_admin)) -> List[UserRoleOut]:
    return [UserRoleOut(value=role.value, label=role.value.title(),
                        permissions=ROLE_PERMISSIONS[role.value])
            for role in UserRole]


@router.get("", response_model=Page[UserOut])
async def list_users(
        search: Optional[str] = Query(
            None, description="Matches name, username or email."),
        name: Optional[str] = None,
        username: Optional[str] = None,
        email: Optional[str] = None,
        role: Optional[str] = None,
        status_filter: Optional[str] = Query(None, alias="status"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        _: User = Depends(require_admin),
        session: AsyncSession = Depends(get_session)) -> Page[UserOut]:
    """The Users screen (section 10), with the documented filters."""
    query = select(User)
    if search:
        needle = f"%{search.strip().lower()}%"
        query = query.where(or_(func.lower(User.full_name).like(needle),
                                func.lower(User.username).like(needle),
                                func.lower(User.email).like(needle)))
    if name:
        query = query.where(func.lower(User.full_name).like(f"%{name.strip().lower()}%"))
    if username:
        query = query.where(func.lower(User.username).like(f"%{username.strip().lower()}%"))
    if email:
        query = query.where(func.lower(User.email).like(f"%{email.strip().lower()}%"))
    if role:
        query = query.where(User.role == role.strip().upper())
    if status_filter:
        query = query.where(User.status == status_filter.strip().upper())

    total = (await session.execute(
        select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (await session.execute(
        query.order_by(User.created_at.desc()).limit(limit).offset(offset))).scalars()
    return Page(items=await _with_creator(session, list(rows)), total=total,
                limit=limit, offset=offset)


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, request: Request,
                      admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)) -> UserOut:
    """Create an account (section 10).

    Uniqueness is checked on both handles before the insert so the caller gets a clear
    message rather than a constraint violation; the unique indexes remain the actual
    guarantee.
    """
    username = payload.username.strip().lower()
    email = str(payload.email).strip().lower()

    clash = (await session.execute(
        select(User).where(or_(func.lower(User.username) == username,
                               func.lower(User.email) == email)))).scalars().first()
    if clash is not None:
        field = "username" if clash.username.lower() == username else "email address"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"That {field} is already in use.")

    user = User(username=username, email=email,
                full_name=(payload.full_name or "").strip() or None,
                hashed_password=hash_password(payload.password),
                role=payload.role, status=payload.status,
                password_changed_at=datetime.now(timezone.utc),
                created_by_user_id=admin.id)
    session.add(user)
    await session.flush()
    audit.record(session, AuditEvent.USER_CREATED, request=request, subject=user,
                 performed_by=admin,
                 detail={"username": user.username, "email": user.email,
                         "role": user.role, "status": user.status})
    await session.commit()
    await session.refresh(user)
    logger.info("user created", extra={"operation": "user_create", "status": "ok",
                                       "user_id": user.id, "role": user.role})
    return (await _with_creator(session, [user]))[0]


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: str, _: User = Depends(require_admin),
                   session: AsyncSession = Depends(get_session)) -> UserOut:
    user = await _get_or_404(session, user_id)
    return (await _with_creator(session, [user]))[0]


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: str, payload: UserUpdate, request: Request,
                      admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)) -> UserOut:
    """Edit name, email, role or status (section 10). Every change is audited."""
    user = await _get_or_404(session, user_id)
    before = {"full_name": user.full_name, "email": user.email, "role": user.role,
              "status": user.status}

    if payload.email is not None:
        email = str(payload.email).strip().lower()
        clash = (await session.execute(
            select(User).where(func.lower(User.email) == email,
                               User.id != user.id))).scalars().first()
        if clash is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="That email address is already in use.")
        user.email = email
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip() or None
    if payload.role is not None:
        _guard_last_admin(user, admin, new_role=payload.role, new_status=payload.status)
        user.role = payload.role
    if payload.status is not None:
        _guard_last_admin(user, admin, new_role=payload.role, new_status=payload.status)
        user.status = payload.status
        if payload.status == UserStatus.ACTIVE.value:
            # Re-enabling clears a brute-force lockout: that is the administrator's
            # way of unlocking an account without waiting out the window.
            user.failed_login_count = 0
            user.locked_at = None

    after = {"full_name": user.full_name, "email": user.email, "role": user.role,
             "status": user.status}
    delta = audit.changes(before, after, ("full_name", "email", "role", "status"))
    if delta:
        audit.record(session, AuditEvent.USER_UPDATED, request=request, subject=user,
                     performed_by=admin, detail={"changes": delta})
        # Role and status changes get their own events as well as the USER_UPDATED
        # row, because section 10 lists them separately and they are what an auditor
        # filters for.
        if "role" in delta:
            audit.record(session, AuditEvent.USER_ROLE_CHANGED, request=request,
                         subject=user, performed_by=admin, detail=delta["role"])
        if "status" in delta:
            event = (AuditEvent.USER_ENABLED if after["status"] == UserStatus.ACTIVE.value
                     else AuditEvent.USER_DISABLED)
            audit.record(session, event, request=request, subject=user,
                         performed_by=admin, detail=delta["status"])
    await session.commit()
    await session.refresh(user)
    return (await _with_creator(session, [user]))[0]


def _guard_last_admin(user: User, admin: User, *, new_role: Optional[str],
                      new_status: Optional[str]) -> None:
    """Stop an administrator locking themselves out of their own deployment.

    Only self-demotion and self-disabling are blocked. One administrator demoting
    another is a legitimate action, and refusing it would need a "how many admins are
    left" count that races with any concurrent change anyway.
    """
    if user.id != admin.id:
        return
    if new_role is not None and new_role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove the Administrator role from your own account. "
                   "Ask another administrator to do it.")
    if new_status is not None and new_status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot disable the account you are signed in with.")


@router.post("/{user_id}/disable", response_model=UserOut)
async def disable_user(user_id: str, request: Request,
                       admin: User = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)) -> UserOut:
    """Disable an account. Section 10: disable rather than delete."""
    user = await _get_or_404(session, user_id)
    _guard_last_admin(user, admin, new_role=None, new_status=UserStatus.INACTIVE.value)
    if user.status != UserStatus.INACTIVE.value:
        before = user.status
        user.status = UserStatus.INACTIVE.value
        audit.record(session, AuditEvent.USER_DISABLED, request=request, subject=user,
                     performed_by=admin, detail={"from": before, "to": user.status})
        await session.commit()
        await session.refresh(user)
    return (await _with_creator(session, [user]))[0]


@router.post("/{user_id}/enable", response_model=UserOut)
async def enable_user(user_id: str, request: Request,
                      admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)) -> UserOut:
    """Re-enable an account, clearing any brute-force lockout with it."""
    user = await _get_or_404(session, user_id)
    if user.status != UserStatus.ACTIVE.value:
        before = user.status
        user.status = UserStatus.ACTIVE.value
        user.failed_login_count = 0
        user.locked_at = None
        audit.record(session, AuditEvent.USER_ENABLED, request=request, subject=user,
                     performed_by=admin, detail={"from": before, "to": user.status})
        await session.commit()
        await session.refresh(user)
    return (await _with_creator(session, [user]))[0]


@router.post("/{user_id}/reset-password", response_model=Message)
async def reset_password(user_id: str, payload: PasswordReset, request: Request,
                         admin: User = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)) -> Message:
    """Set a new password for someone else (section 10).

    The existing password is never shown, never returned and never needed: an
    administrator cannot read it, only replace it. The new one is hashed before it
    reaches the database and appears in no log or audit row.
    """
    user = await _get_or_404(session, user_id)
    user.hashed_password = hash_password(payload.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    # A reset is also the remedy for a locked-out user, so it clears the lockout.
    user.failed_login_count = 0
    if user.status == UserStatus.LOCKED.value:
        user.status = UserStatus.ACTIVE.value
        user.locked_at = None
    audit.record(session, AuditEvent.USER_PASSWORD_RESET, request=request,
                 subject=user, performed_by=admin)
    await session.commit()
    logger.info("password reset", extra={"operation": "user_password_reset",
                                         "status": "ok", "user_id": user.id})
    return Message(message=f"Password reset for {user.username}. "
                           "Ask them to change it after signing in.")


@router.get("/{user_id}/audit", response_model=Page[AuditLogOut])
async def user_audit(user_id: str, limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0),
                     _: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)) -> Page[AuditLogOut]:
    await _get_or_404(session, user_id)
    rows = await audit.recent(session, limit=limit, offset=offset, user_id=user_id)
    total = await audit.count(session, user_id=user_id)
    return Page(items=[AuditLogOut.model_validate(row) for row in rows], total=total,
                limit=limit, offset=offset)
