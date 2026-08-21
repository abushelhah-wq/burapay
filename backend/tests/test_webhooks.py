"""
Callback handling (§4k), against the documented signature scheme.

Source: docs/geidea/04-webhook-callback-notifications.md.

The behaviours under test are the ones that decide whether money is booked
correctly:

* an unverified callback changes nothing,
* a verified callback still has to pass the documented success checklist,
* ``InProgress`` is not a failure,
* a payload full of failed attempts ending in a success is a success,
* a replay does not double-process.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.gateways.geidea.signing import callback_signature
from app.models import Operation, Token, TransactionStatus, WebhookReceived
from app.services import execution
from app.services.bootstrap import integration_type_by_code
from app.services.webhooks import process_webhook

PUBLIC_KEY = "pk_test_abcdef123456"
API_PASSWORD = "apipassword_test_987654"
TIMESTAMP = "10/20/2025 5:16:48 AM"


def build_callback(
    *,
    order_id: str,
    merchant_reference: str,
    amount: str = "10.00",
    currency: str = "SAR",
    status: str = "Success",
    detailed_status: str = "Paid",
    response_code: str = "000",
    detailed_message: str = "The operation was successful",
    token_id: str | None = None,
    transactions: list | None = None,
    sign: bool = True,
    signature: str | None = None,
) -> dict:
    """A callback shaped like the worked sample in docs/geidea/01."""
    order: dict = {
        "orderId": order_id,
        "merchantReferenceId": merchant_reference,
        "amount": amount,
        "totalAmount": amount,
        "currency": currency,
        "status": status,
        "detailedStatus": detailed_status,
        "transactions": transactions if transactions is not None else [
            {
                "transactionId": f"txn_{order_id}",
                "type": "Pay",
                "status": "Success",
                "amount": amount,
                "paymentMethod": {
                    "type": "Card",
                    "brand": "visa",
                    "maskedCardNumber": "444000******0010",
                    "expiryDate": {"month": 1, "year": 39},
                },
                "codes": {
                    "responseCode": response_code,
                    "responseMessage": "Success" if response_code == "000" else "Failed",
                    "detailedResponseCode": response_code,
                    "detailedResponseMessage": detailed_message,
                },
            }
        ],
    }
    if token_id is not None:
        order["tokenId"] = token_id
        order["cardOnFile"] = True

    body: dict = {
        "order": order,
        "timestamp": TIMESTAMP,
        "responseCode": response_code,
        "responseMessage": "Success" if response_code == "000" else "Failed",
        "detailedResponseCode": response_code,
        "detailedResponseMessage": detailed_message,
    }

    if signature is not None:
        body["signature"] = signature
    elif sign:
        body["signature"] = callback_signature(
            public_key=PUBLIC_KEY,
            amount=amount,
            currency=currency,
            order_id=order_id,
            status=status,
            merchant_reference_id=merchant_reference,
            timestamp_value=TIMESTAMP,
            api_password=API_PASSWORD,
        )
    return body


async def make_pending(db, geidea, *, operation=Operation.SALE, amount=1000,
                       integration="HPP"):
    integration_type = await integration_type_by_code(db, integration)
    transaction, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration_type, operation=operation,
        idempotency_key=f"idem-{uuid.uuid4().hex}", amount_minor=amount,
        currency="SAR",
    )
    transaction.gateway_order_id = f"ord_{uuid.uuid4().hex[:12]}"
    await db.commit()
    return transaction


async def deliver(db, geidea, body) -> WebhookReceived:
    import json

    raw = json.dumps(body).encode()
    record = await process_webhook(
        db, gateway=geidea, headers={"content-type": "application/json"},
        body_bytes=raw, body_json=body,
    )
    await db.commit()
    return record


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

async def test_correctly_signed_callback_settles_the_transaction(db, geidea):
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is True
    assert record.matched_transaction_id == transaction.id
    assert transaction.status == TransactionStatus.SUCCESS.value
    assert transaction.completed_at is not None


async def test_unsigned_callback_changes_nothing(db, geidea):
    """An unauthenticated payload is stored and ignored, never acted on."""
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        sign=False,
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is False
    # Stored as evidence...
    assert record.id is not None
    assert record.matched_transaction_id == transaction.id
    # ...but it moved nothing.
    assert transaction.status == TransactionStatus.PENDING.value


async def test_forged_signature_changes_nothing(db, geidea):
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        signature="bm90LWEtcmVhbC1zaWduYXR1cmU=",
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is False
    assert transaction.status == TransactionStatus.PENDING.value


async def test_tampered_amount_breaks_the_signature(db, geidea):
    """
    The signature covers the amount, so changing it must invalidate it.

    This is the property that makes the check worth doing at all.
    """
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        amount="10.00",
    )
    body["order"]["amount"] = "9999.00"

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is False
    assert transaction.status == TransactionStatus.PENDING.value


# ---------------------------------------------------------------------------
# The documented success checklist
# ---------------------------------------------------------------------------

async def test_authentic_callback_with_a_decline_code_is_failed_not_successful(db, geidea):
    """
    §4k: "signature valid" does not mean "payment succeeded".

    A verified callback reporting responseCode 999 is an authentic failure
    notice. Booking it as revenue because the signature checked out is exactly
    the mistake the checklist exists to prevent.
    """
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        status="Failed",
        detailed_status="Failed",
        response_code="999",
        detailed_message="Insufficient funds",
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is True
    assert transaction.status == TransactionStatus.FAILED.value
    assert transaction.error_code == "999"


async def test_in_progress_callback_leaves_the_transaction_pending(db, geidea):
    """
    ``InProgress`` is documented as non-terminal.

    The customer may still be on the 3DS page. Forcing it to FAILED would kill
    a payment that is still live.
    """
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        status="InProgress",
        detailed_status="InProgress",
        response_code="000",
        detailed_message="Still in progress",
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is True
    assert transaction.status == TransactionStatus.PENDING.value


async def test_multiple_attempts_ending_in_success_is_a_success(db, geidea):
    """
    One callback consolidates every attempt against an order.

    Several failed sub-transactions followed by a successful one is a
    successful payload. Reading a sub-transaction's status instead of the outer
    order's would report a completed payment as failed.
    """
    transaction = await make_pending(db, geidea)
    attempts = [
        {
            "transactionId": "txn_fail_1", "type": "Pay", "status": "Failed",
            "amount": "10.00",
            "codes": {"responseCode": "999", "responseMessage": "Failed",
                      "detailedResponseCode": "999",
                      "detailedResponseMessage": "Do not honour"},
        },
        {
            "transactionId": "txn_fail_2", "type": "Pay", "status": "Failed",
            "amount": "10.00",
            "codes": {"responseCode": "999", "responseMessage": "Failed",
                      "detailedResponseCode": "999",
                      "detailedResponseMessage": "Do not honour"},
        },
        {
            "transactionId": "txn_ok", "type": "Pay", "status": "Success",
            "amount": "10.00",
            "codes": {"responseCode": "000", "responseMessage": "Success",
                      "detailedResponseCode": "000",
                      "detailedResponseMessage": "The operation was successful"},
        },
    ]
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        transactions=attempts,
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is True
    assert transaction.status == TransactionStatus.SUCCESS.value
    # The whole history is retained as evidence even though only the outer
    # status drove the decision.
    stored = record.body_json["order"]["transactions"]
    assert len(stored) == 3


async def test_amount_mismatch_is_not_booked_as_success(db, geidea):
    """
    A callback that verifies but describes a different amount is not trusted.

    The signature proves who sent it, not that it is about this order.
    """
    transaction = await make_pending(db, geidea, amount=1000)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        amount="250.00",
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is True
    assert transaction.status == TransactionStatus.PENDING.value


# ---------------------------------------------------------------------------
# Replays and settled transactions
# ---------------------------------------------------------------------------

async def test_replayed_callback_is_not_double_processed(db, geidea):
    transaction = await make_pending(db, geidea)
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
    )

    first = await deliver(db, geidea, body)
    await db.refresh(transaction)
    completed_at = transaction.completed_at
    assert transaction.status == TransactionStatus.SUCCESS.value

    second = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert second.duplicate_of_id == first.id
    assert transaction.status == TransactionStatus.SUCCESS.value
    assert transaction.completed_at == completed_at

    both = (
        await db.execute(
            select(WebhookReceived).where(WebhookReceived.gateway_id == geidea.id)
        )
    ).scalars().all()
    assert len(both) == 2, "both deliveries are retained as evidence"


async def test_callback_for_a_settled_transaction_does_not_reopen_it(db, geidea):
    transaction = await make_pending(db, geidea)
    transaction.status = TransactionStatus.FAILED.value
    await db.commit()

    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
    )
    await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert transaction.status == TransactionStatus.FAILED.value


# ---------------------------------------------------------------------------
# Token capture (§4g)
# ---------------------------------------------------------------------------

async def test_token_from_callback_is_stored_with_its_agreement(db, geidea):
    """
    The token arrives on the callback, not in the response to our own call.

    The agreement identifier is ours, generated at tokenization, and must be
    replayed verbatim on every later merchant-initiated charge -- so it is
    stored alongside the token rather than regenerated.
    """
    transaction = await make_pending(
        db, geidea, operation=Operation.TOKENIZE, amount=0, integration="TOKENIZE"
    )
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        amount="0",
        token_id="5c430263-0fff-4a1a-f0f7-08db59cbdec9",
    )

    record = await deliver(db, geidea, body)
    await db.refresh(transaction)

    assert record.signature_valid is True

    token = (
        await db.execute(select(Token).where(Token.gateway_id == geidea.id))
    ).scalar_one()
    assert token.token_reference == "5c430263-0fff-4a1a-f0f7-08db59cbdec9"
    assert token.agreement_reference == transaction.merchant_reference
    assert token.created_from_transaction_id == transaction.id
    # §0.8: only the permitted card attributes, taken from the masked number.
    assert token.card_brand == "VISA"
    assert token.card_last4 == "0010"
    assert token.card_exp_month == 1
    assert token.card_exp_year == 2039
    assert transaction.token_reference == token.token_reference


async def test_unverified_callback_does_not_store_a_token(db, geidea):
    """A token is a credential. An unauthenticated payload does not create one."""
    transaction = await make_pending(
        db, geidea, operation=Operation.TOKENIZE, amount=0, integration="TOKENIZE"
    )
    body = build_callback(
        order_id=transaction.gateway_order_id,
        merchant_reference=transaction.merchant_reference,
        amount="0",
        token_id="forged-token",
        sign=False,
    )

    await deliver(db, geidea, body)

    tokens = (
        await db.execute(select(Token).where(Token.gateway_id == geidea.id))
    ).scalars().all()
    assert tokens == []
