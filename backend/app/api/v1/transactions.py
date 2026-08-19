"""
Transactions: starting one, completing the HPP return leg, listing and detail
(specification sections 3, 8, 44, 45).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.errors import BenchmarkError, NotConfigured, NotSupported
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import BrowserMeasurement, TimelineEvent, Transaction, TransactionEvent, User
from app.schemas import (BrowserMetricsIn, Message, Page, StartTransactionRequest,
                         StartTransactionResponse, TransactionDetail, TransactionOut)
from app.services import analytics
from app.services.benchmark import (BenchmarkRefused, complete_hpp_transaction,
                                    run_direct_transaction, start_hpp_transaction)

router = APIRouter(prefix="/transactions", tags=["transactions"])
logger = get_logger(__name__)


@router.post("/start", response_model=StartTransactionResponse)
async def start_transaction(payload: StartTransactionRequest,
                            _: User = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)
                            ) -> StartTransactionResponse:
    """Start one benchmarked transaction.

    Direct API completes here and returns the final status. HPP returns a redirect
    target: the customer's time on the gateway's page is theirs, and the transaction
    stays PENDING until they come back.
    """
    try:
        if payload.integration_type == "direct":
            transaction = await run_direct_transaction(
                session, gateway_code=payload.gateway_code, amount=payload.amount,
                currency=payload.currency, description=payload.description,
                reference=payload.reference, environment=payload.environment,
                methodology=payload.methodology)
            return StartTransactionResponse(
                transaction_id=transaction.id, status=transaction.status,
                gateway_reference=transaction.gateway_transaction_id)

        transaction, hpp = await start_hpp_transaction(
            session, gateway_code=payload.gateway_code, amount=payload.amount,
            currency=payload.currency, description=payload.description,
            reference=payload.reference, environment=payload.environment,
            methodology=payload.methodology)
        return StartTransactionResponse(
            transaction_id=transaction.id, status=transaction.status,
            redirect_url=hpp.redirect_url, mode=hpp.mode,
            gateway_reference=hpp.gateway_reference)
    except (BenchmarkRefused, NotConfigured, NotSupported):
        # Guard rails and configuration problems are the caller's to fix; the
        # application-level handlers turn these into a 400 with an explanation.
        raise
    except BenchmarkError as exc:
        # A gateway rejecting the session request is recorded against the transaction;
        # the client still needs to be told why nothing was started.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/{transaction_id}/return")
async def hpp_return(transaction_id: str, request: Request,
                     session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    """Where the gateway sends the customer's browser back to.

    Deliberately unauthenticated: the gateway controls this navigation and cannot
    carry a session token. It is safe because the URL only names a transaction id that
    the platform itself generated, and the only thing it does is confirm that
    transaction's outcome against the gateway.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        return RedirectResponse("/transactions?error=unknown-transaction", status_code=303)

    session.add(TransactionEvent(
        transaction_id=transaction.id,
        event_type=TimelineEvent.RETURN_URL_RECEIVED.value,
        event_timestamp=datetime.now(transaction.started_at.tzinfo),
        offset_ms=0.0, label="Customer returned from gateway"))
    try:
        await complete_hpp_transaction(session, transaction, dict(request.query_params))
    except Exception as exc:                              # noqa: BLE001
        logger.warning("hpp return leg failed",
                       extra={"gateway": transaction.gateway_code,
                              "transaction_id": transaction.id,
                              "operation": "hpp_return", "status": "error",
                              "error": str(exc)[:300]})
    return RedirectResponse(f"/transactions/{transaction.id}", status_code=303)


@router.post("/{transaction_id}/browser-metrics", response_model=Message)
async def record_browser_metrics(transaction_id: str, payload: BrowserMetricsIn,
                                 session: AsyncSession = Depends(get_session)) -> Message:
    """Record what the browser's Performance API could see (section 10).

    Stored in their own table and never merged into the API measurements: these come
    from a different clock on a different machine, and cross-origin rules mean a
    hosted page may expose almost nothing. What is missing stays missing.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such transaction.")
    for name, value in payload.metrics.items():
        try:
            milliseconds = float(value)
        except (TypeError, ValueError):
            continue
        if milliseconds < 0:
            continue
        session.add(BrowserMeasurement(transaction_id=transaction.id, metric_name=name[:80],
                                       value_ms=round(milliseconds, 3),
                                       origin_scope=payload.origin_scope))
    if payload.page_load_time_ms is not None and payload.page_load_time_ms >= 0:
        transaction.page_load_time_ms = round(float(payload.page_load_time_ms), 3)
    await session.commit()
    return Message(message="Browser metrics recorded.")


@router.get("", response_model=Page[TransactionOut])
async def list_transactions(
        gateway_code: Optional[List[str]] = Query(None),
        integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
        status_filter: Optional[str] = Query(None, alias="status"),
        currency: Optional[str] = Query(None),
        environment: Optional[str] = Query(None),
        benchmark_run_id: Optional[str] = Query(None),
        merchant_reference: Optional[str] = Query(None),
        gateway_transaction_id: Optional[str] = Query(None),
        methodology: Optional[str] = Query(None),
        date_from: Optional[datetime] = Query(None),
        date_to: Optional[datetime] = Query(None),
        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
        _: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)) -> Page[TransactionOut]:
    """The searchable transaction list from section 44."""
    filters = analytics.TransactionFilters(
        gateway_codes=gateway_code, integration_type=integration_type, status=status_filter,
        currency=currency, environment=environment, benchmark_run_id=benchmark_run_id,
        merchant_reference=merchant_reference, gateway_transaction_id=gateway_transaction_id,
        methodology=methodology, date_from=date_from, date_to=date_to)

    total = (await session.execute(
        filters.apply(select(func.count()).select_from(Transaction)))).scalar_one()
    rows = (await session.execute(
        filters.apply(select(Transaction).order_by(Transaction.started_at.desc()))
        .limit(limit).offset(offset))).scalars()
    return Page[TransactionOut](items=[TransactionOut.model_validate(r) for r in rows],
                                total=total, limit=limit, offset=offset)


@router.get("/{transaction_id}", response_model=TransactionDetail)
async def get_transaction(transaction_id: str, _: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """One transaction with its call list, timeline and browser metrics (section 45)."""
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such transaction.")
    return await analytics.transaction_detail(session, transaction)


@router.delete("/{transaction_id}", response_model=Message)
async def delete_transaction(transaction_id: str, _: User = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)) -> Message:
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such transaction.")
    await session.delete(transaction)
    await session.commit()
    return Message(message="Transaction deleted.")
