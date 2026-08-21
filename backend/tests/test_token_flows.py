"""
Save-card, CIT and MIT (§4g, §4h, §4i).

Sources: docs/geidea/01-tokenization.md, 02-merchant-initiated-mit.md,
03-standalone-save-card.md.

These assert the exact request shapes the documentation specifies, because the
whole reason these flows were blocked until the pages arrived is that guessing a
field name here produces a request Geidea rejects for reasons that look like
"the feature does not work".

They also assert the call counts, which are a headline benchmark result in their
own right: a customer-initiated charge against a stored card costs Geidea ONE
merchant round trip, a merchant-initiated one costs TWO.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select

from app.gateways.errors import GatewayDeclined
from app.gateways.geidea import endpoints as ep
from app.gateways.geidea.signing import mit_signature, save_card_signature
from app.gateways.results import OrderContext
from app.models import ApiCallLog, Operation, Token, TransactionStatus
from app.services import execution
from app.services.bootstrap import integration_type_by_code
from tests.mock_geidea import state as mock_state

PUBLIC_KEY = "pk_test_abcdef123456"
API_PASSWORD = "apipassword_test_987654"


async def _calls(db, transaction_id):
    result = await db.execute(
        select(ApiCallLog)
        .where(ApiCallLog.transaction_id == transaction_id)
        .order_by(ApiCallLog.sequence_number)
    )
    return list(result.scalars().all())


async def _run(db, geidea, *, operation, integration, runner, amount=1000):
    integration_type = await integration_type_by_code(db, integration)
    transaction, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration_type, operation=operation,
        idempotency_key=f"idem-{uuid.uuid4().hex}", amount_minor=amount,
        currency="SAR",
    )

    def order_for(tx):
        return OrderContext(
            merchant_reference=tx.merchant_reference,
            amount_minor=tx.amount_minor,
            currency=tx.currency,
            return_url="https://test.example/return",
            callback_url="https://test.example/callback",
        )

    await execution.execute(
        db, gateway=geidea, transaction=transaction,
        runner=lambda adapter: runner(adapter, order_for(transaction)),
    )
    await db.refresh(transaction)
    return transaction


def _sent(step: str) -> dict:
    """The body the adapter actually sent for a given step."""
    for call in mock_state.calls:
        if call["step"] == step:
            return call["body"] or {}
    raise AssertionError(f"no {step} call was made; saw {mock_state.steps()}")


# ---------------------------------------------------------------------------
# §4g save card
# ---------------------------------------------------------------------------

async def test_save_card_uses_its_own_endpoint_and_signature(db, geidea):
    """
    The save-card flow is a distinct endpoint with a distinct signature.

    Not the ``cardOnFile: true`` payment flow: different path, different
    signature formula, and Geidea voids the underlying authorisation itself.
    """
    transaction = await _run(
        db, geidea,
        operation=Operation.TOKENIZE, integration="TOKENIZE", amount=0,
        runner=lambda adapter, order: adapter.tokenize(None, order),
    )

    assert transaction.status == TransactionStatus.PENDING.value, (
        "no token exists until the customer completes the hosted flow"
    )
    assert transaction.request_count == 1

    calls = await _calls(db, transaction.id)
    assert [c.step_name for c in calls] == ["save_card_session"]
    assert calls[0].url.endswith("/payment-intent/api/v2/direct/session/saveCard")

    body = _sent("save_card_session")
    assert set(body) >= {
        "currency", "merchantReferenceId", "timeStamp", "signature", "cofAgreement",
    }
    # cofAgreement is the NESTED consent shape, used at tokenization time.
    assert body["cofAgreement"]["type"] == ep.AGREEMENT_TYPE_UNSCHEDULED
    assert body["cofAgreement"]["id"] == transaction.merchant_reference

    # The signature is the save-card formula: timeStamp + publicKey + currency.
    expected = save_card_signature(
        timestamp_value=body["timeStamp"], public_key=PUBLIC_KEY,
        currency=body["currency"], api_password=API_PASSWORD,
    )
    assert body["signature"] == expected


async def test_save_card_never_transmits_a_card(db, geidea):
    """
    §0.8, structurally: this flow cannot leak a PAN because it never has one.

    The customer types the card on Geidea's page. Nothing card-shaped should
    appear in the request at all.
    """
    await _run(
        db, geidea,
        operation=Operation.TOKENIZE, integration="TOKENIZE", amount=0,
        runner=lambda adapter, order: adapter.tokenize(None, order),
    )
    body = _sent("save_card_session")
    for forbidden in ("cardNumber", "cvv", "pan", "paymentMethod"):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# §4h CIT
# ---------------------------------------------------------------------------

async def test_cit_is_one_call_carrying_the_token(db, geidea):
    """
    CIT costs ONE merchant round trip.

    It is the ordinary Session Creation call with ``tokenId`` in place of card
    fields; the customer is still present and completes the hosted flow, so the
    transaction stays PENDING until the callback settles it.
    """
    token = Token(
        gateway_id=geidea.id, token_reference="tok_cit_123",
        agreement_reference="AGREEMENT-CIT", card_brand="VISA", card_last4="0010",
        is_active=True,
    )
    db.add(token)
    await db.commit()

    transaction = await _run(
        db, geidea,
        operation=Operation.CIT_CHARGE, integration="TOKEN_CIT",
        runner=lambda adapter, order: adapter.charge_with_token_cit(token, order),
    )

    assert transaction.request_count == 1
    calls = await _calls(db, transaction.id)
    assert [c.step_name for c in calls] == ["create_session"]
    assert transaction.status == TransactionStatus.PENDING.value
    assert transaction.token_reference == "tok_cit_123"

    body = _sent("create_session")
    assert body["tokenId"] == "tok_cit_123"
    assert body["paymentOperation"] == ep.PAYMENT_OPERATION_PAY
    # No card fields: the token stands in for them.
    assert "paymentMethod" not in body


# ---------------------------------------------------------------------------
# §4i MIT
# ---------------------------------------------------------------------------

async def test_mit_is_two_calls_with_two_different_signatures(db, geidea):
    """
    MIT costs TWO merchant round trips, signed two different ways.

    Session generation uses the base signature; the pay/token call uses the
    MIT-specific HMAC over ``publicKey + sessionId + timestamp``. Using one
    formula for both is the documented way to make MIT look broken.
    """
    token = Token(
        gateway_id=geidea.id, token_reference="tok_mit_456",
        agreement_reference="AGREEMENT-MIT-1", card_brand="VISA", card_last4="0010",
        is_active=True,
    )
    db.add(token)
    await db.commit()

    transaction = await _run(
        db, geidea,
        operation=Operation.MIT_CHARGE, integration="TOKEN_MIT",
        runner=lambda adapter, order: adapter.charge_with_token_mit(token, order),
    )

    assert transaction.status == TransactionStatus.SUCCESS.value
    assert transaction.request_count == 2

    calls = await _calls(db, transaction.id)
    assert [c.step_name for c in calls] == ["create_session", "pay_with_token"]
    assert [c.sequence_number for c in calls] == [1, 2]
    assert calls[1].url.endswith("/pgw/api/v2/direct/pay/token")

    # -- step 2.1: session generation --------------------------------------
    session_body = _sent("create_session")
    assert session_body["initiatedBy"] == ep.INITIATED_BY_MERCHANT
    assert session_body["agreementId"] == "AGREEMENT-MIT-1"
    assert session_body["agreementType"] == ep.AGREEMENT_TYPE_UNSCHEDULED
    assert session_body["tokenId"] == "tok_mit_456"
    # MIT uses FLAT agreement fields, not the nested cofAgreement object that
    # tokenization uses. They are not interchangeable.
    assert "cofAgreement" not in session_body

    # -- step 2.2: pay with token ------------------------------------------
    pay_body = _sent("pay_with_token")
    # Lowercase 'i', exactly as the documented sample spells it.
    assert "sessionid" in pay_body
    assert "sessionId" not in pay_body
    assert pay_body["initiatedBy"] == ep.INITIATED_BY_MERCHANT
    assert pay_body["agreementId"] == "AGREEMENT-MIT-1"
    assert pay_body["agreementType"] == ep.AGREEMENT_TYPE_UNSCHEDULED

    expected = mit_signature(
        public_key=PUBLIC_KEY,
        session_id=pay_body["sessionid"],
        timestamp_value=session_body["timestamp"],
        api_password=API_PASSWORD,
    )
    assert pay_body["signature"] == expected, (
        "the MIT signature must cover the timestamp the session was created "
        "with -- pay/token carries no timestamp of its own for Geidea to use"
    )
    # And it must NOT be the base signature.
    assert pay_body["signature"] != session_body["signature"]


async def test_mit_reuses_the_agreement_id_stored_with_the_token(db, geidea):
    """
    Geidea does not issue the agreement id; this platform does, once.

    Every later merchant-initiated charge must replay the exact value recorded
    at tokenization, so it is read from the token rather than regenerated.
    """
    token = Token(
        gateway_id=geidea.id, token_reference="tok_mit_789",
        agreement_reference="AGREEMENT-STABLE", is_active=True,
    )
    db.add(token)
    await db.commit()

    for _ in range(2):
        mock_state.calls.clear()
        await _run(
            db, geidea,
            operation=Operation.MIT_CHARGE, integration="TOKEN_MIT",
            runner=lambda adapter, order: adapter.charge_with_token_mit(token, order),
        )
        assert _sent("create_session")["agreementId"] == "AGREEMENT-STABLE"
        assert _sent("pay_with_token")["agreementId"] == "AGREEMENT-STABLE"


async def test_mit_rejected_when_the_initiator_flag_is_wrong(db, geidea, monkeypatch):
    """
    The mock enforces the documented contract, so a wrong flag fails loudly.

    This is the failure mode the docs warn about: it does not look like a
    signature problem or a network problem, it looks like "MIT is broken".
    """
    token = Token(
        gateway_id=geidea.id, token_reference="tok_bad", is_active=True,
        agreement_reference="AG",
    )
    db.add(token)
    await db.commit()

    from app.gateways.geidea.adapter import GeideaAdapter

    original = GeideaAdapter.charge_with_token_mit

    async def wrong_initiator(self, token_arg, order):
        monkeypatch.setattr(ep, "INITIATED_BY_MERCHANT", "Customer", raising=False)
        return await original(self, token_arg, order)

    monkeypatch.setattr(GeideaAdapter, "charge_with_token_mit", wrong_initiator)

    transaction = await _run(
        db, geidea,
        operation=Operation.MIT_CHARGE, integration="TOKEN_MIT",
        runner=lambda adapter, order: adapter.charge_with_token_mit(token, order),
    )
    assert transaction.status == TransactionStatus.FAILED.value
    assert transaction.error_code == "declined"


# ---------------------------------------------------------------------------
# Basic auth is what the docs specify
# ---------------------------------------------------------------------------

async def test_requests_use_basic_auth_with_public_key_and_api_password(db, geidea):
    """docs/geidea/01: username = public key, password = API password."""
    await _run(
        db, geidea,
        operation=Operation.TOKENIZE, integration="TOKENIZE", amount=0,
        runner=lambda adapter, order: adapter.tokenize(None, order),
    )
    header = ""
    for call in mock_state.calls:
        header = call["headers"].get("authorization", "")
        if header:
            break
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header[6:]).decode()
    assert decoded == f"{PUBLIC_KEY}:{API_PASSWORD}"
