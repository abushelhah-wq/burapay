"""
Idempotency (§0.6) and the server-side gates (§7b).

The gate tests matter more than they look: every one of these flags is
described in §7b as "enforced server-side, not just referenced", and the way
that claim fails in practice is that someone wires the check into the UI and
the endpoint quietly stays open.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.config import reload_settings
from app.gateways.base import GatewayContext
from app.gateways.errors import (
    FeatureDisabled, ProductionGatewayBlocked, UnsupportedOperationError,
)
from app.gateways.geidea.adapter import GeideaAdapter
from app.gateways.results import OrderContext, RawCard
from app.models import Operation, TransactionStatus
from app.services import execution
from app.services.benchmark import RunStatus, check_limits, create_run
from app.services.bootstrap import integration_type_by_code

CARD = RawCard(number="4111111111111111", exp_month=12, exp_year=2030, cvv="123")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

async def test_same_key_returns_the_same_transaction(db, geidea):
    integration = await integration_type_by_code(db, "DIRECT_API")
    key = f"idem-{uuid.uuid4().hex}"

    first, created_first = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.SALE,
        idempotency_key=key, amount_minor=1000, currency="SAR",
    )
    second, created_second = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.SALE,
        idempotency_key=key, amount_minor=1000, currency="SAR",
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


async def test_concurrent_requests_with_one_key_create_one_transaction(db, geidea):
    """
    The double-click case (§0.6).

    Two coroutines race with the same key on independent sessions. The unique
    constraint decides; the loser re-reads the winner's row. Exactly one
    transaction must exist afterwards.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Transaction

    integration = await integration_type_by_code(db, "DIRECT_API")
    integration_id = integration.id
    gateway_id = geidea.id
    key = f"idem-{uuid.uuid4().hex}"

    async def attempt():
        async with session_scope() as session:
            gateway = await session.get(type(geidea), gateway_id)
            integration_row = await session.get(type(integration), integration_id)
            transaction, created = await execution.get_or_create_transaction(
                session, gateway=gateway, integration_type=integration_row,
                operation=Operation.SALE, idempotency_key=key,
                amount_minor=1000, currency="SAR",
            )
            return transaction.id, created

    results = await asyncio.gather(attempt(), attempt(), attempt())

    ids = {transaction_id for transaction_id, _ in results}
    assert len(ids) == 1, f"expected one transaction, got {ids}"
    assert sum(1 for _, created in results if created) == 1

    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.idempotency_key == key
            )
        )
    assert count == 1


async def test_replaying_a_settled_transaction_makes_no_new_calls(db, geidea):
    """A retry after completion returns the settled row without re-charging."""
    from tests.mock_geidea import state as mock_state

    integration = await integration_type_by_code(db, "DIRECT_API")
    transaction, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.SALE,
        idempotency_key=f"idem-{uuid.uuid4().hex}", amount_minor=1000, currency="SAR",
    )

    async def runner(adapter):
        return await adapter.direct_api_sale(
            OrderContext(
                merchant_reference=transaction.merchant_reference,
                amount_minor=1000, currency="SAR",
            ),
            CARD,
        )

    await execution.execute(db, gateway=geidea, transaction=transaction, runner=runner)
    await db.refresh(transaction)
    assert transaction.status == TransactionStatus.SUCCESS.value
    calls_after_first = len(mock_state.calls)

    await execution.execute(db, gateway=geidea, transaction=transaction, runner=runner)

    assert len(mock_state.calls) == calls_after_first, (
        "a replay re-contacted the gateway"
    )


# ---------------------------------------------------------------------------
# ALLOW_PRODUCTION_GATEWAYS
# ---------------------------------------------------------------------------

