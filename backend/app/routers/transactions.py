"""
Transaction list and detail (§5).

This is the audit log. It is backed by the real tables, paginated, and
filterable by gateway, integration type, operation, status and date range.
Nothing here is sampled, summarised or synthesised -- the detail view serves
the ``api_call_logs`` rows exactly as they were written, in sequence order, so
a transaction can be explained without opening a gateway dashboard (§2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.gateways.registry import adapter_class_or_none
from app.models import (
    ApiCallLog, Gateway, IntegrationType, Token, Transaction, WebhookReceived,
)
from app.schemas.common import Page
from app.schemas.transactions import (
    TokenOut, TransactionDetail, TransactionOut, WebhookOut,
)
from app.security.auth import require_user
from app.services.serialize import (
    token_out, transaction_detail, transaction_out, webhook_out,
)

router = APIRouter(prefix="/api", tags=["transactions"])


async def _lookups(session: AsyncSession):
    gateways = {
        row.id: row for row in (await session.execute(select(Gateway))).scalars()
    }
    integration_types = {
        row.id: row
        for row in (await session.execute(select(IntegrationType))).scalars()
    }
    return gateways, integration_types


@router.get("/transactions", response_model=Page[TransactionOut])
async def list_transactions(
    gateway: Optional[str] = Query(default=None, description="Gateway code."),
    integration_type: Optional[str] = Query(default=None),
    operation: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    parent_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> Page[TransactionOut]:
    gateways, integration_types = await _lookups(session)

    statement = select(Transaction)
    if gateway:
        match = next((row for row in gateways.values() if row.code == gateway), None)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown gateway {gateway!r}.")
        statement = statement.where(Transaction.gateway_id == match.id)
    if integration_type:
        match = next(
            (row for row in integration_types.values() if row.code == integration_type),
            None,
        )
        if match is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Unknown integration type {integration_type!r}.",
            )
        statement = statement.where(Transaction.integration_type_id == match.id)
    if operation:
        statement = statement.where(Transaction.operation == operation)
    if status_filter:
        statement = statement.where(Transaction.status == status_filter)
    if since is not None:
        statement = statement.where(Transaction.created_at >= since)
    if until is not None:
        statement = statement.where(Transaction.created_at <= until)
    if parent_id is not None:
        statement = statement.where(Transaction.parent_transaction_id == parent_id)

    total = await session.scalar(
        select(func.count()).select_from(statement.subquery())
    )
    rows = (
        await session.execute(
            statement.order_by(Transaction.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()

    return Page[TransactionOut](
        items=[
            transaction_out(row, gateways=gateways, integration_types=integration_types)
            for row in rows
        ],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionDetail)
async def get_transaction(
    transaction_id: int,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionDetail:
    """
    One transaction with its complete, ordered call trail.

    The ``api_calls`` list is the answer to "what actually happened": method,
    URL, status code, duration, the timeout that applied, and the redacted
    request and response bodies.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction.")

    gateways, integration_types = await _lookups(session)

    calls = (
        await session.execute(
            select(ApiCallLog)
            .where(ApiCallLog.transaction_id == transaction_id)
            .order_by(ApiCallLog.sequence_number, ApiCallLog.id)
        )
    ).scalars()

    children = (
        await session.execute(
            select(Transaction)
            .where(Transaction.parent_transaction_id == transaction_id)
            .order_by(Transaction.id)
        )
    ).scalars()

    gateway = gateways.get(transaction.gateway_id)
    return transaction_detail(
        transaction,
        gateways=gateways,
        integration_types=integration_types,
        api_calls=list(calls),
        children=list(children),
        adapter_class=adapter_class_or_none(gateway.code) if gateway else None,
    )


@router.get("/webhooks", response_model=Page[WebhookOut])
async def list_webhooks(
    gateway: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> Page[WebhookOut]:
    """Raw inbound callbacks, including ones that matched nothing (§4k)."""
    gateways, _ = await _lookups(session)
    statement = select(WebhookReceived)
    if gateway:
        match = next((row for row in gateways.values() if row.code == gateway), None)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown gateway {gateway!r}.")
        statement = statement.where(WebhookReceived.gateway_id == match.id)

    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = (
        await session.execute(
            statement.order_by(WebhookReceived.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()

    return Page[WebhookOut](
        items=[webhook_out(row, gateways.get(row.gateway_id)) for row in rows],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.get("/tokens", response_model=list[TokenOut])
async def list_tokens(
    gateway: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> list[TokenOut]:
    """Stored card tokens (§4g). References only -- no PAN is ever stored."""
    gateways, _ = await _lookups(session)
    statement = select(Token).order_by(Token.id.desc())
    if gateway:
        match = next((row for row in gateways.values() if row.code == gateway), None)
        if match is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown gateway {gateway!r}.")
        statement = statement.where(Token.gateway_id == match.id)
    rows = (await session.execute(statement)).scalars()
    return [token_out(row, gateways.get(row.gateway_id)) for row in rows]
