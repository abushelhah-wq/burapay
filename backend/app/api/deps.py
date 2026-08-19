"""
Request dependencies: the database session and the authenticated user.

Two roles (specification section 22). Admin configures gateways and credentials, runs
tests and deletes results; Viewer reads dashboards and exports reports. The
distinction is enforced here, once, rather than being re-checked in every route.
"""

from __future__ import annotations

from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_session
from app.models import User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
        session: AsyncSession = Depends(get_session)) -> User:
    token = credentials.credentials if credentials else request.cookies.get("burapay_token")
    if not token:
        raise CREDENTIALS_ERROR
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Invalid or expired session: {type(exc).__name__}.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc

    user = await session.get(User, payload.get("sub", ""))
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires the Admin role.")
    return user


#: Viewer access is the baseline: any authenticated, active user has it.
require_viewer = get_current_user
