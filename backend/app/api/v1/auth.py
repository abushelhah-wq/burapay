"""Authentication routes (specification section 22)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_session
from app.models import User, UserRole
from app.schemas import (LoginRequest, Message, PasswordChange, TokenResponse,
                         UserCreate, UserOut)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, response: Response,
                session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = (await session.execute(
        select(User).where(User.email == payload.email.lower()))).scalar_one_or_none()

    # One message for both "no such user" and "wrong password": distinguishing them
    # tells an attacker which addresses are worth attacking.
    if user is None or not verify_password(payload.password, user.hashed_password):
        logger.warning("failed login", extra={"operation": "login", "status": "denied"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Incorrect email or password.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This account is disabled.")

    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()

    token = create_access_token(user.id, role=user.role, email=user.email)
    # Also set as a cookie so browser-driven flows (the HPP return leg, exports opened
    # in a new tab) authenticate without JavaScript attaching a header.
    response.set_cookie("burapay_token", token, httponly=True, samesite="lax",
                        secure=settings.public_base_url.startswith("https://"),
                        max_age=settings.access_token_expire_minutes * 60, path="/")
    return TokenResponse(access_token=token,
                         expires_in=settings.access_token_expire_minutes * 60,
                         user=UserOut.model_validate(user))


@router.post("/logout", response_model=Message)
async def logout(response: Response) -> Message:
    response.delete_cookie("burapay_token", path="/")
    return Message(message="Signed out.")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/change-password", response_model=Message)
async def change_password(payload: PasswordChange,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)) -> Message:
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Current password is incorrect.")
    user.hashed_password = hash_password(payload.new_password)
    await session.commit()
    return Message(message="Password changed.")


@router.get("/users", response_model=List[UserOut])
async def list_users(_: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)) -> List[UserOut]:
    rows = (await session.execute(select(User).order_by(User.created_at))).scalars()
    return [UserOut.model_validate(row) for row in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, _: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)) -> UserOut:
    existing = (await session.execute(
        select(User).where(User.email == payload.email.lower()))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A user with that email already exists.")
    user = User(email=payload.email.lower(), full_name=payload.full_name,
                hashed_password=hash_password(payload.password),
                role=UserRole(payload.role).value, is_active=True)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return UserOut.model_validate(user)


@router.delete("/users/{user_id}", response_model=Message)
async def delete_user(user_id: str, admin: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)) -> Message:
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="You cannot delete the account you are signed in with.")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")
    await session.delete(user)
    await session.commit()
    return Message(message="User deleted.")
