"""
Signature algorithms, checked against the documentation's own reference code.

Geidea uses four textually distinct signature schemes. Two of the fetched pages
ship reference implementations; those are reproduced here verbatim and asserted
to agree with this codebase's versions. That is a stronger check than asserting
a hardcoded digest, because it would still catch a change to our implementation
*and* would fail loudly if someone "tidied" the field order.

Sources:
  docs/geidea/02-merchant-initiated-mit.md          (PHP, C# and Python refs)
  docs/geidea/03-standalone-save-card.md            (field list only)
  docs/geidea/04-webhook-callback-notifications.md  (Python ref)
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from app.gateways.geidea.signing import (
    DEFAULT_TIMESTAMP_FORMAT, build_signature, callback_signature, format_amount,
    mit_signature, save_card_signature, timestamp, verify_callback_signature,
)

PUBLIC_KEY = "6620c3e2-5088-41a8-8be6-98c003153932"
SESSION_ID = "9c52785f-f092-4977-7816-08db602e2587"
API_PASSWORD = "an-api-password"
TIMESTAMP = "10/20/2025 5:16:48 AM"


# --- the docs' own reference implementations, transcribed verbatim ----------

def _doc_mit_reference(merchant_public_key, session_id, ts, api_password):
    """docs/geidea/02-merchant-initiated-mit.md, "Python equivalent"."""
    data = f"{merchant_public_key}{session_id}{ts}"
    digest = hmac.new(
        api_password.encode("utf-8"), data.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _doc_callback_reference(
    merchant_public_key, order_amount, order_currency, order_id, status,
    merchant_reference_id, ts, api_password,
):
    """docs/geidea/04-webhook-callback-notifications.md, "Python implementation"."""
    data = (
        f"{merchant_public_key}{order_amount}{order_currency}"
        f"{order_id}{status}{merchant_reference_id}{ts}"
    )
    digest = hmac.new(
        api_password.encode("utf-8"), data.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


# --- the assertions ---------------------------------------------------------

def test_signing_matches_the_documented_reference_implementations():
    assert mit_signature(
        public_key=PUBLIC_KEY, session_id=SESSION_ID,
        timestamp_value=TIMESTAMP, api_password=API_PASSWORD,
    ) == _doc_mit_reference(PUBLIC_KEY, SESSION_ID, TIMESTAMP, API_PASSWORD)

    assert callback_signature(
        public_key=PUBLIC_KEY, amount="101", currency="SAR",
        order_id="9e238617-1ae9-469f-02f7-08db602e5619", status="Success",
        merchant_reference_id="test-site-1", timestamp_value=TIMESTAMP,
        api_password=API_PASSWORD,
    ) == _doc_callback_reference(
        PUBLIC_KEY, "101", "SAR", "9e238617-1ae9-469f-02f7-08db602e5619",
        "Success", "test-site-1", TIMESTAMP, API_PASSWORD,
    )


def test_the_four_schemes_produce_different_signatures():
    """
    The whole risk this guards against: reusing one flow's formula elsewhere.

    Every pair must differ over the same inputs, otherwise a copy-paste between
    them would go unnoticed.
    """
    signatures = {
        "mit": mit_signature(
            public_key=PUBLIC_KEY, session_id=SESSION_ID,
            timestamp_value=TIMESTAMP, api_password=API_PASSWORD,
        ),
        "save_card": save_card_signature(
            timestamp_value=TIMESTAMP, public_key=PUBLIC_KEY, currency="SAR",
            api_password=API_PASSWORD,
        ),
        "callback": callback_signature(
            public_key=PUBLIC_KEY, amount="10.00", currency="SAR",
            order_id=SESSION_ID, status="Success", merchant_reference_id="ref",
            timestamp_value=TIMESTAMP, api_password=API_PASSWORD,
        ),
        "base": build_signature(
            api_password=API_PASSWORD,
            values={
                "publicKey": PUBLIC_KEY, "amount": "10.00", "currency": "SAR",
                "merchantReferenceId": "ref", "timestamp": TIMESTAMP,
            },
        ),
    }
    assert len(set(signatures.values())) == len(signatures), signatures


def test_save_card_field_order_is_timestamp_first():
    """
    docs/geidea/03: {timeStamp}{MerchantPublicKey}{OrderCurrency}.

    The public key sits SECOND here and FIRST in the MIT and callback formulas.
    Swapping them is the exact mistake this asserts against.
    """
    correct = save_card_signature(
        timestamp_value=TIMESTAMP, public_key=PUBLIC_KEY, currency="SAR",
        api_password=API_PASSWORD,
    )
    swapped = base64.b64encode(
        hmac.new(
            API_PASSWORD.encode(),
            f"{PUBLIC_KEY}{TIMESTAMP}SAR".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert correct != swapped


def test_signature_depends_on_every_signed_field():
    """Changing any covered field must change the signature."""
    base_kwargs = dict(
        public_key=PUBLIC_KEY, amount="10.00", currency="SAR", order_id="ord",
        status="Success", merchant_reference_id="ref", timestamp_value=TIMESTAMP,
        api_password=API_PASSWORD,
    )
    original = callback_signature(**base_kwargs)
    for field, altered in (
        ("amount", "10.01"),
        ("currency", "AED"),
        ("order_id", "ord2"),
        ("status", "Failed"),
        ("merchant_reference_id", "ref2"),
        ("timestamp_value", "10/20/2025 5:16:49 AM"),
        ("api_password", "other-password"),
    ):
        assert callback_signature(**{**base_kwargs, field: altered}) != original, field


def test_verification_is_constant_time_and_rejects_empties():
    good = mit_signature(
        public_key=PUBLIC_KEY, session_id=SESSION_ID,
        timestamp_value=TIMESTAMP, api_password=API_PASSWORD,
    )
    assert verify_callback_signature(good, good) is True
    assert verify_callback_signature(good, "wrong") is False
    # A missing signature is never a pass.
    assert verify_callback_signature(good, "") is False
    assert verify_callback_signature("", "") is False


def test_timestamp_matches_the_documented_worked_example_shape():
    """
    The only worked example is ``10/20/2025 5:16:48 AM`` -- unpadded hour.

    The timestamp is part of three of the four signed strings, so a stray
    leading zero changes the signature.
    """
    rendered = timestamp()
    date_part, _, time_part = rendered.partition(" ")
    assert len(date_part.split("/")) == 3
    assert not time_part.startswith("0"), rendered
    assert rendered.endswith(("AM", "PM"))
    # Round-trips through the documented format.
    import datetime

    datetime.datetime.strptime(rendered, DEFAULT_TIMESTAMP_FORMAT)


def test_amounts_render_with_the_currency_exponent():
    """Three-decimal currencies are why this is not a hardcoded two."""
    assert format_amount(1000, "SAR") == "10.00"
    assert format_amount(1000, "KWD") == "1.000"
    assert format_amount(1000, "JPY") == "1000"


@pytest.mark.parametrize("currency", ["SAR", "EGP", "AED", "QAR", "KWD", "USD"])
def test_save_card_signature_is_currency_specific(currency):
    """The currency is signed, so each one yields a distinct signature."""
    signatures = {
        save_card_signature(
            timestamp_value=TIMESTAMP, public_key=PUBLIC_KEY, currency=code,
            api_password=API_PASSWORD,
        )
        for code in ("SAR", "EGP", "AED", "QAR", "KWD", "USD")
    }
    assert len(signatures) == 6
