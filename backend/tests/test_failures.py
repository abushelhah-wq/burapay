"""
Failure handling (§6).

Network errors, timeouts, non-2xx responses and unparseable bodies are four
different things, and the platform must tell them apart. Every one of them must
also leave a finalised transaction and a complete log row -- the requirement
that "a crashed call must never leave a transaction row silently stuck in
PENDING with no log explaining why".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.gateways.results import OrderContext, RawCard
from app.models import ApiCallLog, Operation, TransactionStatus
from app.services import execution
from app.services.bootstrap import integration_type_by_code
from tests.mock_geidea import state as mock_state

pytestmark = pytest.mark.asyncio

CARD = RawCard(number="4111111111111111", exp_month=12, exp_year=2030, cvv="123")


async def _sale(db, geidea, amount=1000):
    integration = await integration_type_by_code(db, "DIRECT_API")
    transaction, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.SALE,
        idempotency_key=f"idem-{uuid.uuid4().hex}", amount_minor=amount, currency="SAR",
    )

    async def runner(adapter):
        return await adapter.direct_api_sale(
            OrderContext(
                merchant_reference=transaction.merchant_reference,
                amount_minor=amount, currency="SAR",
                return_url="https://test.example/r", callback_url="https://test.example/c",
            ),
            CARD,
        )

    await execution.execute(db, gateway=geidea, transaction=transaction, runner=runner)
    await db.refresh(transaction)
    return transaction


async def _logs(db, transaction_id):
    result = await db.execute(
        select(ApiCallLog)
        .where(ApiCallLog.transaction_id == transaction_id)
        .order_by(ApiCallLog.sequence_number)
    )
    return list(result.scalars().all())


async def test_timeout_is_recorded_and_reconciled_not_left_pending(db, geidea):
    """
    §4j: a timeout triggers one order query rather than leaving PENDING forever.

    Here the order was never created, so reconciliation has nothing to query --
    and the transaction must still end TIMEOUT with a log row explaining it,
    never PENDING.
    """
    mock_state.delay_seconds = {"create_session": 6.0}  # HTTP_TIMEOUT_SECONDS is 5

    transaction = await _sale(db, geidea)

    assert transaction.status == TransactionStatus.TIMEOUT.value
    assert transaction.error_code == "timeout"
    assert transaction.completed_at is not None
    assert transaction.duration_ms is not None

    logs = await _logs(db, transaction.id)
    assert len(logs) == 1
    assert logs[0].step_name == "create_session"
    assert logs[0].response_status_code is None
    assert "timed out" in (logs[0].error_text or "")
    # The timeout that applied is recorded, so timeout behaviour is comparable
    # between gateways.
    assert logs[0].timeout_seconds == 5.0
    assert transaction.request_count == 1


async def test_non_2xx_is_an_error_with_the_body_logged(db, geidea):
    mock_state.fail_status = {"pay": 502}

    transaction = await _sale(db, geidea)

    assert transaction.status == TransactionStatus.ERROR.value
    assert transaction.error_code == "http_status"

    logs = await _logs(db, transaction.id)
    assert [log.step_name for log in logs] == [
        "create_session", "initiate_authentication", "authenticate_payer", "pay",
    ]
    assert logs[-1].response_status_code == 502
    # The body of the failure is kept -- that is where a gateway puts its
    # reason, and discarding it makes the failure unexplainable.
    assert logs[-1].response_body_json is not None
    assert transaction.request_count == 4


async def test_unparseable_body_is_a_parse_error_not_a_success(db, geidea):
    """
    A 2xx we cannot read is an ERROR: we do not know whether money moved.

    Treating it as success would corrupt the audit trail in the one direction
    that matters.
    """
    mock_state.garbage_body = {"pay"}

    transaction = await _sale(db, geidea)

    assert transaction.status == TransactionStatus.ERROR.value
    assert transaction.error_code == "parse_error"

    logs = await _logs(db, transaction.id)
    assert logs[-1].response_status_code == 200
    assert "not JSON" in (logs[-1].error_text or "")
    # The raw body is still stored, so the actual response can be inspected.
    assert logs[-1].response_body_json is not None


async def test_network_error_when_the_gateway_is_unreachable(db, geidea):
    """An unroutable base URL exercises the network-failure path."""
    geidea.config_json = {"api_base": "http://127.0.0.1:1"}
    await db.commit()

    transaction = await _sale(db, geidea)

    assert transaction.status == TransactionStatus.ERROR.value
    assert transaction.error_code == "network_error"
    logs = await _logs(db, transaction.id)
    assert len(logs) == 1
    assert logs[0].response_status_code is None
    assert logs[0].error_text
    assert transaction.request_count == 1


async def test_timeout_reconciles_to_success_when_the_order_exists(db, geidea):
    """
    The case §4j is really about: the call timed out but the payment worked.

    A successful sale is run first so a real order exists; the transaction is
    then forced back to PENDING with that order id and reconciled, proving the
    query resolves it rather than leaving it unresolved.
    """
    settled = await _sale(db, geidea)
    assert settled.status == TransactionStatus.SUCCESS.value
    order_id = settled.gateway_order_id
    assert order_id

    settled.status = TransactionStatus.TIMEOUT.value
    settled.error_message = "forced"
    before = settled.request_count
    await db.commit()

    resolved = await execution.reconcile_after_timeout(db, geidea, settled)
    await db.refresh(settled)

    assert resolved is TransactionStatus.SUCCESS
    assert settled.status == TransactionStatus.SUCCESS.value
    # The reconciliation query is itself a round trip and is counted.
    assert settled.request_count == before + 1
    assert "resolved by order query" in (settled.error_message or "")

    logs = await _logs(db, settled.id)
    assert logs[-1].step_name == "order_query"
