"""
The API call log — every request this platform made to a gateway, and what came back.

Separate from the transaction pages on purpose. Those answer "how did this payment
go?"; this answers "what does this gateway actually do?" — the question you have when a
provider returns nothing but "General error", when two gateways disagree about what a
field is called, or when you want to see the shape of a working call next to a broken
one.

Every row carries the request and the response, both sanitized before they were ever
stored: card numbers truncated to a last four, CVVs dropped by name, secrets redacted
(specification sections 24, 25, 43).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.db.session import get_session
from app.models import ApiMeasurement, Transaction, User
from app.schemas import ApiLogEntry, Message, Page

router = APIRouter(prefix="/logs", tags=["logs"])


def _filtered(statement: Select, *, gateway_code: Optional[List[str]],
              integration_type: Optional[str], normalized_operation: Optional[str],
              outcome: Optional[str], transaction_id: Optional[str],
              search: Optional[str], date_from: Optional[datetime],
              date_to: Optional[datetime]) -> Select:
    """Apply the filters. The join to transactions is what makes gateway filtering work.

    ``outcome`` is deliberately three-valued rather than a boolean: "everything",
    "only what failed" and "only what worked" are all things you want, and a boolean
    with a null default cannot express the first without ambiguity.
    """
    statement = statement.join(Transaction,
                               Transaction.id == ApiMeasurement.transaction_id)
    if gateway_code:
        statement = statement.where(Transaction.gateway_code.in_(gateway_code))
    if integration_type:
        statement = statement.where(Transaction.integration_type == integration_type)
    if normalized_operation:
        statement = statement.where(
            ApiMeasurement.normalized_operation == normalized_operation)
    if outcome == "failed":
        statement = statement.where(ApiMeasurement.success.is_(False))
    elif outcome == "succeeded":
        statement = statement.where(ApiMeasurement.success.is_(True))
    if transaction_id:
        statement = statement.where(ApiMeasurement.transaction_id == transaction_id)
    if search:
        # One box over the three fields worth searching: the endpoint, the operation
        # name and the gateway's own message. Anything more needs a query language.
        pattern = f"%{search}%"
        statement = statement.where(or_(
            ApiMeasurement.endpoint.ilike(pattern),
            ApiMeasurement.operation_name.ilike(pattern),
            ApiMeasurement.gateway_response_message.ilike(pattern)))
    if date_from:
        statement = statement.where(ApiMeasurement.started_at >= date_from)
    if date_to:
        statement = statement.where(ApiMeasurement.started_at <= date_to)
    return statement


@router.get("", response_model=Page[ApiLogEntry])
async def list_logs(
        gateway_code: Optional[List[str]] = Query(None),
        integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
        normalized_operation: Optional[str] = Query(None),
        outcome: Optional[str] = Query(None, pattern="^(succeeded|failed)$"),
        transaction_id: Optional[str] = Query(None),
        search: Optional[str] = Query(None, max_length=200),
        date_from: Optional[datetime] = Query(None),
        date_to: Optional[datetime] = Query(None),
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
        _: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)) -> Page[ApiLogEntry]:
    """Newest call first, across every transaction."""
    filters = dict(gateway_code=gateway_code, integration_type=integration_type,
                   normalized_operation=normalized_operation, outcome=outcome,
                   transaction_id=transaction_id, search=search,
                   date_from=date_from, date_to=date_to)

    total = (await session.execute(_filtered(
        select(func.count()).select_from(ApiMeasurement), **filters))).scalar_one()

    rows = (await session.execute(
        _filtered(select(ApiMeasurement, Transaction), **filters)
        .order_by(ApiMeasurement.started_at.desc(), ApiMeasurement.sequence.desc())
        .limit(limit).offset(offset))).all()

    return Page[ApiLogEntry](items=[_entry(call, transaction) for call, transaction in rows],
                             total=total, limit=limit, offset=offset)


def _entry(call: ApiMeasurement, transaction: Transaction) -> ApiLogEntry:
    return ApiLogEntry(
        id=call.id,
        transaction_id=call.transaction_id,
        gateway_code=transaction.gateway_code,
        integration_type=transaction.integration_type,
        payment_mode=transaction.payment_mode,
        environment=transaction.environment,
        merchant_reference=transaction.merchant_reference,
        sequence=call.sequence,
        operation_name=call.operation_name,
        normalized_operation=call.normalized_operation,
        endpoint=call.endpoint,
        http_method=call.http_method,
        started_at=call.started_at,
        completed_at=call.completed_at,
        duration_ms=call.duration_ms,
        http_status=call.http_status,
        gateway_response_code=call.gateway_response_code,
        gateway_response_message=call.gateway_response_message,
        success=call.success,
        error_category=call.error_category,
        error_message=call.error_message,
        timed_out=call.timed_out,
        is_setup_call=call.is_setup_call,
        request_size_bytes=call.request_size_bytes,
        response_size_bytes=call.response_size_bytes,
        request_snippet=call.request_snippet,
        response_snippet=call.response_snippet,
    )


@router.get("/summary", response_model=Dict[str, Any])
async def log_summary(_: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """What the filters need to offer, drawn from what has actually been recorded.

    A hard-coded operation list goes stale the moment an adapter adds one, and offering
    a gateway with no calls to show is a dead end. Both lists come from the data.
    """
    gateways = (await session.execute(
        select(Transaction.gateway_code, func.count(ApiMeasurement.id))
        .join(ApiMeasurement, ApiMeasurement.transaction_id == Transaction.id)
        .group_by(Transaction.gateway_code)
        .order_by(func.count(ApiMeasurement.id).desc()))).all()
    operations = (await session.execute(
        select(ApiMeasurement.normalized_operation)
        .group_by(ApiMeasurement.normalized_operation)
        .order_by(ApiMeasurement.normalized_operation))).scalars().all()
    total = (await session.execute(
        select(func.count()).select_from(ApiMeasurement))).scalar_one()
    failed = (await session.execute(
        select(func.count()).select_from(ApiMeasurement)
        .where(ApiMeasurement.success.is_(False)))).scalar_one()
    return {
        "total": total,
        "failed": failed,
        "gateways": [{"code": code, "calls": count} for code, count in gateways],
        "operations": list(operations),
    }


@router.delete("", response_model=Message)
async def clear_logs(before: Optional[datetime] = Query(
                         None, description="Delete calls started before this. "
                                           "Omit to delete every call."),
                     _: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)) -> Message:
    """Delete recorded calls. Admin only, because it destroys evidence.

    The transactions themselves are untouched — their timings are columns on the
    transaction row, so clearing the call log does not rewrite any measurement that has
    already been reported.
    """
    statement = select(ApiMeasurement.id)
    if before:
        statement = statement.where(ApiMeasurement.started_at < before)
    ids = (await session.execute(statement)).scalars().all()
    if not ids:
        return Message(message="No calls matched; nothing was deleted.")
    for measurement_id in ids:
        record = await session.get(ApiMeasurement, measurement_id)
        if record is not None:
            await session.delete(record)
    await session.commit()
    return Message(message=f"Deleted {len(ids)} recorded call"
                           f"{'' if len(ids) == 1 else 's'}.")
