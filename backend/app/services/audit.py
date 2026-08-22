"""
The audit trail (specification section 10).

Every authentication and user-management event lands here: who did what, to whom,
from where, and when. Two rules govern what may be written:

* **No secrets.** Never a password, a password hash, a token, a CSRF value or a
  gateway credential. :func:`_safe_detail` drops anything whose key looks like one,
  so a caller that passes a dict through carelessly still cannot leak.
* **Never lie by omission.** A failed login against a handle that matches no account
  is still recorded, with the handle that was tried and a null ``user_id``. Recording
  only the attempts that hit a real account would hide exactly the pattern this log
  exists to expose.

Rows are written on the caller's session and committed with the caller's work, so an
action and the record of it either both happen or neither does.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AuditEvent, AuditLog, User

#: Key fragments whose values are never written to the audit log, whatever the caller
#: passed. Matched case-insensitively against the whole key.
_SECRET_KEY_FRAGMENTS = ("password", "secret", "token", "hash", "credential", "csrf",
                         "authorization", "api_key", "apikey", "private")


def _safe_detail(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop anything that looks like a credential, at any depth."""
    if not detail:
        return {}
    clean: Dict[str, Any] = {}
    for key, value in detail.items():
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
            # The *fact* that it changed is useful; the value never is.
            clean[key] = "[redacted]"
            continue
        clean[key] = _safe_detail(value) if isinstance(value, dict) else value
    return clean


def client_address(request: Optional[Request]) -> Optional[str]:
    """The caller's address, as far as it can be trusted.

    Behind the deployment's Traefik the socket address is the proxy's, so the first
    entry of ``X-Forwarded-For`` is the real client. Directly exposed, that header is
    attacker-controlled and must be ignored — hence the setting rather than a guess.
    """
    if request is None:
        return None
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


def user_agent(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    value = request.headers.get("user-agent")
    return value[:500] if value else None


def record(session: AsyncSession, event: AuditEvent, *,
           request: Optional[Request] = None,
           subject: Optional[User] = None,
           subject_label: Optional[str] = None,
           performed_by: Optional[User] = None,
           detail: Optional[Dict[str, Any]] = None) -> AuditLog:
    """Add an audit row to ``session``. The caller commits.

    Returns the row so a test can assert on it without a second query.
    """
    row = AuditLog(
        event=event.value,
        user_id=subject.id if subject is not None else None,
        performed_by_user_id=performed_by.id if performed_by is not None else None,
        subject_label=(subject_label or (subject.username if subject is not None else None)),
        performed_by_label=(performed_by.username if performed_by is not None else None),
        ip_address=client_address(request),
        user_agent=user_agent(request),
        detail=_safe_detail(detail))
    session.add(row)
    return row


def changes(before: Dict[str, Any], after: Dict[str, Any],
            fields: Sequence[str]) -> Dict[str, Any]:
    """The subset of ``fields`` that actually changed, as ``{field: {from, to}}``."""
    return {field: {"from": before.get(field), "to": after.get(field)}
            for field in fields
            if field in after and before.get(field) != after.get(field)}


async def recent(session: AsyncSession, *, limit: int = 100, offset: int = 0,
                 event: Optional[str] = None,
                 user_id: Optional[str] = None) -> Sequence[AuditLog]:
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    if event:
        query = query.where(AuditLog.event == event)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    rows = await session.execute(query.limit(limit).offset(offset))
    return list(rows.scalars())


async def count(session: AsyncSession, *, event: Optional[str] = None,
                user_id: Optional[str] = None) -> int:
    from sqlalchemy import func

    query = select(func.count()).select_from(AuditLog)
    if event:
        query = query.where(AuditLog.event == event)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    return (await session.execute(query)).scalar_one()