async def test_production_credentials_blocked_at_the_adapter_layer(db, geidea):
    """
    §7b: the gate must hold even when the UI is bypassed entirely.

    This calls the adapter directly -- no route, no dependency, no frontend --
    which is precisely the bypass the requirement is about.
    """
    context = GatewayContext(
        gateway_id=geidea.id, gateway_code="geidea", display_name="Geidea",
        credentials={"public_key": "pk", "api_password": "pw"},
        config={"api_base": "http://127.0.0.1:1"},
        timeout_seconds=5.0,
        is_production=True,
        allow_production=False,
    )
    adapter = GeideaAdapter(context)

    with pytest.raises(ProductionGatewayBlocked):
        await adapter.direct_api_sale(
            OrderContext(merchant_reference="X", amount_minor=100, currency="SAR"),
            CARD,
        )
    with pytest.raises(ProductionGatewayBlocked):
        await adapter.create_hpp_session(
            OrderContext(merchant_reference="X", amount_minor=100, currency="SAR")
        )


async def test_production_credentials_allowed_when_the_flag_is_on(db, geidea, mock_gateway):
    context = GatewayContext(
        gateway_id=geidea.id, gateway_code="geidea", display_name="Geidea",
        credentials={"public_key": "pk", "api_password": "pw"},
        config={"api_base": mock_gateway},
        timeout_seconds=5.0,
        is_production=True,
        allow_production=True,
    )
    adapter = GeideaAdapter(context)
    result = await adapter.create_hpp_session(
        OrderContext(merchant_reference="X", amount_minor=100, currency="SAR")
    )
    assert result.redirect_url


# ---------------------------------------------------------------------------
# ALLOW_DIRECT_CARD_ENTRY
# ---------------------------------------------------------------------------

def test_direct_card_entry_gate_refuses_when_disabled(monkeypatch):
    from fastapi import HTTPException

    from app.security.gates import assert_direct_card_entry_allowed, require_direct_card_entry

    monkeypatch.setenv("ALLOW_DIRECT_CARD_ENTRY", "0")
    reload_settings()
    try:
        with pytest.raises(HTTPException) as http_error:
            require_direct_card_entry()
        assert http_error.value.status_code == 403
        assert http_error.value.detail["env_var"] == "ALLOW_DIRECT_CARD_ENTRY"

        with pytest.raises(FeatureDisabled):
            assert_direct_card_entry_allowed()
    finally:
        monkeypatch.undo()
        reload_settings()


def test_direct_card_entry_gate_accepts_both_spellings(monkeypatch):
    """
    The repository's .env.example uses 1/0; §7b describes true/false.

    Both must parse, or a deployment that spells it the other way silently
    reads as "off" -- or worse, as "on".
    """
    from app.security.gates import direct_card_entry_enabled

    for value, expected in (
        ("1", True), ("0", False), ("true", True), ("false", False),
        ("TRUE", True), ("no", False), ("yes", True), ("on", True), ("off", False),
    ):
        monkeypatch.setenv("ALLOW_DIRECT_CARD_ENTRY", value)
        reload_settings()
        assert direct_card_entry_enabled() is expected, f"{value!r} parsed wrongly"
    monkeypatch.undo()
    reload_settings()


# ---------------------------------------------------------------------------
# Benchmark limits
# ---------------------------------------------------------------------------

async def test_benchmark_run_over_the_cap_stops_rather_than_truncating(db, geidea):
    """
    §7b: exceeding the maximum must stop the run, not silently shorten it.

    A run that quietly does 5 of a requested 50 would report as complete and be
    wrong, which is worse than not running at all.
    """
    decision = await check_limits(db, gateway=geidea, requested_count=50)
    assert decision.allowed is False
    assert "BENCHMARK_MAX_TRANSACTIONS_PER_RUN" in (decision.reason or "")

    run = await create_run(
        db, gateway=geidea, integration_type=None, operation=Operation.SALE,
        requested_count=50,
    )
    assert run.status == RunStatus.STOPPED
    assert run.completed_count == 0
    # The reason is persisted, so "why did nothing happen" is answerable later.
    assert run.stop_reason and "exceeds" in run.stop_reason


