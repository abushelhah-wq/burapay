"""
The benchmark engine — one transaction, measured end to end.

This is where the specification's fair-comparison rules are enforced in code:

* All timing comes from ``TransactionTimer``'s monotonic origin and from the
  instrumented client's ``perf_counter`` deltas. Wall-clock timestamps are recorded
  for display and ordering only (section 2).
* Gateway API time is the sum of the *timed* calls. Setup calls — tokenization, the
  payment that a refund test needs something to refund — are recorded and excluded
  (section 39).
* Application overhead is measured as elapsed time minus the time spent inside
  gateway calls, and stored separately so it can never inflate a gateway's number
  (section 51). Database writes happen after the timer is read.
* A metric whose bracketing events did not both occur is stored as NULL rather than
  guessed (section 9).
* Nothing may target a production gateway environment unless it has been explicitly
  unlocked (section 26).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (ErrorCategory, NotConfigured, NotSupported,
                             normalize)
from app.core.logging import get_logger
from app.core.sanitizer import sanitize
from app.benchmarks.timeline import EVENT_LABELS, TransactionTimer
from app.gateways.base import Card, HppSession, PaymentRequest, PaymentResult
from app.gateways.http import InstrumentedClient
from app.gateways.registry import build_adapter
from app.models import (ApiMeasurement, Gateway, IntegrationType, TimelineEvent,
                        Transaction, TransactionEvent, TransactionStatus)
from app.services import bootstrap as settings_service
from app.services.credentials import get_gateway, load_credentials

logger = get_logger(__name__)


class BenchmarkRefused(RuntimeError):
    """The platform declined to run this transaction at all (guard rails, not gateways)."""


def new_reference(prefix: str = "burapay") -> str:
    """A unique merchant reference. Gateways reject repeats, so every attempt needs one."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


async def _test_card(session: AsyncSession) -> Card:
    values = await settings_service.get_setting(session, "test_card")
    return Card(number=str(values.get("number", "4111111111111111")),
                month=str(values.get("month", "12")),
                year=str(values.get("year", "2030")),
                cvc=str(values.get("cvc", "123")),
                holder=str(values.get("holder", "BuraPay Benchmark")))


#: A 3DS challenge form is a page's worth of markup at most. Anything larger is not
#: a challenge and is not worth carrying in a transaction's context.
CHALLENGE_HTML_LIMIT = 256_000


def _bounded_challenge(context: Mapping[str, Any]) -> dict:
    """Keep a challenge form to a sane size before it is stored."""
    values = dict(context or {})
    html = values.get("challenge_html")
    if isinstance(html, str) and len(html) > CHALLENGE_HTML_LIMIT:
        values["challenge_html"] = None
        values["challenge_html_dropped"] = (
            f"The gateway returned {len(html)} characters of challenge markup, which is "
            "too large to be a 3DS form. It was not stored.")
    return values


def _token_context(result: PaymentResult) -> dict:
    """The reusable half of a payment: what a later charge would need to repeat it.

    A token on its own is not enough on every gateway — Geidea also wants the
    agreement the card was stored under — so the two travel together and are shown
    together rather than leaving somebody to find the second half later.
    """
    values = {"stored_token": result.stored_token,
              "stored_token_hint": result.stored_token_hint,
              "agreement_id": result.agreement_id,
              "agreement_type": result.agreement_type}
    return {key: value for key, value in values.items() if value}


def _guard_environment(environment: str) -> None:
    if environment != "sandbox" and not settings.allow_production_gateways:
        raise BenchmarkRefused(
            "This deployment is restricted to sandbox/test environments. Production "
            "gateway access requires ALLOW_PRODUCTION_GATEWAYS to be enabled "
            "explicitly, which is a deliberate configuration change.")


async def _persist_measurements(session: AsyncSession, transaction: Transaction,
                                client: InstrumentedClient) -> None:
    """Write the call log. Runs after the timer has been read, never inside it."""
    for record in client.measurements:
        session.add(ApiMeasurement(
            transaction_id=transaction.id,
            gateway_id=transaction.gateway_id,
            sequence=record.sequence,
            operation_name=record.operation_name,
            normalized_operation=record.normalized_operation.value,
            endpoint=record.endpoint,
            http_method=record.http_method,
            started_at=record.started_at,
            completed_at=record.completed_at,
            duration_ms=record.duration_ms,
            http_status=record.http_status,
            gateway_response_code=record.gateway_response_code,
            gateway_response_message=record.gateway_response_message,
            success=record.success,
            error_category=record.error_category.value if record.error_category else None,
            error_message=record.error_message,
            timed_out=record.timed_out,
            is_setup_call=record.is_setup_call,
            request_size_bytes=record.request_size_bytes,
            response_size_bytes=record.response_size_bytes,
            response_snippet=record.response_snippet,
            request_snippet=record.request_snippet,
        ))


