"""
Operation execution: idempotency, adapter invocation, finalisation (§0.6, §6).

Every gateway operation in this platform goes through :func:`execute`. It is
responsible for the guarantees the rest of the system relies on:

* **Exactly one transaction per idempotency key.** Enforced by a unique
  constraint and by handling the resulting IntegrityError, so a double-click or
  a client retry cannot produce two charges even when both requests race.
* **The transaction row exists before the first outbound call.** Committed up
  front, so a process that dies mid-call still leaves a row -- and the shared
  HTTP client's log rows point at it.
* **Every exit path finalises the row.** Success, decline, timeout, network
  failure, parse failure, unhandled exception: all of them set a terminal
  status, an error code, timings and ``request_count``. No path leaves a
  transaction stuck in PENDING with nothing explaining why (§6).
* **A timeout triggers reconciliation.** Rather than giving up, the platform
  asks the gateway what actually happened via an order query (§4j), because a
  timed-out payment may well have succeeded.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.gateways.base import GatewayAdapter, GatewayContext
from app.gateways.errors import (
    GatewayError, GatewayTimeoutError, UnsupportedOperationError,
)
from app.gateways.registry import UnknownGateway, get_adapter_class
from app.gateways.results import BaseResult, OrderStatusResult, PaymentResult, TokenResult
from app.logging_setup import get_logger
from app.models import (
    Gateway, IntegrationType, Operation, Token, Transaction, TransactionStatus,
)
from app.security.credentials import CredentialStore
from app.timeutil import duration_ms, utcnow

logger = get_logger(__name__)

#: Callback path templates. Absolute URLs are built from PUBLIC_BASE_URL by
#: :func:`build_urls` -- the only place in the codebase that constructs a
#: gateway-facing URL, so a staging deploy on another domain needs no change.
CALLBACK_PATH = "/api/webhooks/{gateway_code}"
RETURN_PATH = "/api/payments/return/{gateway_code}"


def new_merchant_reference() -> str:
    """Our own reference, always set before the first call reaches a gateway."""
    return f"BP-{uuid.uuid4().hex[:20].upper()}"


def build_urls(gateway_code: str) -> tuple[str, str]:
    """Return ``(return_url, callback_url)`` built from PUBLIC_BASE_URL."""
    settings = get_settings()
    return (
        settings.callback_url(RETURN_PATH.format(gateway_code=gateway_code)),
        settings.callback_url(CALLBACK_PATH.format(gateway_code=gateway_code)),
    )


async def build_context(
    session: AsyncSession,
    gateway: Gateway,
    *,
    transaction_id: Optional[int] = None,
    with_urls: bool = True,
) -> GatewayContext:
    """
    Assemble everything the adapter needs, resolving credentials at call time.

    The decrypted credentials live only on the returned context, which is
    discarded when the request ends (§7b).
    """
    store = CredentialStore(session)
    credentials = await store.resolve(gateway.id)
    is_production = await store.is_production(gateway.id)
    settings = get_settings()

    return_url = callback_url = None
    if with_urls:
        return_url, callback_url = build_urls(gateway.code)

    return GatewayContext(
        gateway_id=gateway.id,
        gateway_code=gateway.code,
        display_name=gateway.display_name,
        credentials=credentials,
        config=gateway.config_json or {},
        timeout_seconds=settings.HTTP_TIMEOUT_SECONDS,
        is_production=is_production,
        allow_production=settings.ALLOW_PRODUCTION_GATEWAYS,
        transaction_id=transaction_id,
        return_url=return_url,
        callback_url=callback_url,
    )


async def build_adapter(
    session: AsyncSession,
    gateway: Gateway,
    *,
    transaction_id: Optional[int] = None,
) -> GatewayAdapter:
    adapter_class = get_adapter_class(gateway.code)
    context = await build_context(session, gateway, transaction_id=transaction_id)
    return adapter_class(context)


# ---------------------------------------------------------------------------
# Idempotent transaction creation
# ---------------------------------------------------------------------------

async def get_or_create_transaction(
    session: AsyncSession,
    *,
    gateway: Gateway,
    integration_type: IntegrationType,
    operation: Operation,
    idempotency_key: str,
    amount_minor: int,
    currency: str,
    merchant_reference: Optional[str] = None,
    parent: Optional[Transaction] = None,
    token_reference: Optional[str] = None,
) -> tuple[Transaction, bool]:
    """
    Return ``(transaction, created)`` for an idempotency key (§0.6).

    The SELECT is an optimisation; the unique constraint is the guarantee. Two
    concurrent requests with the same key both pass the SELECT, one INSERT
    wins, and the loser re-reads the winner's row instead of creating a second
    transaction.
    """
    # Read the identifiers now, before any flush can fail. A rollback expires
    # every object in the session, and touching `gateway.id` afterwards would
    # trigger an implicit lazy load from a context that cannot perform IO --
    # which turns the recovery path into a crash exactly when it is needed.
    gateway_id = gateway.id
    gateway_code = gateway.code
    integration_type_id = integration_type.id

    existing = await session.execute(
        select(Transaction).where(
            Transaction.gateway_id == gateway_id,
            Transaction.idempotency_key == idempotency_key,
        )
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return found, False

    parent_id = parent.id if parent else None
    parent_card = (
        (
            parent.card_brand, parent.card_last4, parent.card_exp_month,
            parent.card_exp_year, parent.gateway_order_id,
        )
        if parent is not None
        else None
    )

    transaction = Transaction(
        gateway_id=gateway_id,
        integration_type_id=integration_type_id,
        operation=operation.value,
        parent_transaction_id=parent_id,
        status=TransactionStatus.PENDING.value,
        amount_minor=amount_minor,
        currency=currency,
        merchant_reference=merchant_reference or new_merchant_reference(),
        idempotency_key=idempotency_key,
        token_reference=token_reference,
        started_at=utcnow(),
        request_count=0,
    )
    if parent_card is not None:
        # Carry the card identity down the chain so a refund row shows which
        # card it refunded without a join the UI would otherwise need.
        (
            transaction.card_brand, transaction.card_last4,
            transaction.card_exp_month, transaction.card_exp_year,
            transaction.gateway_order_id,
        ) = parent_card

    session.add(transaction)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        retry = await session.execute(
            select(Transaction).where(
                Transaction.gateway_id == gateway_id,
                Transaction.idempotency_key == idempotency_key,
            )
        )
        winner = retry.scalar_one_or_none()
        if winner is None:
            # The constraint fired for some other reason; surfacing it is
            # correct -- swallowing it would hide a real schema problem.
            raise
        logger.info(
            "idempotency key already in flight; returning the existing transaction",
            extra={"gateway": gateway_code, "idempotency_key": idempotency_key,
                   "transaction_id": winner.id},
        )
        return winner, False

    await session.commit()
    return transaction, True


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

Runner = Callable[[GatewayAdapter], Awaitable[BaseResult]]


async def execute(
    session: AsyncSession,
    *,
    gateway: Gateway,
    transaction: Transaction,
    runner: Runner,
    reconcile_on_timeout: bool = True,
) -> Transaction:
    """
    Run one adapter operation against an existing PENDING transaction.

    ``runner`` receives the adapter and returns a result. Everything else --
    error classification, timing, call counting, reconciliation, persistence --
    happens here, once, so no operation can implement it slightly differently.
    """
    if transaction.status != TransactionStatus.PENDING.value:
        # Idempotent replay: the work is already done. Returning the settled
        # row is the correct answer to a repeated request (§0.6).
        return transaction

    adapter = await build_adapter(session, gateway, transaction_id=transaction.id)
    started = transaction.started_at or utcnow()

    try:
        result = await runner(adapter)
    except GatewayTimeoutError as exc:
        logger.warning(
            "gateway call timed out",
            extra={"gateway": gateway.code, "transaction_id": transaction.id,
                   "timeout_seconds": exc.timeout_seconds},
        )
        await _finalise_failure(session, transaction, exc, started)
        if reconcile_on_timeout:
            # §4j: a timeout must not leave a transaction stuck. Ask the
            # gateway what actually happened before giving up.
            await reconcile_after_timeout(session, gateway, transaction)
        return transaction
    except (GatewayError, UnknownGateway) as exc:
        await _finalise_failure(session, transaction, exc, started)
        return transaction
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        # An unexpected exception must still finalise the row. A transaction
        # left PENDING because of a bug in our own code is the exact failure
        # §6 forbids.
        logger.exception(
            "unhandled error during gateway operation",
            extra={"gateway": gateway.code, "transaction_id": transaction.id},
        )
        await _finalise_failure(session, transaction, exc, started)
        return transaction

    await _finalise_success(session, transaction, result, started)
    return transaction


async def _finalise_success(
    session: AsyncSession,
    transaction: Transaction,
    result: BaseResult,
    started: Any,
) -> None:
    completed = utcnow()
    transaction.completed_at = completed
    transaction.duration_ms = duration_ms(started, completed)
    # Authoritative count, derived from the calls the shared client actually
    # made. The client also increments live, which is what keeps a crashed
    # operation's count honest.
    transaction.request_count = result.request_count

    if isinstance(result, (PaymentResult, TokenResult, OrderStatusResult)):
        transaction.status = getattr(result, "status", TransactionStatus.SUCCESS).value
        transaction.error_code = getattr(result, "error_code", None)
        transaction.error_message = getattr(result, "error_message", None)
    else:
        transaction.status = TransactionStatus.SUCCESS.value

    for attribute in ("gateway_order_id", "gateway_transaction_id", "token_reference"):
        value = getattr(result, attribute, None)
        if value:
            setattr(transaction, attribute, value)

    card = getattr(result, "card", None)
    if card is not None:
        transaction.card_brand = card.brand or transaction.card_brand
        transaction.card_last4 = card.last4 or transaction.card_last4
        transaction.card_exp_month = card.exp_month or transaction.card_exp_month
        transaction.card_exp_year = card.exp_year or transaction.card_exp_year

    await session.flush()
    await _apply_to_parent(session, transaction)
    await session.commit()


async def _finalise_failure(
    session: AsyncSession,
    transaction: Transaction,
    exc: BaseException,
    started: Any,
) -> None:
    completed = utcnow()
    transaction.completed_at = completed
    transaction.duration_ms = duration_ms(started, completed)
    transaction.status = getattr(exc, "status", TransactionStatus.ERROR.value)
    transaction.error_code = getattr(exc, "code", "internal_error")
    # str(exc) rather than repr(): the message is shown to an operator, and a
    # repr of a card-carrying exception is exactly what §0.8 guards against.
    transaction.error_message = str(exc)[:4000]

    # Trust the live count the shared client kept, since no result object
    # exists on this path. Refresh so the in-memory row reflects the
    # increments made on the client's own connection.
    await session.refresh(transaction, attribute_names=["request_count"])
    await session.commit()


async def _apply_to_parent(session: AsyncSession, transaction: Transaction) -> None:
    """
    Roll a successful capture or refund into the parent's running totals (§4d, §4e).

    Kept here rather than in the adapter so every gateway gets the same
    accounting, and so the ceilings the UI enforces come from one place.
    """
    if transaction.status != TransactionStatus.SUCCESS.value:
        return
    if transaction.parent_transaction_id is None:
        return

    parent = await session.get(Transaction, transaction.parent_transaction_id)
    if parent is None:
        return

    if transaction.operation in (Operation.CAPTURE.value, Operation.PARTIAL_CAPTURE.value):
        parent.captured_amount_minor += transaction.amount_minor
    elif transaction.operation in (Operation.REFUND.value, Operation.PARTIAL_REFUND.value):
        parent.refunded_amount_minor += transaction.amount_minor
    elif transaction.operation == Operation.VOID.value:
        # A void removes the authorisation entirely: nothing further is
        # capturable against it.
        parent.captured_amount_minor = parent.amount_minor
        parent.status = TransactionStatus.SUCCESS.value
    await session.flush()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def reconcile_after_timeout(
    session: AsyncSession, gateway: Gateway, transaction: Transaction
) -> Optional[TransactionStatus]:
    """
    Resolve a timed-out transaction with a single order query (§4j).

    Deliberately one attempt, not a retry loop: the goal is that a timeout
    never leaves an unexplained PENDING row, not to poll a gateway until it
    agrees with us. If the query itself fails, the TIMEOUT status stands and
    both the timeout and the failed query are in ``api_call_logs``.
    """
    if not transaction.gateway_order_id and not transaction.gateway_transaction_id:
        logger.info(
            "cannot reconcile timed-out transaction: no gateway order id was "
            "returned before the timeout",
            extra={"transaction_id": transaction.id},
        )
        return None

    try:
        adapter = await build_adapter(session, gateway, transaction_id=transaction.id)
        status_result = await adapter.query_order(transaction)
    except UnsupportedOperationError:
        logger.info(
            "gateway does not support order query; timed-out transaction left as TIMEOUT",
            extra={"gateway": gateway.code, "transaction_id": transaction.id},
        )
        return None
    except Exception:  # noqa: BLE001
        logger.exception(
            "reconciliation query failed; leaving transaction as TIMEOUT",
            extra={"gateway": gateway.code, "transaction_id": transaction.id},
        )
        return None

    transaction.status = status_result.status.value
    transaction.request_count += status_result.request_count
    if status_result.gateway_order_id:
        transaction.gateway_order_id = status_result.gateway_order_id
    if status_result.gateway_transaction_id:
        transaction.gateway_transaction_id = status_result.gateway_transaction_id
    transaction.error_message = (
        f"Call timed out; resolved by order query to "
        f"{status_result.gateway_status or status_result.status.value}."
    )
    await session.flush()
    await _apply_to_parent(session, transaction)
    await session.commit()
    logger.info(
        "reconciled timed-out transaction",
        extra={"transaction_id": transaction.id, "status": transaction.status},
    )
    return status_result.status


async def record_token(
    session: AsyncSession,
    *,
    gateway: Gateway,
    transaction: Transaction,
    result: TokenResult,
) -> Optional[Token]:
    """Persist a token reference produced by a tokenize or save-card flow."""
    if not result.token_reference:
        return None
    existing = await session.execute(
        select(Token).where(
            Token.gateway_id == gateway.id,
            Token.token_reference == result.token_reference,
        )
    )
    token = existing.scalar_one_or_none()
    if token is not None:
        return token

    card = result.card
    token = Token(
        gateway_id=gateway.id,
        token_reference=result.token_reference,
        agreement_reference=result.agreement_reference,
        card_brand=card.brand if card else None,
        card_last4=card.last4 if card else None,
        card_exp_month=card.exp_month if card else None,
        card_exp_year=card.exp_year if card else None,
        created_from_transaction_id=transaction.id,
        is_active=True,
    )
    session.add(token)
    await session.flush()
    return token
