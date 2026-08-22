"""
Password hashing, password policy, JWT issuing and CSRF tokens.

Passwords are hashed with bcrypt via passlib — a modern, deliberately slow,
salted algorithm (specification section 9). Nothing here ever returns, logs or
stores a plaintext password: the only functions that see one take it as an
argument and hand back a hash or a boolean.

Tokens are HS256 JWTs signed with ``APP_SECRET_KEY`` carrying the user id, role and
username — never a credential, never anything a gateway issued. They expire, which is
what makes ``ACCESS_TOKEN_EXPIRE_MINUTES`` a session lifetime rather than a
suggestion.
"""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

#: bcrypt truncates at 72 bytes; anything past that is silently ignored, which would
#: make two different long passwords interchangeable. Rejected rather than truncated.
PASSWORD_MAX_BYTES = 72
PASSWORD_MIN_LENGTH = 12


class PasswordPolicyError(ValueError):
    """A password that does not meet the complexity rules.

    Carries the unmet rules, never the password.
    """

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def validate_password(password: str, *, username: str = "", email: str = "") -> None:
    """Enforce the complexity rules from specification section 10.

    Raises :class:`PasswordPolicyError` listing everything that is wrong, rather than
    only the first problem: a form that reveals one rule per submission is a form
    people work around.
    """
    problems: List[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        problems.append(f"no more than {PASSWORD_MAX_BYTES} bytes")
    if not re.search(r"[a-z]", password):
        problems.append("a lower-case letter")
    if not re.search(r"[A-Z]", password):
        problems.append("an upper-case letter")
    if not re.search(r"[0-9]", password):
        problems.append("a digit")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("a symbol")

    # A password that contains the account handle is guessable from the account name
    # alone, however many character classes it satisfies.
    lowered = password.lower()
    for part in (username, email.split("@")[0] if email else ""):
        if part and len(part) >= 4 and part.lower() in lowered:
            problems.append("something other than the username or email address")
            break

    if problems:
        raise PasswordPolicyError(problems)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        # A malformed stored hash must read as "wrong password", never as a 500.
        return False


#: A real bcrypt hash of a value nobody knows, verified against when the submitted
#: handle matches no account. Skipping the hash there would make "no such user"
#: measurably faster than "wrong password" and turn the login endpoint into a user
#: directory (specification section 9).
_DECOY_HASH = pwd_context.hash(secrets.token_urlsafe(32))


def burn_password_time() -> None:
    """Spend the same work as a real verification, and learn nothing from it."""
    pwd_context.verify("burapay-no-such-account", _DECOY_HASH)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def create_access_token(subject: str, *, role: str, email: str, username: str = "",
                        expires_minutes: Optional[int] = None) -> str:
    minutes = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": subject,
        "role": role,
        "email": email,
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        # A per-session identifier. Not used to revoke today, but it is what makes a
        # token traceable to one sign-in in the audit log.
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode a token, raising ``jwt.PyJWTError`` if it is invalid or expired."""
    return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #

#: The session cookie. HttpOnly, so script cannot read it.
SESSION_COOKIE = "burapay_token"
#: The CSRF cookie. Deliberately *not* HttpOnly: the double-submit defence works
#: because our own JavaScript can read this and echo it in a header, while a
#: cross-site page can send the cookie but cannot read it to build the header.
CSRF_COOKIE = "burapay_csrf"
CSRF_HEADER = "X-CSRF-Token"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: Optional[str], header_value: Optional[str]) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


def use_secure_cookies() -> bool:
    """Whether cookies get the ``Secure`` attribute.

    Tied to the public base URL rather than to a separate switch, so a deployment
    cannot end up serving HTTPS with cookies that are allowed to travel in the clear.
    Plain-HTTP development keeps working because ``Secure`` would stop the cookie
    being stored at all.
    """
    return settings.public_base_url.startswith("https://")