async def _persist_events(session: AsyncSession, transaction: Transaction,
                          timer: TransactionTimer) -> None:
    for entry in timer.entries:
        session.add(TransactionEvent(
            transaction_id=transaction.id,
            event_type=entry.event_type.value,
            event_timestamp=entry.timestamp,
            offset_ms=entry.offset_ms,
            label=entry.label or EVENT_LABELS.get(entry.event_type, ""),
            event_metadata=sanitize(entry.metadata),
        ))


def _apply_timings(transaction: Transaction, timer: TransactionTimer,
                   client: InstrumentedClient, *, total_ms: Optional[float] = None) -> None:
    """Attach the measured durations, keeping the four categories separate."""
    summary = timer.summary(gateway_api_time_ms=client.gateway_api_time_ms,
                            total_duration_ms=total_ms)
    transaction.gateway_api_time_ms = summary["gateway_api_time_ms"]
    transaction.three_ds_time_ms = summary["three_ds_time_ms"]
    transaction.customer_interaction_time_ms = summary["customer_interaction_time_ms"]
    transaction.redirect_time_ms = summary["redirect_time_ms"]
    transaction.total_duration_ms = summary["total_duration_ms"]
    transaction.api_call_count = len(client.timed_measurements)

    # Overhead is what the wall gave us minus what the gateway took. Everything the
    # platform itself did lands here and nowhere else.
    all_call_ms = sum(m.duration_ms for m in client.measurements)
    if transaction.total_duration_ms is not None:
        transaction.app_overhead_ms = round(
            max(0.0, transaction.total_duration_ms - all_call_ms), 3)


def _apply_result(transaction: Transaction, result: PaymentResult) -> None:
    transaction.status = result.status.value
    transaction.gateway_transaction_id = result.gateway_reference
    transaction.gateway_response_code = result.gateway_code
    transaction.gateway_response_message = (result.gateway_message or "")[:2000] or None
    if result.status is TransactionStatus.SUCCESS:
        transaction.error_category = None
        transaction.error_message = None
    elif result.status is TransactionStatus.DECLINED:
        transaction.error_category = ErrorCategory.GATEWAY_DECLINE.value
        transaction.error_message = result.gateway_message
    elif result.status is TransactionStatus.CANCELLED:
        transaction.error_category = ErrorCategory.CUSTOMER_CANCELLED.value
        transaction.error_message = result.gateway_message


def _apply_failure(transaction: Transaction, exc: BaseException, gateway_code: str,
                   operation: str) -> None:
    error = normalize(exc, gateway=gateway_code, operation=operation)
    transaction.error_category = error.category.value
    transaction.error_message = error.message[:4000]
    if error.gateway_code:
        transaction.gateway_response_code = error.gateway_code
    transaction.status = {
        ErrorCategory.TIMEOUT: TransactionStatus.TIMEOUT,
        ErrorCategory.GATEWAY_DECLINE: TransactionStatus.DECLINED,
        ErrorCategory.CUSTOMER_CANCELLED: TransactionStatus.CANCELLED,
    }.get(error.category, TransactionStatus.ERROR).value


async def _prepare(session: AsyncSession, gateway_code: str, environment: str
                   ) -> Tuple[Gateway, Any]:
    _guard_environment(environment)
    gateway = await get_gateway(session, gateway_code)
    if gateway is None:
        raise BenchmarkRefused(f"Unknown gateway {gateway_code!r}.")
    if not gateway.enabled:
        raise BenchmarkRefused(f"{gateway.name} is disabled in the gateway catalogue.")
    credentials = await load_credentials(session, gateway_code, environment)
    adapter = build_adapter(gateway_code, credentials, environment)
    adapter.require_configured()
    return gateway, adapter


# --------------------------------------------------------------------------- #
# Direct API
# --------------------------------------------------------------------------- #

