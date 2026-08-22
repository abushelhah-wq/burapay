"""
Request dependencies: the database session, the authenticated user, and role checks.

Two roles (specification section 10). **ADMIN** configures gateways and credentials,
manages users and deletes results. **USER** signs in, runs payment tests, performs the
permitted transaction operations and reads everything the study produces. The
distinction is enforced here, once, rather than being re-checked in every route — and
it is enforced on the backend, because a hidden button is not a permission.

Two ways to authenticate, with different risks:

* An ``Authorization: Bearer`` header. Nothing attaches it automatically, so it cannot
  be replayed by a cross-site request and needs no CSRF defence.
* The ``burapay_token`` cookie, which browsers *do* attach automatically. That is what
  makes browser-driven navigation work — the gateway return leg, a report opened in a
  new tab — and also what makes CSRF possible, so cookie-authenticated requests with a
  side effect must carry the double-submit token as well.
"""

from __future__ import annotations

from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, csrf_matches,
                               decode_access_token)
from app.db.session import get_session
from app.models import User, UserRole, UserStatus

bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"})

#: Methods that cannot change anything, and so need no CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _check_csrf(request: Request) -> None:
    """Double-submit check for a cookie-authenticated write.

    The token is sent twice: in a cookie the browser attaches automatically, and in a
    header only our own script can set. A cross-site page can cause the cookie to be
    sent but cannot read it to build the header, so the two matching is proof the
    request came from our own front end.
    """
    if request.method in SAFE_METHODS:
        return
    if not csrf_matches(request.cookies.get(CSRF_COOKIE),
                        request.headers.get(CSRF_HEADER)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token. Sign in again and retry.")


async def get_current_user(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
        session: AsyncSession = Depends(get_session)) -> User:
    if credentials:
        token: Optional[str] = credentials.credentials
        from_cookie = False
    else:
        token = request.cookies.get(SESSION_COOKIE)
        from_cookie = token is not None

    if not token:
        raise CREDENTIALS_ERROR

    if from_cookie:
        _check_csrf(request)

    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Your session has expired. Please sign in again.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Invalid session: {type(exc).__name__}.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc

    user = await session.get(User, payload.get("sub", ""))
    # The token says what the account was when it was issued; the row says what it is
    # now. Disabling an account therefore takes effect on the next request rather than
    # when the token would have expired.
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise CREDENTIALS_ERROR
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires the Administrator role.")
    return user


#: The baseline: any authenticated, active account. Named for what it grants rather
#: than aliased to ``get_current_user``, so a route reads as a permission decision.
require_user = get_current_user
