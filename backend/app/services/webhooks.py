"""
Inbound callback handling (§4k).

The order of operations matters and is deliberate:

1. **Store the raw payload first**, before verification or interpretation. A
   callback that fails signature checking is evidence, not garbage -- discarding
   it is how "the gateway says it sent a callback and we have no record" becomes
   unanswerable.
2. **Verify the signature.** For Geidea this is a real HMAC-SHA256 check over
   the documented field list (docs/geidea/04). ``signature_valid`` is ``True``,
   ``False``, or ``NULL`` when the gateway's adapter cannot verify at all --
   three distinct states, none of them fabricated.
3. **Refuse to act on an unverified payload.** A callback whose signature does
   NOT verify updates nothing. It is stored, flagged, and surfaced -- early in
   an integration it usually means a credential mismatch rather than an attack,
   which is exactly why it needs to be visible instead of silently dropped.
4. **Apply the documented success checklist**, not just the signature. A valid
   signature with ``responseCode != "000"`` is an authentic *failure* notice.
   Treating "signature valid" as "payment succeeded" would book declines as
   revenue.
5. **Detect replays.** A repeated order/status pair is recorded against the
   original rather than re-applied, and a transaction already in a terminal
   state is never re-processed.

Two lifecycle facts from the documentation shape the rest of the system:

* An order sits at ``InProgress`` while the customer is on the 3DS page or is
  retrying, and **no callback is sent during that time**. "No callback yet" is
  therefore not evidence of failure -- reconciliation polls instead of assuming.
* One callback consolidates every attempt against an order into a single
  ``transactions[]`` array. Several failed sub-transactions followed by a
  success is a *successful* payload; only the outer order status is
  authoritative.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateways.base import WebhookEvent
from app.gateways.errors import (
    CredentialsMissing, DocumentationRequiredError, UnsupportedOperationError,
)
from app.logging_setup import get_logger
from app.gateways.registry import get_adapter_class
from app.models import (
    Gateway, TERMINAL_STATUSES, Token, Transaction, TransactionStatus,
    WebhookReceived,
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
    this gateway has no verification scheme implemented, or when the credentials
    needed to verify are missing. ``None`` is never treated as a pass: it routes
    reconciliation through an authenticated order query instead.
    """
    try:
        adapter = await build_adapter(session, gateway, transaction_id=None)
        return await adapter.verify_webhook(headers, body_bytes)
    except (DocumentationRequiredError, UnsupportedOperationError, CredentialsMissing) as exc:
        # Cannot verify -- distinct from verifying and failing.
        logger.warning(
            "callback signature could not be checked",
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

    await apply_event(
        session,
        gateway=gateway,
        transaction=transaction,
        event=event,
        signature_valid=record.signature_valid,
        body_json=body_json,
    )
    await session.flush()
    return record


async def apply_event(
    session: AsyncSession,
    *,
    gateway: Gateway,
    transaction: Transaction,
    event: WebhookEvent,
    signature_valid: Optional[bool],
    body_json: Any,
) -> None:
    """
    Update a transaction from a callback, idempotently and only when trusted.

    The signature decides whether the payload may change anything:

    * ``False`` -- verified and wrong. Nothing is updated, not even an
      identifier. Recorded for investigation.
    * ``True``  -- authentic. The documented success checklist then decides
      SUCCESS versus FAILED.
    * ``None``  -- unverifiable on this build. Identifiers are backfilled and
      the status is settled by an authenticated order query instead.

    A transaction already in a terminal state is never re-processed (§4k).
    """
    if signature_valid is False:
        logger.error(
            "callback signature did not verify; refusing to apply it. Early in "
            "an integration this usually means the wrong API password or public "
            "key is configured, not an attack.",
            extra={
                "gateway": gateway.code,
                "transaction_id": transaction.id,
                "merchant_reference": event.merchant_reference,
            },
        )
        return

    if event.gateway_order_id and not transaction.gateway_order_id:
        transaction.gateway_order_id = event.gateway_order_id
    if event.gateway_transaction_id and not transaction.gateway_transaction_id:
        transaction.gateway_transaction_id = event.gateway_transaction_id

    # A token can arrive on a callback for a transaction that is already
    # settled, so capture it before the terminal-state check.
    await _capture_token(
        session, gateway=gateway, transaction=transaction, body_json=body_json
    )

    if TransactionStatus(transaction.status) in TERMINAL_STATUSES:
        logger.info(
            "callback for an already-settled transaction; identifiers "
            "backfilled, status left unchanged",
            extra={"transaction_id": transaction.id, "status": transaction.status},
        )
        await session.flush()
        return

    resolved: Optional[TransactionStatus] = None

    if signature_valid is True:
        adapter_class = get_adapter_class(gateway.code)
        reports_success = getattr(adapter_class, "callback_reports_success", None)
        if reports_success is not None and reports_success(body_json):
            resolved = TransactionStatus.SUCCESS
        else:
            normalise = getattr(adapter_class, "normalise_status", None)
            candidate = normalise(event.status) if (normalise and event.status) else None
            # An authenticated callback that does not meet the success
            # checklist is a decline, unless the gateway says the order is
            # still in flight -- InProgress is documented as non-terminal, and
            # forcing it to FAILED would kill a payment the customer is still
            # completing.
            resolved = (
                TransactionStatus.PENDING
                if candidate is TransactionStatus.PENDING
                else TransactionStatus.FAILED
            )
            logger.info(
                "authentic callback reporting a non-success outcome",
                extra={
                    "transaction_id": transaction.id,
                    "gateway_status": event.status,
                    "error_code": event.error_code,
                },
            )

        # Amount cross-check, per the documented checklist. A mismatch means the
        # callback is authentic but does not describe this order's amount, which
        # is worth refusing to book rather than reconciling away.
        if (
            resolved is TransactionStatus.SUCCESS
            and event.amount_minor is not None
            and transaction.amount_minor
            and event.amount_minor != transaction.amount_minor
        ):
            logger.error(
                "callback amount does not match the recorded transaction amount; "
                "not marking it successful",
                extra={
                    "transaction_id": transaction.id,
                    "callback_amount_minor": event.amount_minor,
                    "transaction_amount_minor": transaction.amount_minor,
                },
            )
            resolved = None
    else:
        # Unverifiable: settle it with a call we authenticated ourselves.
        resolved = await _confirm_by_query(session, gateway, transaction)

    if resolved is not None and resolved is not TransactionStatus.PENDING:
        transaction.status = resolved.value
        if transaction.completed_at is None:
            transaction.completed_at = utcnow()

    if event.error_code and event.error_code != "000":
        transaction.error_code = event.error_code
        transaction.error_message = event.error_message
    await session.flush()


async def _capture_token(
    session: AsyncSession,
    *,
    gateway: Gateway,
    transaction: Transaction,
    body_json: Any,
) -> None:
    """
    Store a ``tokenId`` that arrived on a callback (§4g).

    A saved card is not created by the request that started the flow -- the
    customer enters the card on the gateway's page, and the token comes back
    here. The agreement identifier is the one this platform generated at
    tokenization time; the gateway does not issue one, and the exact same value
    has to be replayed on every later merchant-initiated charge.
    """
    adapter_class = get_adapter_class(gateway.code)
    extract = getattr(adapter_class, "token_from_webhook", None)
    if extract is None:
        return
    token_reference = extract(body_json)
    if not token_reference:
        return

    existing = await session.execute(
        select(Token).where(
            Token.gateway_id == gateway.id,
            Token.token_reference == token_reference,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return

    card = _card_from_callback(body_json)
    session.add(
        Token(
            gateway_id=gateway.id,
            token_reference=token_reference,
            agreement_reference=transaction.merchant_reference,
            card_brand=card.get("brand"),
            card_last4=card.get("last4"),
            card_exp_month=card.get("exp_month"),
            card_exp_year=card.get("exp_year"),
            created_from_transaction_id=transaction.id,
            is_active=True,
        )
    )
    transaction.token_reference = token_reference
    await session.flush()
    logger.info(
        "stored card token from callback",
        extra={"gateway": gateway.code, "transaction_id": transaction.id},
    )


def _card_from_callback(body_json: Any) -> dict[str, Any]:
    """
    The card attributes §0.8 permits, read from a callback's payment method.

    Only brand, last four and expiry are taken. ``maskedCardNumber`` arrives as
    ``444000******0010``; the last four are sliced from it and the rest is left
    behind.
    """
    out: dict[str, Any] = {}
    if not isinstance(body_json, dict):
        return out
    order = body_json.get("order")
    if not isinstance(order, dict):
        return out
    transactions = order.get("transactions")
    if not isinstance(transactions, list):
        return out
    for entry in transactions:
        if not isinstance(entry, dict):
            continue
        method = entry.get("paymentMethod")
        if not isinstance(method, dict):
            continue
        brand = method.get("brand")
        if brand:
            out["brand"] = str(brand).upper()
        masked = str(method.get("maskedCardNumber") or "")
        digits = "".join(ch for ch in masked if ch.isdigit())
        if len(digits) >= 4:
            out["last4"] = digits[-4:]
        expiry = method.get("expiryDate")
        if isinstance(expiry, dict):
            month, year = expiry.get("month"), expiry.get("year")
            if month:
                out["exp_month"] = int(month)
            if year:
                # The sample shows a two-digit year (39). Normalise to four
                # digits rather than storing an ambiguous value.
                year_int = int(year)
                out["exp_year"] = year_int + 2000 if year_int < 100 else year_int
    return out


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