async def run_direct_transaction(
        session: AsyncSession, *, gateway_code: str, amount: float, currency: str,
        description: str = "BuraPay benchmark transaction",
        reference: Optional[str] = None, environment: str = "sandbox",
        benchmark_run_id: Optional[str] = None, methodology: str = "mixed",
        payment_mode: str = "standard", card: Optional[Card] = None,
        token_id: Optional[str] = None, agreement_id: Optional[str] = None,
        initiated_by: Optional[str] = None,
        browser: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[str] = None) -> Transaction:
    """Run one Direct API transaction.

    Usually start to final state in this process. A gateway that stops for a 3DS
    challenge is the exception: the transaction is left PENDING with everywhere the
    browser needs to go in its context, exactly as HPP leg 1 does, and finishes in
    ``complete_direct_transaction`` when the issuer sends the cardholder back.

    ``card`` and ``token_id`` are what the operator entered on the payment form. With
    neither, the test card configured on the Settings page is used, which is what an
    automated benchmark run does.
    """
    gateway, adapter = await _prepare(session, gateway_code, environment)
    adapter.require_supports(IntegrationType.DIRECT)
    adapter.require_supports_mode(payment_mode)
    if not adapter.supports_currency(currency):
        raise BenchmarkRefused(
            f"{gateway.name} is not configured for {currency}. Supported: "
            f"{', '.join(adapter.supported_currencies) or 'not declared'}.")

    reference = reference or new_reference()
    if payment_mode == "token":
        # A stored-token charge sends no card details at all — that is the point of it.
        payment_card: Optional[Card] = None
    elif card is not None or token_id:
        # The operator supplied one or the other on the payment form. A supplied token
        # means deliberately not typing a card, so no test card is substituted.
        payment_card = card
    else:
        payment_card = await _test_card(session)

    transaction = Transaction(
        benchmark_run_id=benchmark_run_id, gateway_id=gateway.id, gateway_code=gateway.code,
        merchant_reference=reference, integration_type=IntegrationType.DIRECT.value,
        payment_mode=payment_mode, environment=environment, amount=amount,
        currency=currency.upper(), description=description,
        status=TransactionStatus.IN_PROGRESS.value, methodology=methodology,
        created_by_user_id=created_by_user_id)
    session.add(transaction)
    await session.flush()          # id is needed before the flow starts

    # Built after the flush because a 3DS challenge sends the cardholder back to this
    # transaction's own return leg, and that needs the id.
    request = PaymentRequest(
        amount=amount, currency=currency.upper(), reference=reference,
        description=description,
        return_url=f"{settings.public_base_url}/api/v1/transactions/{transaction.id}/return",
        webhook_url=f"{settings.public_base_url}/api/webhooks/{gateway_code}",
        payment_mode=payment_mode, card=payment_card, token_id=token_id,
        browser=dict(browser or {}),
        metadata={key: value for key, value in
                  (("agreement_id", agreement_id), ("initiated_by", initiated_by))
                  if value})

    timer = TransactionTimer()
    transaction.started_at = timer.started_at
    timer.mark(TimelineEvent.BENCHMARK_STARTED)
    client = adapter.build_client(settings.http_timeout_seconds)

    try:
        timer.mark(TimelineEvent.PAYMENT_REQUEST_SENT)
        result = await adapter.process_direct_payment(client, request)
        timer.mark(TimelineEvent.PAYMENT_RESPONSE_RECEIVED)
        if result.three_ds_required:
            # 3DS timing is bracketed from the authentication calls the adapter made,
            # so it reflects the gateway's own sequence rather than a guess.
            auth_calls = [m for m in client.timed_measurements
                          if m.normalized_operation.value == "AUTHENTICATION"]
            if auth_calls:
                first, last = auth_calls[0], auth_calls[-1]
                base = timer.first(TimelineEvent.PAYMENT_REQUEST_SENT)
                offset = base.offset_ms if base else 0.0
                timer.mark_at(TimelineEvent.THREE_DS_INITIATED, offset,
                              timestamp=first.started_at)
                timer.mark_at(TimelineEvent.THREE_DS_COMPLETED,
                              offset + sum(m.duration_ms for m in auth_calls),
                              timestamp=last.completed_at or last.started_at)
        if result.requires_customer_action:
            # The issuer wants the cardholder. Stop the clock on the gateway's share
            # here; everything from now until they come back is their time.
            timer.mark(TimelineEvent.REDIRECT_INITIATED,
                       label="3DS challenge; awaiting the cardholder")
            _apply_result(transaction, result)
            total_ms = timer.elapsed_ms()
            transaction.context = sanitize({
                "adapter_context": _bounded_challenge(result.context),
                "return_url": request.return_url,
                "webhook_url": request.webhook_url,
                # Where to send the browser. A gateway that only handed back an
                # auto-submitting form gets our own challenge page, which renders it.
                "action_url": result.action_url or f"/three-ds/{transaction.id}",
                "awaiting_customer_action": True,
                # Leg 1's own numbers, so the resume leg adds to them rather than
                # overwriting them — the same arrangement HPP uses.
                "leg1_gateway_api_time_ms": client.gateway_api_time_ms,
                "leg1_elapsed_ms": total_ms,
                "leg1_sequence": len(client.measurements),
            })
            _apply_timings(transaction, timer, client, total_ms=total_ms)
            transaction.total_duration_ms = None   # not final until the customer returns
            await _persist_measurements(session, transaction, client)
            await _persist_events(session, transaction, timer)
            await session.commit()
            await session.refresh(transaction)
            logger.info("direct transaction paused for 3DS",
                        extra={"gateway": gateway_code, "transaction_id": transaction.id,
                               "operation": "direct_payment", "status": "pending"})
            return transaction

        timer.mark(TimelineEvent.AUTHORIZATION_RESPONSE)
        _apply_result(transaction, result)
        if result.stored_token:
            # Shown on the transaction page for the operator to copy into Settings or
            # straight into the next payment. Deliberately not written into the
            # gateway's credentials by the platform: a card token belongs to a
            # cardholder, and storing one automatically is not a decision this
            # application should make on someone's behalf.
            transaction.context = {**(transaction.context or {}),
                                   **_token_context(result)}
        timer.mark(TimelineEvent.FINAL_STATUS_CONFIRMED, status=result.status.value)
    except Exception as exc:                              # noqa: BLE001 - every failure is data
        timer.mark(TimelineEvent.ERROR, label=type(exc).__name__)
        _apply_failure(transaction, exc, gateway_code, "direct_payment")
        if isinstance(exc, (NotConfigured, NotSupported)):
            logger.warning("direct transaction refused",
                           extra={"gateway": gateway_code, "transaction_id": transaction.id,
                                  "operation": "direct_payment", "status": "refused"})
        else:
            logger.warning("direct transaction failed",
                           extra={"gateway": gateway_code, "transaction_id": transaction.id,
                                  "operation": "direct_payment", "status": "error"})
    finally:
        total_ms = timer.elapsed_ms()
        await client.client.aclose()

    transaction.completed_at = datetime.now(timezone.utc)
    _apply_timings(transaction, timer, client, total_ms=total_ms)
    await _persist_measurements(session, transaction, client)
    await _persist_events(session, transaction, timer)
    await session.commit()
    await session.refresh(transaction)

    logger.info("transaction complete",
                extra={"gateway": gateway_code, "transaction_id": transaction.id,
                       "operation": "direct_payment",
                       "duration_ms": transaction.total_duration_ms,
                       "gateway_api_time_ms": transaction.gateway_api_time_ms,
                       "status": transaction.status})
    return transaction


