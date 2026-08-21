"""
Payment operations (§4, §5).

Every endpoint here follows the same shape, and the shape is the point:

1. Resolve the gateway and integration type.
2. Apply the server-side gates -- raw card entry, production credentials.
3. Create (or find) the transaction for the request's idempotency key.
4. Hand the adapter to :func:`app.services.execution.execute`, which owns
   timing, call counting, error classification and finalisation.
5. Return the transaction.

No endpoint talks to a gateway itself, builds a URL itself, or decides what a
failure means. That is what keeps every operation comparable, which is what
makes the benchmark worth anything.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.gateways.base import Capability
from app.gateways.errors import InvalidTransactionState
from app.gateways.registry import adapter_class_or_none
from app.gateways.results import OrderContext, RawCard
from app.models import (
    Gateway, IntegrationType, IntegrationTypeCode, Operation, Token, Transaction,
    TransactionStatus,
)
from app.schemas.payments import (
    CaptureRequest, DirectPaymentRequest, HPPSessionRequest, RefundRequest,
    TokenChargeRequest, TokenizeRequest, VoidRequest,
)
from app.schemas.transactions import HPPSessionOut, TransactionOut
from app.security.auth import require_user
from app.security.gates import require_direct_card_entry
from app.services import execution
from app.services.serialize import transaction_out

router = APIRouter(prefix="/api/payments", tags=["payments"])


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

async def _gateway(session: AsyncSession, code: str) -> Gateway:
    result = await session.execute(select(Gateway).where(Gateway.code == code))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown gateway {code!r}.")
    if not gateway.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Gateway {code!r} is disabled."
        )
    return gateway


async def _integration_type(session: AsyncSession, code: IntegrationTypeCode) -> IntegrationType:
    result = await session.execute(
        select(IntegrationType).where(IntegrationType.code == code.value)
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Integration type {code.value!r} is missing; bootstrap did not run.",
        )
    return integration


def _require_capability(gateway: Gateway, capability: Capability) -> None:
    """
    Refuse an operation the adapter does not implement, before touching the DB.

    The frontend already hides these controls -- this is the server-side half,
    so calling the API directly gets the same typed refusal (§3).
    """
    adapter_class = adapter_class_or_none(gateway.code)
    if adapter_class is None:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            f"No adapter is registered for gateway {gateway.code!r}.",
        )
    if capability not in adapter_class.capabilities:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            {
                "code": "unsupported_operation",
                "gateway": gateway.code,
                "operation": capability.value,
                "message": (
                    f"{gateway.display_name} does not support "
                    f"{capability.value.replace('_', ' ')}."
                ),
                "doc_url": adapter_class.doc_urls.get(capability.value),
            },
        )


async def _serialise(session: AsyncSession, transaction: Transaction) -> TransactionOut:
    gateways = {
        row.id: row for row in (await session.execute(select(Gateway))).scalars()
    }
    integration_types = {
        row.id: row
        for row in (await session.execute(select(IntegrationType))).scalars()
    }
    return transaction_out(
        transaction, gateways=gateways, integration_types=integration_types
    )


def _order(
    transaction: Transaction, gateway: Gateway, *, customer_email: Optional[str] = None
) -> OrderContext:
    """
    Build the adapter's order context.

    The return and callback URLs come from ``PUBLIC_BASE_URL`` here, in the
    service layer, so no adapter is ever in a position to hardcode a domain.
    """
    return_url, callback_url = execution.build_urls(gateway.code)
    return OrderContext(
        merchant_reference=transaction.merchant_reference,
        amount_minor=transaction.amount_minor,
        currency=transaction.currency,
        return_url=return_url,
        callback_url=callback_url,
        customer_email=customer_email,
    )


def _raw_card(payload) -> RawCard:
    return RawCard(
        number=payload.number,
        exp_month=payload.exp_month,
        exp_year=payload.exp_year,
        cvv=payload.cvv,
        holder_name=payload.holder_name,
    )


# ---------------------------------------------------------------------------
# (a) Hosted payment page
# ---------------------------------------------------------------------------

@router.post("/{code}/hpp/session", response_model=HPPSessionOut)
async def create_hpp_session(
    code: str,
    payload: HPPSessionRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> HPPSessionOut:
    """
    Create a hosted checkout session and return the redirect URL (§4a).

    The transaction stays PENDING until it is reconciled -- neither the browser
    return nor the callback is trusted on its own.
    """
    gateway = await _gateway(session, code)
    _require_capability(gateway, Capability.CREATE_HPP_SESSION)
    integration = await _integration_type(session, IntegrationTypeCode.HPP)

    transaction, _ = await execution.get_or_create_transaction(
        session,
        gateway=gateway,
        integration_type=integration,
        operation=Operation.SALE,
        idempotency_key=payload.idempotency_key,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
    )

    holder: dict[str, object] = {}

    async def runner(adapter):
        result = await adapter.create_hpp_session(
            _order(transaction, gateway, customer_email=payload.customer_email)
        )
        holder["result"] = result
        return result

    await execution.execute(
        session, gateway=gateway, transaction=transaction, runner=runner
    )

    result = holder.get("result")
    # An HPP session that succeeded is still PENDING as a payment: the customer
    # has not paid yet. Only reconciliation moves it off PENDING.
    if result is not None and transaction.status == TransactionStatus.SUCCESS.value:
        transaction.status = TransactionStatus.PENDING.value
        transaction.completed_at = None
        await session.commit()

    return HPPSessionOut(
        transaction=await _serialise(session, transaction),
        redirect_url=getattr(result, "redirect_url", None),
        session_id=getattr(result, "session_id", None),
        redirect_field=getattr(result, "redirect_field", None),
    )


# ---------------------------------------------------------------------------
# (b, c) Direct API
# ---------------------------------------------------------------------------

@router.post("/{code}/direct/sale", response_model=TransactionOut)
async def direct_sale(
    code: str,
    payload: DirectPaymentRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """Direct API sale with a raw test card (§4b). Gated by ALLOW_DIRECT_CARD_ENTRY."""
    require_direct_card_entry()
    return await _direct(session, code, payload, Operation.SALE)


@router.post("/{code}/direct/auth", response_model=TransactionOut)
async def direct_auth(
    code: str,
    payload: DirectPaymentRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """
    Pre-authorisation, no capture (§4c).

    Geidea documents pre-auth as Saudi-platform-only. A merchant account
    without it produces a "not supported for this merchant" error carrying the
    gateway's own code, not a generic failure.
    """
    require_direct_card_entry()
    return await _direct(session, code, payload, Operation.AUTH)


async def _direct(
    session: AsyncSession,
    code: str,
    payload: DirectPaymentRequest,
    operation: Operation,
) -> TransactionOut:
    gateway = await _gateway(session, code)
    capability = (
        Capability.DIRECT_API_SALE
        if operation is Operation.SALE
        else Capability.DIRECT_API_AUTH
    )
    _require_capability(gateway, capability)
    integration = await _integration_type(session, IntegrationTypeCode.DIRECT_API)

    transaction, _ = await execution.get_or_create_transaction(
        session,
        gateway=gateway,
        integration_type=integration,
        operation=operation,
        idempotency_key=payload.idempotency_key,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
    )

    card = _raw_card(payload.card)

    async def runner(adapter):
        order = _order(transaction, gateway, customer_email=payload.customer_email)
        if operation is Operation.SALE:
            return await adapter.direct_api_sale(order, card)
        return await adapter.direct_api_auth(order, card)

    await execution.execute(
        session, gateway=gateway, transaction=transaction, runner=runner
    )
    return await _serialise(session, transaction)


# ---------------------------------------------------------------------------
# (d, e, f) Capture / refund / void
# ---------------------------------------------------------------------------

async def _parent(session: AsyncSession, transaction_id: int) -> Transaction:
    parent = await session.get(Transaction, transaction_id)
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction.")
    return parent


@router.post("/transactions/{transaction_id}/capture", response_model=TransactionOut)
async def capture(
    transaction_id: int,
    payload: CaptureRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """
    Capture an authorisation, fully or partially (§4d).

    Repeat with smaller amounts for multiple partial captures. The remaining
    capturable balance is tracked on the authorisation and enforced here, so a
    capture that would exceed it is refused before it reaches the gateway.
    """
    parent = await _parent(session, transaction_id)
    gateway = await _gateway(session, (await _gateway_code(session, parent)))

    amount = payload.amount_minor
    is_partial = amount is not None and amount < parent.remaining_capturable_minor
    _require_capability(
        gateway, Capability.PARTIAL_CAPTURE if is_partial else Capability.CAPTURE
    )

    if parent.operation != Operation.AUTH.value:
        raise InvalidTransactionState(
            "Only an authorisation can be captured. This transaction is "
            f"{parent.operation}."
        )
    if parent.status != TransactionStatus.SUCCESS.value:
        raise InvalidTransactionState(
            f"This authorisation is {parent.status}, so it cannot be captured."
        )

    return await _child_operation(
        session,
        gateway=gateway,
        parent=parent,
        operation=Operation.PARTIAL_CAPTURE if is_partial else Operation.CAPTURE,
        idempotency_key=payload.idempotency_key,
        amount_minor=amount if amount is not None else parent.remaining_capturable_minor,
        runner_name="capture",
        amount_argument=amount,
    )


@router.post("/transactions/{transaction_id}/refund", response_model=TransactionOut)
async def refund(
    transaction_id: int,
    payload: RefundRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """
    Refund a captured payment, fully or partially (§4e).

    Refusing to refund an uncaptured authorisation happens here as well as in
    the UI: there is nothing to give back, and asking the gateway would only
    produce a decline that looks like a gateway problem.
    """
    parent = await _parent(session, transaction_id)
    gateway = await _gateway(session, (await _gateway_code(session, parent)))

    amount = payload.amount_minor
    is_partial = amount is not None and amount < parent.remaining_refundable_minor
    _require_capability(
        gateway, Capability.PARTIAL_REFUND if is_partial else Capability.REFUND
    )

    if parent.status != TransactionStatus.SUCCESS.value:
        raise InvalidTransactionState(
            f"This transaction is {parent.status}, so it cannot be refunded."
        )
    if parent.remaining_refundable_minor <= 0:
        raise InvalidTransactionState(
            "Nothing is refundable on this transaction. An authorisation must "
            "be captured before any part of it can be refunded."
        )

    return await _child_operation(
        session,
        gateway=gateway,
        parent=parent,
        operation=Operation.PARTIAL_REFUND if is_partial else Operation.REFUND,
        idempotency_key=payload.idempotency_key,
        amount_minor=amount if amount is not None else parent.remaining_refundable_minor,
        runner_name="refund",
        amount_argument=amount,
    )


@router.post("/transactions/{transaction_id}/void", response_model=TransactionOut)
async def void(
    transaction_id: int,
    payload: VoidRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """Void an authorisation that has not been captured (§4f)."""
    parent = await _parent(session, transaction_id)
    gateway = await _gateway(session, (await _gateway_code(session, parent)))
    _require_capability(gateway, Capability.VOID)

    if parent.operation != Operation.AUTH.value:
        raise InvalidTransactionState(
            "Only an authorisation can be voided. Refund a completed payment instead."
        )
    if parent.captured_amount_minor:
        raise InvalidTransactionState(
            "This authorisation has already been captured; refund it instead."
        )

    return await _child_operation(
        session,
        gateway=gateway,
        parent=parent,
        operation=Operation.VOID,
        idempotency_key=payload.idempotency_key,
        amount_minor=parent.amount_minor,
        runner_name="void",
        amount_argument=None,
    )


async def _gateway_code(session: AsyncSession, transaction: Transaction) -> str:
    gateway = await session.get(Gateway, transaction.gateway_id)
    if gateway is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "This transaction references a gateway that no longer exists.",
        )
    return gateway.code


async def _child_operation(
    session: AsyncSession,
    *,
    gateway: Gateway,
    parent: Transaction,
    operation: Operation,
    idempotency_key: str,
    amount_minor: int,
    runner_name: str,
    amount_argument: Optional[int],
) -> TransactionOut:
    """Create and run a capture/refund/void against an existing transaction."""
    integration = await session.get(IntegrationType, parent.integration_type_id)
    if integration is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Missing integration type."
        )

    transaction, _ = await execution.get_or_create_transaction(
        session,
        gateway=gateway,
        integration_type=integration,
        operation=operation,
        idempotency_key=idempotency_key,
        amount_minor=amount_minor,
        currency=parent.currency,
        parent=parent,
    )

    async def runner(adapter):
        method = getattr(adapter, runner_name)
        if runner_name == "void":
            return await method(parent)
        return await method(parent, amount_argument)

    await execution.execute(
        session, gateway=gateway, transaction=transaction, runner=runner
    )
    return await _serialise(session, transaction)


# ---------------------------------------------------------------------------
# (g, h, i) Token operations
# ---------------------------------------------------------------------------

@router.post("/{code}/tokenize", response_model=TransactionOut)
async def tokenize(
    code: str,
    payload: TokenizeRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """Stand-alone save-card (§4g). Stores the token reference, never the PAN."""
    require_direct_card_entry()
    gateway = await _gateway(session, code)
    _require_capability(gateway, Capability.TOKENIZE)
    integration = await _integration_type(session, IntegrationTypeCode.TOKENIZE)

    transaction, _ = await execution.get_or_create_transaction(
        session,
        gateway=gateway,
        integration_type=integration,
        operation=Operation.TOKENIZE,
        idempotency_key=payload.idempotency_key,
        amount_minor=0,
        currency="SAR",
    )
    card = _raw_card(payload.card)
    holder: dict[str, object] = {}

    async def runner(adapter):
        result = await adapter.tokenize(card)
        holder["result"] = result
        return result

    await execution.execute(
        session, gateway=gateway, transaction=transaction, runner=runner
    )
    result = holder.get("result")
    if result is not None:
        await execution.record_token(
            session, gateway=gateway, transaction=transaction, result=result
        )
        await session.commit()
    return await _serialise(session, transaction)


@router.post("/{code}/token/cit", response_model=TransactionOut)
async def charge_token_cit(
    code: str,
    payload: TokenChargeRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """Customer-initiated charge against a stored token (§4h)."""
    return await _token_charge(session, code, payload, Operation.CIT_CHARGE)


@router.post("/{code}/token/mit", response_model=TransactionOut)
async def charge_token_mit(
    code: str,
    payload: TokenChargeRequest,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """Merchant-initiated charge against a stored token, no customer present (§4i)."""
    return await _token_charge(session, code, payload, Operation.MIT_CHARGE)


async def _token_charge(
    session: AsyncSession,
    code: str,
    payload: TokenChargeRequest,
    operation: Operation,
) -> TransactionOut:
    gateway = await _gateway(session, code)
    is_mit = operation is Operation.MIT_CHARGE
    _require_capability(
        gateway,
        Capability.CHARGE_WITH_TOKEN_MIT if is_mit else Capability.CHARGE_WITH_TOKEN_CIT,
    )
    integration = await _integration_type(
        session, IntegrationTypeCode.TOKEN_MIT if is_mit else IntegrationTypeCode.TOKEN_CIT
    )

    result = await session.execute(
        select(Token).where(
            Token.gateway_id == gateway.id,
            Token.token_reference == payload.token_reference,
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No stored token {payload.token_reference!r} for {gateway.display_name}.",
        )
    if not token.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "That token is no longer active.")

    transaction, _ = await execution.get_or_create_transaction(
        session,
        gateway=gateway,
        integration_type=integration,
        operation=operation,
        idempotency_key=payload.idempotency_key,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        token_reference=token.token_reference,
    )

    async def runner(adapter):
        order = _order(transaction, gateway)
        if is_mit:
            return await adapter.charge_with_token_mit(token, order)
        return await adapter.charge_with_token_cit(token, order)

    await execution.execute(
        session, gateway=gateway, transaction=transaction, runner=runner
    )
    return await _serialise(session, transaction)


# ---------------------------------------------------------------------------
# (j) Order query
# ---------------------------------------------------------------------------

@router.post("/transactions/{transaction_id}/query", response_model=TransactionOut)
async def query_order(
    transaction_id: int,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> TransactionOut:
    """
    Ask the gateway for a transaction's authoritative status (§4j).

    Does not create a new transaction row: the query is a reconciliation of an
    existing one, and its HTTP call is added to that transaction's audit trail
    and round-trip count.
    """
    transaction = await _parent(session, transaction_id)
    gateway = await _gateway(session, await _gateway_code(session, transaction))
    _require_capability(gateway, Capability.QUERY_ORDER)

    adapter = await execution.build_adapter(
        session, gateway, transaction_id=transaction.id
    )
    result = await adapter.query_order(transaction)

    transaction.status = result.status.value
    transaction.request_count += result.request_count
    if result.gateway_order_id:
        transaction.gateway_order_id = result.gateway_order_id
    if result.gateway_transaction_id:
        transaction.gateway_transaction_id = result.gateway_transaction_id
    await session.commit()
    return await _serialise(session, transaction)
