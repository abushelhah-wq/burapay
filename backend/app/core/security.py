"""
Password hashing and JWT issuing.

Passwords are hashed with bcrypt via passlib. Tokens are short-lived HS256 JWTs
signed with ``APP_SECRET_KEY`` and carry only the user id, email and role — never a
credential, never anything a gateway issued.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        # A malformed stored hash must read as "wrong password", never as a 500.
        return False


def create_access_token(subject: str, *, role: str, email: str,
                        expires_minutes: Optional[int] = None) -> str:
    minutes = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": subject,
        "role": role,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode a token, raising ``jwt.PyJWTError`` if it is invalid or expired."""
    return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])
