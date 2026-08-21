"""
End-to-end flow tests (§6).

Each test asserts three things, not one: the final transaction status, the
``request_count``, and the exact ordered ``api_call_logs`` sequence. Asserting
only the status would let a regression that doubles the number of round trips
pass silently -- and the round-trip count is the product's headline metric, so
a silent change there is the worst kind of regression this codebase can have.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.gateways.results import OrderContext, RawCard
from app.models import ApiCallLog, Operation, Transaction, TransactionStatus
from app.services import execution
from app.services.bootstrap import integration_type_by_code
from tests.mock_geidea import state as mock_state

pytestmark = pytest.mark.asyncio

TEST_CARD = RawCard(
    number="4111111111111111", exp_month=12, exp_year=2030, cvv="123",
    holder_name="Benchmark Runner",
)


def key() -> str:
    return f"idem-{uuid.uuid4().hex}"


async def _calls(db, transaction_id: int) -> list[ApiCallLog]:
    result = await db.execute(
        select(ApiCallLog)
        .where(ApiCallLog.transaction_id == transaction_id)
        .order_by(ApiCallLog.sequence_number)
    )
    return list(result.scalars().all())


async def _run_direct(db, geidea, *, operation=Operation.SALE, amount=1000):
    integration = await integration_type_by_code(db, "DIRECT_API")
    transaction, created = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=operation,
        idempotency_key=key(), amount_minor=amount, currency="SAR",
    )
    assert created

    async def runner(adapter):
        order = OrderContext(
            merchant_reference=transaction.merchant_reference,
            amount_minor=transaction.amount_minor,
            currency=transaction.currency,
            return_url="https://test.example/return",
            callback_url="https://test.example/callback",
        )
        if operation is Operation.SALE:
            return await adapter.direct_api_sale(order, TEST_CARD)
        return await adapter.direct_api_auth(order, TEST_CARD)

    await execution.execute(db, gateway=geidea, transaction=transaction, runner=runner)
    await db.refresh(transaction)
    return transaction


# ---------------------------------------------------------------------------
# Direct API sale
# ---------------------------------------------------------------------------

async def test_direct_sale_succeeds_with_full_documented_sequence(db, geidea):
    transaction = await _run_direct(db, geidea)

    assert transaction.status == TransactionStatus.SUCCESS.value
    assert transaction.gateway_order_id
    assert transaction.duration_ms is not None

    calls = await _calls(db, transaction.id)
    assert [call.step_name for call in calls] == [
        "create_session", "initiate_authentication", "authenticate_payer", "pay",
    ]
    assert [call.sequence_number for call in calls] == [1, 2, 3, 4]
    # The count is derived from the calls, so these can never disagree.
    assert transaction.request_count == len(calls) == 4
    assert all(call.response_status_code == 200 for call in calls)
    assert all(call.duration_ms is not None for call in calls)
    # §6: the timeout in force is recorded on every call.
    assert all(call.timeout_seconds == 5.0 for call in calls)


async def test_direct_sale_skips_payer_when_frictionless(db, geidea):
    """
    The open question the benchmark exists to answer.

    The adapter does not hardcode a call count: given an explicit frictionless
    marker it completes in three calls, and the transaction records that it
    did. Whether Geidea really permits this is what a live sandbox run decides.
    """
    mock_state.frictionless = True
    transaction = await _run_direct(db, geidea)

    calls = await _calls(db, transaction.id)
    assert [call.step_name for call in calls] == [
        "create_session", "initiate_authentication", "pay",
    ]
    assert transaction.request_count == 3
    assert transaction.status == TransactionStatus.SUCCESS.value


async def test_decline_is_failed_not_error(db, geidea):
    """
    A decline is a completed round trip, so it is FAILED, not ERROR.

    Collapsing the two would make a gateway that declines fast look identical
    to one that is broken.
    """
    mock_state.decline_pay = True
    transaction = await _run_direct(db, geidea)

    assert transaction.status == TransactionStatus.FAILED.value
    assert transaction.error_code == "declined"
    assert "Insufficient funds" in (transaction.error_message or "")
    assert transaction.request_count == 4
    assert len(await _calls(db, transaction.id)) == 4


async def test_pre_auth_capability_error_is_distinguishable(db, geidea):
    """§4c: 'not supported for this merchant' must not read as a generic failure."""
    mock_state.capability_error = True
    transaction = await _run_direct(db, geidea, operation=Operation.AUTH)

    assert transaction.status == TransactionStatus.FAILED.value
    assert transaction.error_code == "merchant_capability"
    assert "merchant account" in (transaction.error_message or "").lower()


# ---------------------------------------------------------------------------
# Auth -> capture / void
# ---------------------------------------------------------------------------

async def _capture(db, geidea, parent, amount=None):
    integration = await integration_type_by_code(db, "DIRECT_API")
    is_partial = amount is not None and amount < parent.remaining_capturable_minor
    child, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration,
        operation=Operation.PARTIAL_CAPTURE if is_partial else Operation.CAPTURE,
        idempotency_key=key(),
        amount_minor=amount if amount is not None else parent.remaining_capturable_minor,
        currency=parent.currency, parent=parent,
    )
    await execution.execute(
        db, gateway=geidea, transaction=child,
        runner=lambda adapter: adapter.capture(parent, amount),
    )
    await db.refresh(child)
    await db.refresh(parent)
    return child


async def test_auth_then_full_capture(db, geidea):
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)
    assert auth.status == TransactionStatus.SUCCESS.value
    assert auth.remaining_capturable_minor == 1000

    capture = await _capture(db, geidea, auth)

    assert capture.status == TransactionStatus.SUCCESS.value
    assert capture.request_count == 1
    assert [call.step_name for call in await _calls(db, capture.id)] == ["capture"]
    assert auth.captured_amount_minor == 1000
    assert auth.remaining_capturable_minor == 0


async def test_multiple_partial_captures_track_the_balance(db, geidea):
    """§4d: multiple partial captures against one authorisation."""
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)

    first = await _capture(db, geidea, auth, amount=400)
    assert first.status == TransactionStatus.SUCCESS.value
    assert first.operation == Operation.PARTIAL_CAPTURE.value
    assert auth.captured_amount_minor == 400
    assert auth.remaining_capturable_minor == 600

    second = await _capture(db, geidea, auth, amount=600)
    assert second.status == TransactionStatus.SUCCESS.value
    assert auth.captured_amount_minor == 1000
    assert auth.remaining_capturable_minor == 0


async def test_capture_beyond_authorised_amount_is_refused_locally(db, geidea):
    """
    Over-capture is blocked before any HTTP call.

    Asking the gateway a question we already know the answer to wastes a round
    trip and pollutes the benchmark with a decline that is our fault.
    """
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)
    mock_state.calls.clear()

    capture = await _capture(db, geidea, auth, amount=5000)

    assert capture.status == TransactionStatus.FAILED.value
    assert capture.error_code == "invalid_state"
    assert capture.request_count == 0
    assert mock_state.calls == []


async def test_auth_then_void(db, geidea):
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)
    integration = await integration_type_by_code(db, "DIRECT_API")

    child, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.VOID,
        idempotency_key=key(), amount_minor=auth.amount_minor, currency="SAR",
        parent=auth,
    )
    await execution.execute(
        db, gateway=geidea, transaction=child,
        runner=lambda adapter: adapter.void(auth),
    )
    await db.refresh(child)
    await db.refresh(auth)

    assert child.status == TransactionStatus.SUCCESS.value
    assert child.request_count == 1
    assert [call.step_name for call in await _calls(db, child.id)] == ["void"]
    # A void leaves nothing capturable.
    assert auth.remaining_capturable_minor == 0


async def test_void_after_capture_is_refused(db, geidea):
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)
    await _capture(db, geidea, auth)
    integration = await integration_type_by_code(db, "DIRECT_API")

    child, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.VOID,
        idempotency_key=key(), amount_minor=auth.amount_minor, currency="SAR",
        parent=auth,
    )
    await execution.execute(
        db, gateway=geidea, transaction=child,
        runner=lambda adapter: adapter.void(auth),
    )
    await db.refresh(child)

    assert child.status == TransactionStatus.FAILED.value
    assert child.error_code == "invalid_state"
    assert child.request_count == 0


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

async def _refund(db, geidea, parent, amount=None):
    integration = await integration_type_by_code(db, "DIRECT_API")
    is_partial = amount is not None and amount < parent.remaining_refundable_minor
    child, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration,
        operation=Operation.PARTIAL_REFUND if is_partial else Operation.REFUND,
        idempotency_key=key(),
        amount_minor=amount if amount is not None else parent.remaining_refundable_minor,
        currency=parent.currency, parent=parent,
    )
    await execution.execute(
        db, gateway=geidea, transaction=child,
        runner=lambda adapter: adapter.refund(parent, amount),
    )
    await db.refresh(child)
    await db.refresh(parent)
    return child


async def test_full_refund_of_a_sale(db, geidea):
    sale = await _run_direct(db, geidea)
    refund = await _refund(db, geidea, sale)

    assert refund.status == TransactionStatus.SUCCESS.value
    assert refund.request_count == 1
    assert [call.step_name for call in await _calls(db, refund.id)] == ["refund"]
    assert sale.refunded_amount_minor == 1000
    assert sale.remaining_refundable_minor == 0


async def test_partial_refunds_accumulate(db, geidea):
    sale = await _run_direct(db, geidea)

    first = await _refund(db, geidea, sale, amount=300)
    assert first.operation == Operation.PARTIAL_REFUND.value
    assert sale.refunded_amount_minor == 300
    assert sale.remaining_refundable_minor == 700

    await _refund(db, geidea, sale, amount=700)
    assert sale.refunded_amount_minor == 1000


async def test_refund_of_uncaptured_auth_is_blocked(db, geidea):
    """§4e: an authorisation that was never captured has nothing to refund."""
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)
    assert auth.remaining_refundable_minor == 0
    mock_state.calls.clear()

    refund = await _refund(db, geidea, auth, amount=500)

    assert refund.status == TransactionStatus.FAILED.value
    assert refund.error_code == "invalid_state"
    assert refund.request_count == 0
    assert mock_state.calls == []


async def test_refund_after_capture_is_allowed_up_to_captured_amount(db, geidea):
    auth = await _run_direct(db, geidea, operation=Operation.AUTH)
    await _capture(db, geidea, auth, amount=600)

    assert auth.remaining_refundable_minor == 600
    refund = await _refund(db, geidea, auth, amount=600)
    assert refund.status == TransactionStatus.SUCCESS.value