# --------------------------------------------------------------------------- #
# HPP — leg 1
# --------------------------------------------------------------------------- #

async def start_hpp_transaction(
        session: AsyncSession, *, gateway_code: str, amount: float, currency: str,
        description: str = "BuraPay benchmark transaction",
        reference: Optional[str] = None, environment: str = "sandbox",
        benchmark_run_id: Optional[str] = None, methodology: str = "mixed",
        created_by_user_id: Optional[str] = None) -> Tuple[Transaction, HppSession]:
    """Create the hosted session and return where to send the browser.

    The transaction stays ``PENDING`` until the customer comes back: the time they
    spend on the gateway's page belongs to them, not to the gateway's API.
    """
    gateway, adapter = await _prepare(session, gateway_code, environment)
    adapter.require_supports(IntegrationType.HPP)
    if not adapter.supports_currency(currency):
        raise BenchmarkRefused(
            f"{gateway.name} is not configured for {currency}. Supported: "
            f"{', '.join(adapter.supported_currencies) or 'not declared'}.")

    reference = reference or new_reference()
    transaction = Transaction(
        benchmark_run_id=benchmark_run_id, gateway_id=gateway.id, gateway_code=gateway.code,
        merchant_reference=reference, integration_type=IntegrationType.HPP.value,
        environment=environment, amount=amount, currency=currency.upper(),
        description=description, status=TransactionStatus.IN_PROGRESS.value,
        methodology=methodology, created_by_user_id=created_by_user_id)
    session.add(transaction)
    await session.flush()

    request = PaymentRequest(
        amount=amount, currency=currency.upper(), reference=reference,
        description=description,
        return_url=f"{settings.public_base_url}/api/v1/transactions/{transaction.id}/return",
        webhook_url=f"{settings.public_base_url}/api/webhooks/{gateway_code}")

    timer = TransactionTimer()
    transaction.started_at = timer.started_at
    timer.mark(TimelineEvent.BENCHMARK_STARTED)
    client = adapter.build_client(settings.http_timeout_seconds)

    try:
        timer.mark(TimelineEvent.SESSION_REQUEST_SENT)
        hpp = await adapter.create_hpp_session(client, request)
        timer.mark(TimelineEvent.SESSION_RESPONSE_RECEIVED)
        timer.mark(TimelineEvent.HPP_URL_GENERATED, mode=hpp.mode)
        timer.mark(TimelineEvent.REDIRECT_INITIATED)
    except Exception as exc:                              # noqa: BLE001
        timer.mark(TimelineEvent.ERROR, label=type(exc).__name__)
        _apply_failure(transaction, exc, gateway_code, "create_hpp_session")
        transaction.completed_at = datetime.now(timezone.utc)
        _apply_timings(transaction, timer, client, total_ms=timer.elapsed_ms())
        await client.client.aclose()
        await _persist_measurements(session, transaction, client)
        await _persist_events(session, transaction, timer)
        await session.commit()
        raise

    total_ms = timer.elapsed_ms()
    await client.client.aclose()

    transaction.status = TransactionStatus.PENDING.value
    transaction.gateway_transaction_id = hpp.gateway_reference
    transaction.context = sanitize({
        "adapter_context": hpp.context,
        "return_url": request.return_url,
        "webhook_url": request.webhook_url,
        "mode": hpp.mode,
        "redirect_url": hpp.redirect_url,
        # The session-creation leg's own numbers, so leg 2 can add to them rather
        # than overwrite them.
        "leg1_gateway_api_time_ms": client.gateway_api_time_ms,
        "leg1_elapsed_ms": total_ms,
        "leg1_sequence": len(client.measurements),
    })
    _apply_timings(transaction, timer, client, total_ms=total_ms)
    transaction.total_duration_ms = None      # not final yet; set when the customer returns
    await _persist_measurements(session, transaction, client)
    await _persist_events(session, transaction, timer)
    await session.commit()
    await session.refresh(transaction)
    return transaction, hpp


