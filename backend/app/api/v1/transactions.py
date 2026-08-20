"""
Transactions: starting one, completing the HPP return leg, listing and detail
(specification sections 3, 8, 44, 45).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings as app_settings
from app.core.errors import BenchmarkError, NotConfigured, NotSupported
from app.core.logging import get_logger
from app.db.session import get_session
from app.gateways.base import Card
from app.models import BrowserMeasurement, IntegrationType, Transaction, User
from app.schemas import (BrowserMetricsIn, HppHandoff, Message, Page,
                         StartTransactionRequest, StartTransactionResponse,
                         ThreeDsChallenge, TransactionDetail, TransactionOut)
from app.services import analytics
from app.services.benchmark import (BenchmarkRefused, complete_pending_transaction,
                                    run_direct_transaction, start_hpp_transaction)

router = APIRouter(prefix="/transactions", tags=["transactions"])
logger = get_logger(__name__)


def _accepted_card(payload: StartTransactionRequest) -> Optional[Card]:
    """Turn a typed card into an adapter Card, or refuse it.

    Card entry is refused outright unless somebody turned it on: a PAN typed into this
    application is card data in this application, which is a decision for whoever runs
    the deployment and not a default. Hosted checkout never needs it — the card is
    entered on the provider's page — so a card sent with an HPP request is rejected
    rather than quietly ignored, since accepting it would mean receiving card data for
    no reason at all.
    """
    if payload.card is None:
        return None
    if payload.integration_type != "direct":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hosted checkout collects the card on the provider's own page. "
                   "Remove the card details, or switch to Direct API.")
    if not app_settings.allow_direct_card_entry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Typing a card number into this application is turned off. Set "
                   "ALLOW_DIRECT_CARD_ENTRY=true in the deployment's .env and restart "
                   "the API to enable it — sandbox test cards only. A card token id "
                   "works either way and is not card data.")
    if payload.environment != "sandbox":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Card entry is only ever accepted against a sandbox environment.")
    return Card(number=payload.card.number, month=payload.card.month,
                year=payload.card.year, cvc=payload.card.cvc,
                holder=payload.card.holder)


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
    card = _accepted_card(payload)
    try:
        if payload.integration_type == "direct":
            transaction = await run_direct_transaction(
                session, gateway_code=payload.gateway_code, amount=payload.amount,
                currency=payload.currency, description=payload.description,
                reference=payload.reference, environment=payload.environment,
                methodology=payload.methodology, payment_mode=payload.payment_mode,
                card=card, token_id=payload.token_id,
                agreement_id=payload.agreement_id, initiated_by=payload.initiated_by,
                browser=payload.browser.model_dump() if payload.browser else None)
            context = transaction.context or {}
            return StartTransactionResponse(
                transaction_id=transaction.id, status=transaction.status,
                gateway_reference=transaction.gateway_transaction_id,
                stored_token=context.get("stored_token"),
                agreement_id=context.get("agreement_id"),
                requires_customer_action=bool(context.get("awaiting_customer_action")),
                redirect_url=(context.get("action_url")
                              if context.get("awaiting_customer_action") else None),
                mode="redirect" if context.get("awaiting_customer_action") else None)

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

    Serves both pauses: a hosted checkout the customer has finished, and a Direct
    payment that stopped for a 3DS challenge. Which one it is comes from the
    transaction, so a gateway only ever has one return URL to be told about.

    Deliberately unauthenticated: the gateway controls this navigation and cannot
    carry a session token. It is safe because the URL only names a transaction id that
    the platform itself generated, and the only thing it does is confirm that
    transaction's outcome against the gateway.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        return RedirectResponse("/transactions?error=unknown-transaction", status_code=303)

    # The RETURN_URL_RECEIVED event is recorded by the completion service, which knows
    # the offset from the transaction's own start; recording it here as well would put
    # a duplicate at offset zero, at the wrong end of the timeline.
    try:
        await complete_pending_transaction(session, transaction,
                                           dict(request.query_params))
    except Exception as exc:                              # noqa: BLE001
        logger.warning("return leg failed",
                       extra={"gateway": transaction.gateway_code,
                              "transaction_id": transaction.id,
                              "operation": "return_leg", "status": "error",
                              "error": str(exc)[:300]})
    return RedirectResponse(f"/transactions/{transaction.id}", status_code=303)


@router.get("/{transaction_id}/hpp", response_model=HppHandoff)
async def hpp_handoff(transaction_id: str, _: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)) -> HppHandoff:
    """What the browser needs to continue a hosted checkout.

    Adyen's Sessions flow and HyperPay's Copy&Pay hand back an identifier for their
    own browser component rather than a URL to redirect to, so the front end mounts
    that component in a page of ours. This returns the client-side parameters for it —
    never a credential.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such transaction.")
    if transaction.integration_type != IntegrationType.HPP.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That transaction is not a hosted checkout.")

    context = dict(transaction.context or {})
    return HppHandoff(
        transaction_id=transaction.id,
        gateway_code=transaction.gateway_code,
        status=transaction.status,
        mode=str(context.get("mode") or "redirect"),
        redirect_url=context.get("redirect_url"),
        return_url=f"/api/v1/transactions/{transaction.id}/return",
        environment=transaction.environment,
        amount=transaction.amount,
        currency=transaction.currency,
        widget=dict(context.get("adapter_context") or {}))


