"""
Inbound callback handling (§4k).

The order of operations matters and is deliberate:

1. **Store the raw payload first**, before verification or interpretation. A
   callback that fails signature checking is evidence, not garbage -- throwing
   it away is how "the gateway says it sent a callback and we have no record"
   becomes unanswerable.
2. **Verify the signature** where the adapter can. Where the algorithm is not
   documented, ``signature_valid`` is left NULL, meaning "not verifiable on
   this build" -- distinct from both valid and forged. A fabricated boolean
   would be worse than an honest null.
3. **Match to a transaction** by merchant reference or gateway order id.
4. **Detect replays.** A repeated event reference, or a callback for an already
   terminal transaction, is recorded and linked to the original rather than
   re-applied. A duplicate callback must never double-process.
5. **Reconcile against an authenticated call.** Because the callback's
   authenticity cannot always be established, the transaction's status is
   confirmed with an order query rather than taken from the payload -- §4a is
   explicit that the redirect return is not authoritative, and the same caution
   applies to an unverified callback.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateways.base import WebhookEvent
from app.gateways.errors import DocumentationRequiredError, UnsupportedOperationError
from app.logging_setup import get_logger
from app.models import (
    Gateway, TERMINAL_STATUSES, Transaction, TransactionStatus, WebhookReceived,
)
from app.redaction import redact_body, redact_headers
from app.services.execution import build_adapter
from app.timeutil import utcnow

logger = get_logger(__name__)


async def store_webhook(
    session: AsyncSession,
    *,
    gateway: Gateway,
    headers: Mapping[str, str],
    body_bytes: bytes,
    body_json: Any,
) -> WebhookReceived:
    """Persist the raw callback before anything is decided about it."""
    record = WebhookReceived(
        gateway_id=gateway.id,
        headers_json=redact_headers(headers),
        body_json=(
            redact_body(body_json)
            if body_json is not None
            else {"_raw": body_bytes.decode("utf-8", errors="replace")[:20_000]}
        ),
        signature_valid=None,
        received_at=utcnow(),
    )
    session.add(record)
    await session.flush()
    return record


async def verify_signature(
    session: AsyncSession,
    *,
    gateway: Gateway,
    headers: Mapping[str, str],
    body_bytes: bytes,
) -> Optional[bool]:
    """
    Verify the callback signature, or report that it cannot be verified.

    Returns ``True``/``False`` when the adapter can decide, and ``None`` when
    the verification scheme is undocumented for this gateway. ``None`` is not a
    pass -- callers treat it as "do not trust this payload", which is why
    reconciliation always goes through an authenticated order query.
    """
    try:
        adapter = await build_adapter(session, gateway, transaction_id=None)
        return await adapter.verify_webhook(headers, body_bytes)
    except (DocumentationRequiredError, UnsupportedOperationError) as exc:
        logger.warning(
            "callback signature could not be verified",
            extra={"gateway": gateway.code, "reason": exc.code},
        )
        return None
    except Exception:  # noqa: BLE001
        logger.exception(
            "callback signature verification raised",
            extra={"gateway": gateway.code},
        )
        return False


async def match_transaction(
    session: AsyncSession, *, gateway: Gateway, event: WebhookEvent
) -> Optional[Transaction]:
    """Find the transaction a callback refers to, most specific match first."""
    if event.merchant_reference:
        result = await session.execute(
            select(Transaction).where(
                Transaction.gateway_id == gateway.id,
                Transaction.merchant_reference == event.merchant_reference,
            )
        )
        found = result.scalar_one_or_none()
        if found is not None:
            return found

    if event.gateway_order_id:
        result = await session.execute(
            select(Transaction).where(
                Transaction.gateway_id == gateway.id,
                Transaction.gateway_order_id == event.gateway_order_id,
            )
        )
        found = result.scalar_one_or_none()
        if found is not None:
            return found

    return None


async def find_duplicate(
    session: AsyncSession, *, gateway: Gateway, event: WebhookEvent,
    exclude_id: int,
) -> Optional[WebhookReceived]:
    """An earlier callback carrying the same gateway event reference."""
    if not event.event_reference:
        return None
    result = await session.execute(
        select(WebhookReceived)
        .where(
            WebhookReceived.gateway_id == gateway.id,
            WebhookReceived.event_reference == event.event_reference,
            WebhookReceived.id != exclude_id,
        )
        .order_by(WebhookReceived.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def process_webhook(
    session: AsyncSession,
    *,
    gateway: Gateway,
    headers: Mapping[str, str],
    body_bytes: bytes,
    body_json: Any,
) -> WebhookReceived:
    """
    Full callback pipeline. Always returns a stored record.

    Never raises for a bad payload: a gateway that receives a 500 will retry,
    and retrying a callback we have already stored adds noise without adding
    information. Problems are recorded on the row instead.
    """
    record = await store_webhook(
        session, gateway=gateway, headers=headers,
        body_bytes=body_bytes, body_json=body_json,
    )
    record.signature_valid = await verify_signature(
        session, gateway=gateway, headers=headers, body_bytes=body_bytes
    )

    try:
        adapter = await build_adapter(session, gateway, transaction_id=None)
        event = adapter.parse_webhook(body_json)
    except Exception:  # noqa: BLE001
        logger.exception("failed to parse callback", extra={"gateway": gateway.code})
        await session.flush()
        return record

    record.event_reference = event.event_reference
    transaction = await match_transaction(session, gateway=gateway, event=event)
    record.matched_transaction_id = transaction.id if transaction else None

    duplicate_of = await find_duplicate(
        session, gateway=gateway, event=event, exclude_id=record.id
    )
    if duplicate_of is not None:
        record.duplicate_of_id = duplicate_of.id
        logger.info(
            "duplicate callback ignored",
            extra={"gateway": gateway.code, "original_id": duplicate_of.id,
                   "event_reference": event.event_reference},
        )
        await session.flush()
        return record

    if transaction is None:
        logger.warning(
            "callback matched no transaction",
            extra={"gateway": gateway.code,
                   "merchant_reference": event.merchant_reference,
                   "gateway_order_id": event.gateway_order_id},
        )
        await session.flush()
        return record

    await apply_event(session, gateway=gateway, transaction=transaction, event=event)
    await session.flush()
    return record


async def apply_event(
    session: AsyncSession,
    *,
    gateway: Gateway,
    transaction: Transaction,
    event: WebhookEvent,
) -> None:
    """
    Update a transaction from a callback, idempotently.

    A transaction already in a terminal state is left alone: replaying a
    callback must not re-apply it (§4k). Identifiers are still backfilled,
    because learning the gateway's order id from a callback is useful even when
    the status is settled.
    """
    if event.gateway_order_id and not transaction.gateway_order_id:
        transaction.gateway_order_id = event.gateway_order_id
    if event.gateway_transaction_id and not transaction.gateway_transaction_id:
        transaction.gateway_transaction_id = event.gateway_transaction_id

    if TransactionStatus(transaction.status) in TERMINAL_STATUSES:
        logger.info(
            "callback for an already-settled transaction; identifiers "
            "backfilled, status left unchanged",
            extra={"transaction_id": transaction.id, "status": transaction.status},
        )
        await session.flush()
        return

    # §4a: neither the redirect return nor an unverified callback is
    # authoritative. Confirm with an authenticated order query, and fall back
    # to the callback's own status only if the query is impossible.
    resolved = await _confirm_by_query(session, gateway, transaction)
    if resolved is None and event.status:
        adapter_class = type(await build_adapter(session, gateway, transaction_id=None))
        normalise = getattr(adapter_class, "normalise_status", None)
        resolved = normalise(event.status) if normalise else None
        if resolved is not None:
            logger.info(
                "order query unavailable; using the callback's own status",
                extra={"transaction_id": transaction.id, "status": resolved.value},
            )

    if resolved is not None:
        transaction.status = resolved.value
        if transaction.completed_at is None:
            transaction.completed_at = utcnow()
    if event.error_code and event.error_code != "000":
        transaction.error_code = event.error_code
        transaction.error_message = event.error_message
    await session.flush()


async def _confirm_by_query(
    session: AsyncSession, gateway: Gateway, transaction: Transaction
) -> Optional[TransactionStatus]:
    if not (transaction.gateway_order_id or transaction.gateway_transaction_id):
        return None
    try:
        adapter = await build_adapter(session, gateway, transaction_id=transaction.id)
        result = await adapter.query_order(transaction)
    except (UnsupportedOperationError, DocumentationRequiredError):
        return None
    except Exception:  # noqa: BLE001
        logger.exception(
            "order query during callback reconciliation failed",
            extra={"transaction_id": transaction.id},
        )
        return None
    # The query is itself a logged HTTP call and counts toward the
    # transaction's round-trip total.
    transaction.request_count += result.request_count
    return result.status