async def _complete_second_leg(
        session: AsyncSession, transaction: Transaction, params: Mapping[str, str], *,
        confirm, operation: str) -> Transaction:
    """Finish a transaction that paused for the customer, and add the two legs up.

    Shared by hosted checkout — where the customer spent the pause on the gateway's
    own page — and by a Direct payment that stopped for a 3DS challenge. The shape is
    the same in both cases and so is the rule that matters: the pause belongs to the
    customer and never to the gateway's API time.

    Offsets here are computed from the persisted ``started_at`` rather than from a
    fresh monotonic origin, because this runs in a different request from leg 1. The
    *durations* of the confirmation calls still come from ``perf_counter``, so the
    numbers that get compared between gateways remain monotonic-clock measurements;
    only the position of the events on the timeline uses the wall clock, which is the
    one thing the wall clock is fit for here.
    """
    gateway, adapter = await _prepare(session, transaction.gateway_code,
                                      transaction.environment)
    context = dict(transaction.context or {})
    adapter_context = context.get("adapter_context") or {}

    request = PaymentRequest(
        amount=transaction.amount, currency=transaction.currency,
        reference=transaction.merchant_reference,
        description=transaction.description or "",
        return_url=context.get("return_url", ""),
        webhook_url=context.get("webhook_url", ""),
        payment_mode=transaction.payment_mode or "standard")

    started_at = transaction.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return_offset_ms = round((now - started_at).total_seconds() * 1000.0, 3)

    timer = TransactionTimer()
    timer.mark_at(TimelineEvent.RETURN_URL_RECEIVED, return_offset_ms, timestamp=now)

    client = adapter.build_client(settings.http_timeout_seconds)
    confirm_started = time.perf_counter()
    # Whatever happens next, the customer is no longer being waited on. Leaving this
    # set would keep offering a "continue verification" route into a finished payment.
    context.pop("awaiting_customer_action", None)
    transaction.context = context

    try:
        result = await confirm(adapter, client, request, adapter_context)
        _apply_result(transaction, result)
        if result.stored_token:
            transaction.context = {**context, **_token_context(result)}
        timer.mark_at(TimelineEvent.FINAL_STATUS_CONFIRMED,
                      return_offset_ms + (time.perf_counter() - confirm_started) * 1000.0,
                      status=result.status.value)
    except Exception as exc:                              # noqa: BLE001
        timer.mark_at(TimelineEvent.ERROR,
                      return_offset_ms + (time.perf_counter() - confirm_started) * 1000.0,
                      label=type(exc).__name__)
        _apply_failure(transaction, exc, transaction.gateway_code, operation)
    finally:
        await client.client.aclose()

    transaction.completed_at = datetime.now(timezone.utc)
    # Leg 1 and leg 2 are added together: the gateway's API contribution is both legs,
    # and the customer's time in between is neither.
    leg1_api_ms = float(context.get("leg1_gateway_api_time_ms") or 0.0)
    transaction.gateway_api_time_ms = round(leg1_api_ms + client.gateway_api_time_ms, 3)
    transaction.total_duration_ms = round(
        (transaction.completed_at - started_at).total_seconds() * 1000.0, 3)
    transaction.api_call_count = (
        (transaction.api_call_count or 0) + len(client.timed_measurements))

    # Everything between leg 1 handing over to the browser and the customer's return
    # that was not a gateway call is time the customer spent.
    leg1_elapsed = float(context.get("leg1_elapsed_ms") or 0.0)
    interaction = return_offset_ms - leg1_elapsed
    transaction.customer_interaction_time_ms = round(interaction, 3) if interaction > 0 else None

    if transaction.webhook_received_at is not None:
        received = transaction.webhook_received_at
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        transaction.webhook_latency_ms = round(
            (received - started_at).total_seconds() * 1000.0, 3)

    # Continue leg 1's sequence numbering so the call list reads as one flow.
    offset = int(context.get("leg1_sequence") or 0)
    for record in client.measurements:
        record.sequence += offset
    await _persist_measurements(session, transaction, client)
    await _persist_events(session, transaction, timer)
    await session.commit()
    await session.refresh(transaction)

    logger.info("pending transaction completed",
                extra={"gateway": transaction.gateway_code,
                       "transaction_id": transaction.id, "operation": operation,
                       "duration_ms": transaction.total_duration_ms,
                       "gateway_api_time_ms": transaction.gateway_api_time_ms,
                       "status": transaction.status})
    return transaction