@router.get("/{transaction_id}/three-ds", response_model=ThreeDsChallenge)
async def three_ds_challenge(transaction_id: str, _: User = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)
                             ) -> ThreeDsChallenge:
    """What the browser needs to put the cardholder in front of their issuer.

    Returned rather than served as a page: an auto-submitting 3DS form is markup that
    arrived from a gateway, and rendering it inside this application's own origin
    would give somebody else's HTML this application's cookies. The front end puts it
    in a sandboxed frame instead, where it can reach the issuer and nothing else.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such transaction.")
    context = dict(transaction.context or {})
    adapter_context = dict(context.get("adapter_context") or {})
    if not context.get("awaiting_customer_action"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That transaction is not waiting for a 3DS challenge.")

    challenge_url = adapter_context.get("challenge_url")
    has_form = bool(adapter_context.get("challenge_html"))
    if not challenge_url and not has_form:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The gateway said a challenge was required but returned nowhere to "
                   "send the cardholder. The full response is on the transaction page, "
                   "under the authentication call.")
    return ThreeDsChallenge(
        transaction_id=transaction.id, gateway_code=transaction.gateway_code,
        status=transaction.status,
        mode="redirect" if challenge_url else "form",
        # The markup itself is never returned here — the browser is sent to the
        # sandboxed document that serves it, so it never enters this application's page.
        challenge_url=(challenge_url
                       or f"/api/v1/transactions/{transaction.id}/three-ds/challenge"),
        return_url=f"/api/v1/transactions/{transaction.id}/return",
        amount=transaction.amount, currency=transaction.currency,
        environment=transaction.environment)


@router.get("/{transaction_id}/three-ds/challenge", include_in_schema=False)
async def three_ds_challenge_document(transaction_id: str,
                                      session: AsyncSession = Depends(get_session)
                                      ) -> HTMLResponse:
    """Serve the issuer's challenge form as a page of its own, not inside a frame.

    A 3DS challenge ends by sending the browser back to this application's return URL.
    In an iframe that return is blocked by our own ``X-Frame-Options: DENY``, and the
    cardholder is left staring at an empty box — which is exactly what happened. So the
    challenge runs at the top level instead, where the return is an ordinary navigation
    and the header never applies.

    Serving somebody else's markup from our own host would normally hand it our origin.
    ``Content-Security-Policy: sandbox`` without ``allow-same-origin`` prevents that:
    the document lands in an opaque origin, so it can post itself to the issuer and
    navigate, and it cannot read this application's cookies, storage or DOM.

    Unauthenticated for the same reason the return leg is — the cardholder arrives here
    by navigation, carrying no session token — and it answers only while the
    transaction is actually waiting for a challenge.
    """
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such transaction.")
    context = dict(transaction.context or {})
    if not context.get("awaiting_customer_action"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That transaction is not waiting for a 3DS challenge.")

    html = (dict(context.get("adapter_context") or {})).get("challenge_html")
    if not html:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The gateway returned no challenge form for this "
                                   "payment.")
    return HTMLResponse(html, headers={
        # No allow-same-origin: that is the whole point. allow-top-navigation lets the
        # issuer send the browser back to us when the cardholder is done.
        "Content-Security-Policy":
            "sandbox allow-forms allow-scripts allow-top-navigation allow-popups",
        # These override the defaults the middleware would otherwise set.
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    })


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
        payment_mode: Optional[str] = Query(None, pattern="^(standard|store_card|token)$"),
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
        payment_mode=payment_mode,
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
