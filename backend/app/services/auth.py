"""
Sign-in: rate limiting, brute-force protection, and the credential check itself
(specification section 9).

Three defences, each covering what the others miss:

1. **Per-address rate limiting.** A sliding window over recent attempts from one
   client address, checked *before* any account lookup. Per-account lockout alone
   does not stop one attacker spraying one password across a hundred usernames.
2. **Per-account lockout.** After ``LOGIN_MAX_FAILED_ATTEMPTS`` consecutive
   failures the account moves to ``LOCKED`` and stays there for
   ``LOGIN_LOCKOUT_MINUTES``. It unlocks itself when the window passes, so a locked
   account is not an administrator ticket, and an administrator can clear it sooner.
3. **A single generic failure.** "Invalid username or password" for a missing
   account, a wrong password, a disabled account and a locked one alike, with the
   same work done in each case — see ``burn_password_time``. Telling a caller which
   of the four it was turns the login form into a user directory.

The rate-limit window lives in process memory. That is honest about what it is: a
single-process deployment gets exactly what it says, and the per-account lockout,
which is in the database, is the defence that survives a restart or a second worker.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import burn_password_time, verify_password
from app.models import User, UserStatus

#: The one message every failure returns. Deliberately says nothing about which of
#: the several possible causes applied.
GENERIC_FAILURE = "Invalid username or password."


# --------------------------------------------------------------------------- #
# Per-address rate limiting
# --------------------------------------------------------------------------- #

class SlidingWindowLimiter:
    """Attempts per key within a window. In-process, deliberately simple."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> Deque[float]:
        hits = self._hits[key]
        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def check(self, key: str) -> Tuple[bool, int]:
        """``(allowed, retry_after_seconds)`` without consuming an attempt."""
        now = time.monotonic()
        hits = self._prune(key, now)
        if len(hits) < self.limit:
            return True, 0
        return False, max(1, int(self.window - (now - hits[0])) + 1)

    def hit(self, key: str) -> None:
        """Record a failed attempt. Successes do not count against the limit."""
        now = time.monotonic()
        self._prune(key, now)
        self._hits[key].append(now)

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def reset(self) -> None:
        self._hits.clear()


login_limiter = SlidingWindowLimiter(settings.login_rate_limit_attempts,
                                     settings.login_rate_limit_window_seconds)


# --------------------------------------------------------------------------- #
# Credential check
# --------------------------------------------------------------------------- #

@dataclass
class AuthOutcome:
    """What happened, in enough detail for the audit log and no more.

    ``user`` is set whenever the handle matched an account, even when the attempt
    failed — the audit row should be attributable. ``reason`` never reaches the
    client; the client gets :data:`GENERIC_FAILURE`.
    """

    user: Optional[User]
    ok: bool
    reason: str = ""
    locked_now: bool = False


async def find_by_handle(session: AsyncSession, handle: str) -> Optional[User]:
    """Look an account up by username *or* email address, case-insensitively."""
    needle = (handle or "").strip().lower()
    if not needle:
        return None
    result = await session.execute(
        select(User).where(or_(func.lower(User.username) == needle,
                               func.lower(User.email) == needle)))
    return result.scalars().first()


def _lockout_expired(user: User) -> bool:
    if user.locked_at is None:
        return True
    locked_at = user.locked_at
    if locked_at.tzinfo is None:
        locked_at = locked_at.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=settings.login_lockout_minutes)
    return datetime.now(timezone.utc) - locked_at >= window


async def authenticate(session: AsyncSession, handle: str,
                       password: str) -> AuthOutcome:
    """Check credentials and apply brute-force accounting.

    Does not commit: the caller writes the audit row in the same transaction, so the
    attempt and its record land together.
    """
    user = await find_by_handle(session, handle)

    if user is None:
        # Same work as a real verification, so "no such account" is not the fast path.
        burn_password_time()
        return AuthOutcome(None, False, "no such account")

    # A lockout that has run its course clears itself here, before the password is
    # checked, so the correct password works again the moment the window passes.
    if user.status == UserStatus.LOCKED.value and _lockout_expired(user):
        user.status = UserStatus.ACTIVE.value
        user.locked_at = None
        user.failed_login_count = 0

    if not verify_password(password, user.hashed_password):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        locked_now = False
        if (user.status == UserStatus.ACTIVE.value
                and user.failed_login_count >= settings.login_max_failed_attempts):
            user.status = UserStatus.LOCKED.value
            user.locked_at = datetime.now(timezone.utc)
            locked_now = True
        return AuthOutcome(user, False, "wrong password", locked_now=locked_now)

    # The password was right. Everything below is about whether the account may be
    # used at all — and none of it changes what the caller is told.
    if user.status == UserStatus.LOCKED.value:
        return AuthOutcome(user, False, "account locked")
    if user.status != UserStatus.ACTIVE.value:
        return AuthOutcome(user, False, "account inactive")

    user.failed_login_count = 0
    user.locked_at = None
    return AuthOutcome(user, True)