async def test_benchmark_run_within_the_cap_is_allowed(db, geidea):
    decision = await check_limits(db, gateway=geidea, requested_count=5)
    assert decision.allowed is True

    run = await create_run(
        db, gateway=geidea, integration_type=None, operation=Operation.SALE,
        requested_count=5,
    )
    assert run.status == RunStatus.PENDING
    assert run.stop_reason is None


async def test_benchmark_minimum_interval_is_enforced(db, geidea, monkeypatch):
    monkeypatch.setenv("BENCHMARK_MIN_INTERVAL_SECONDS", "3600")
    reload_settings()
    try:
        first = await create_run(
            db, gateway=geidea, integration_type=None, operation=Operation.SALE,
            requested_count=1,
        )
        assert first.status == RunStatus.PENDING

        second = await create_run(
            db, gateway=geidea, integration_type=None, operation=Operation.SALE,
            requested_count=1,
        )
        assert second.status == RunStatus.STOPPED
        assert "BENCHMARK_MIN_INTERVAL_SECONDS" in (second.stop_reason or "")
    finally:
        monkeypatch.undo()
        reload_settings()


# ---------------------------------------------------------------------------
# Unsupported / undocumented operations
# ---------------------------------------------------------------------------

async def test_undocumented_operations_raise_rather_than_guessing(db, geidea, mock_gateway):
    """
    §0.2: an unverified request shape is never sent to a real payment gateway.

    Tokenize, CIT, MIT and webhook verification have no documented shape in
    this build, so they must refuse with a typed error naming the doc page --
    not send a plausible-looking guess.
    """
    from app.gateways.errors import DocumentationRequiredError

    context = GatewayContext(
        gateway_id=geidea.id, gateway_code="geidea", display_name="Geidea",
        credentials={"public_key": "pk", "api_password": "pw"},
        config={"api_base": mock_gateway}, timeout_seconds=5.0,
    )
    adapter = GeideaAdapter(context)
    order = OrderContext(merchant_reference="X", amount_minor=100, currency="SAR")

    with pytest.raises(DocumentationRequiredError) as exc:
        await adapter.tokenize(CARD)
    assert "save-card" in exc.value.doc_url

    with pytest.raises(DocumentationRequiredError) as exc:
        await adapter.charge_with_token_mit(object(), order)
    assert "merchant-initiated-mit" in exc.value.doc_url

    with pytest.raises(DocumentationRequiredError):
        await adapter.charge_with_token_cit(object(), order)

    with pytest.raises(DocumentationRequiredError) as exc:
        await adapter.verify_webhook({}, b"{}")
    assert "sample-callback-responses" in exc.value.doc_url


def test_capabilities_exclude_the_undocumented_operations():
    """The UI hides what the adapter cannot do, driven by this set (§3)."""
    from app.gateways.base import Capability

    caps = GeideaAdapter.capabilities
    assert Capability.TOKENIZE not in caps
    assert Capability.CHARGE_WITH_TOKEN_CIT not in caps
    assert Capability.CHARGE_WITH_TOKEN_MIT not in caps
    assert Capability.VERIFY_WEBHOOK not in caps
    # And the ones that are implemented are declared.
    assert Capability.DIRECT_API_SALE in caps
    assert Capability.MULTIPLE_PARTIAL_CAPTURES in caps


async def test_unsupported_operation_is_typed_not_an_attribute_error(db, geidea):
    """§3: an adapter that cannot do something raises a catchable typed error."""
    from app.gateways.base import GatewayAdapter

    class Bare(GatewayAdapter):
        code = "bare"
        display_name = "Bare"

    adapter = Bare(
        GatewayContext(
            gateway_id=1, gateway_code="bare", display_name="Bare",
            credentials={}, config={}, timeout_seconds=1.0,
        )
    )
    with pytest.raises(UnsupportedOperationError) as exc:
        await adapter.refund(object())
    assert exc.value.operation == "refund"