# --------------------------------------------------------------------------- #
# HPP — leg 2
# --------------------------------------------------------------------------- #

async def complete_hpp_transaction(session: AsyncSession, transaction: Transaction,
                                   params: Mapping[str, str]) -> Transaction:
    """Confirm the outcome after the customer returns from the gateway's page."""

    async def confirm(adapter, client, request, adapter_context) -> PaymentResult:
        return await adapter.confirm_hpp_payment(client, request, adapter_context, params)

    return await _complete_second_leg(session, transaction, params, confirm=confirm,
                                      operation="confirm_hpp_payment")


# --------------------------------------------------------------------------- #
# Direct API — the 3DS resume leg
# --------------------------------------------------------------------------- #

async def complete_direct_transaction(session: AsyncSession, transaction: Transaction,
                                      params: Mapping[str, str]) -> Transaction:
    """Finish a Direct payment after the cardholder completed the 3DS challenge.

    Only the authorisation call the adapter still had to make is timed. The OTP, the
    issuer's page and the round trip through the browser were the customer's, and land
    in ``customer_interaction_time_ms`` where they cannot flatter or penalise the
    gateway's API numbers.
    """

    async def confirm(adapter, client, request, adapter_context) -> PaymentResult:
        return await adapter.complete_direct_payment(client, request, adapter_context,
                                                     params)

    return await _complete_second_leg(session, transaction, params, confirm=confirm,
                                      operation="complete_direct_payment")


async def complete_pending_transaction(session: AsyncSession, transaction: Transaction,
                                       params: Mapping[str, str]) -> Transaction:
    """Resume whichever kind of pause this transaction is sitting in."""
    if transaction.integration_type == IntegrationType.DIRECT.value:
        return await complete_direct_transaction(session, transaction, params)
    return await complete_hpp_transaction(session, transaction, params)
