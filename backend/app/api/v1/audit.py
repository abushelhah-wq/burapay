"""
The audit log (specification section 10).

Read-only and administrator-only. There is deliberately no route that writes, edits or
deletes a row: an audit trail an operator can rewrite answers no question. Rows are
written by the services that perform the audited actions, in the same transaction.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.session import get_session
from app.models import AuditEvent, User
from app.schemas import AuditLogOut, Page
from app.services import audit as audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("/events", response_model=List[str])
async def list_event_types(_: User = Depends(require_admin)) -> List[str]:
    """Every event the platform can record, so the filter is not a free-text guess."""
    return [event.value for event in AuditEvent]


@router.get("", response_model=Page[AuditLogOut])
async def list_audit_logs(
        event: Optional[str] = Query(None, description="Exact event name."),
        user_id: Optional[str] = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: User = Depends(require_admin),
        session: AsyncSession = Depends(get_session)) -> Page[AuditLogOut]:
    rows = await audit_service.recent(session, limit=limit, offset=offset,
                                      event=event, user_id=user_id)
    total = await audit_service.count(session, event=event, user_id=user_id)
    return Page(items=[AuditLogOut.model_validate(row) for row in rows], total=total,
                limit=limit, offset=offset)
