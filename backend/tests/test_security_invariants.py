"""
The invariants that must hold whatever else changes (§0.3, §0.7, §0.8, §7b).

Three of these are structural tests over the source tree rather than runtime
tests. That is deliberate: "no endpoint handler calls httpx directly" and "no
hardcoded domain" are rules about the code, and a rule that is only written in
a docstring is a rule that gets broken by the next person in a hurry.
"""

from __future__ import annotations

import json
import pathlib
import re
import uuid

import pytest
from sqlalchemy import select, text

from app.gateways.results import OrderContext, RawCard
from app.models import ApiCallLog, GatewayCredential, Operation
from app.services import execution
from app.services.bootstrap import integration_type_by_code

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

PAN = "4111111111111111"
CVV = "737"
CARD = RawCard(number=PAN, exp_month=11, exp_year=2031, cvv=CVV, holder_name="Test Payer")


# ---------------------------------------------------------------------------
# Structural rules
# ---------------------------------------------------------------------------

def test_only_the_shared_client_imports_httpx():
    """§0.3: no endpoint handler may call httpx/requests directly."""
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        if path.name == "http_client.py":
            continue
        source = path.read_text()
        if re.search(r"^\s*(import|from)\s+(httpx|requests|aiohttp|urllib3)\b",
                     source, re.MULTILINE):
            offenders.append(str(path.relative_to(APP_ROOT.parent)))
    assert offenders == [], (
        "These modules reach the network outside the shared gateway client, "
        "so their calls would not be logged, timed or redacted: " + ", ".join(offenders)
    )


def test_no_hardcoded_public_domain():
    """
    §7b: PUBLIC_BASE_URL is the only source of gateway-facing URLs.

    A hardcoded domain would silently break any deploy on another host --
    staging would send a production callback URL and nobody would notice until
    the callbacks went missing.
    """
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r"\bbusrapay\.com\b|\bburapay\.com\b", line):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{number}")
    assert offenders == [], "Hardcoded deployment domain found at: " + ", ".join(offenders)


def test_callback_urls_come_from_settings(monkeypatch):
    from app.config import reload_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://staging.example.org/")
    reload_settings()
    try:
        return_url, callback_url = execution.build_urls("geidea")
        assert return_url.startswith("https://staging.example.org/")
        assert callback_url.startswith("https://staging.example.org/")
        # The trailing slash must not produce a double slash: some gateways
        # validate the return URL by exact match.
        assert "//api" not in return_url.replace("https://", "")
    finally:
        monkeypatch.undo()
        reload_settings()


# ---------------------------------------------------------------------------
# Card data never reaches storage
# ---------------------------------------------------------------------------

async def test_pan_and_cvv_never_reach_the_log_tables(db, geidea):
    """
    §0.8, end to end.

    Runs a real sale with a real PAN through the whole stack, then greps every
    stored row -- not just the ones we expect to contain card data -- for the
    PAN and the CVV.
    """
    integration = await integration_type_by_code(db, "DIRECT_API")
    transaction, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.SALE,
        idempotency_key=f"idem-{uuid.uuid4().hex}", amount_minor=2500, currency="SAR",
    )

    async def runner(adapter):
        return await adapter.direct_api_sale(
            OrderContext(
                merchant_reference=transaction.merchant_reference,
                amount_minor=2500, currency="SAR",
                return_url="https://test.example/r", callback_url="https://test.example/c",
            ),
            CARD,
        )

    await execution.execute(db, gateway=geidea, transaction=transaction, runner=runner)
    await db.refresh(transaction)

    logs = list(
        (
            await db.execute(
                select(ApiCallLog).where(ApiCallLog.transaction_id == transaction.id)
            )
        ).scalars()
    )
    assert logs, "the flow must have produced log rows"

    blob = json.dumps([
        {
            "headers": log.request_headers_json,
            "request": log.request_body_json,
            "response": log.response_body_json,
            "response_headers": log.response_headers_json,
            "url": log.url,
            "error": log.error_text,
        }
        for log in logs
    ])
    assert PAN not in blob, "a full PAN reached api_call_logs"
    assert CVV not in blob, "a CVV reached api_call_logs"
    assert "[REDACTED:PAN last4=1111]" in blob, "the PAN was not redacted as expected"
    assert "[REDACTED:CVV]" in blob

    # The permitted card attributes are retained.
    assert transaction.card_last4 == "1111"
    assert transaction.card_exp_month == 11
    assert transaction.card_exp_year == 2031

    # And nothing anywhere else in the database holds them either.
    for table in ("transactions", "api_call_logs", "webhooks_received", "tokens"):
        # Cast the whole row to text so this catches a PAN in any column,
        # including one added later that nobody thought to redact.
        found = await db.scalar(
            text(
                f"SELECT count(*) FROM {table} t "
                f"WHERE CAST(t.* AS text) LIKE :needle"
            ).bindparams(needle=f"%{PAN}%")
        )
        assert found == 0, f"a full PAN was found in {table}"


async def test_authorization_header_is_masked(db, geidea):
    integration = await integration_type_by_code(db, "DIRECT_API")
    transaction, _ = await execution.get_or_create_transaction(
        db, gateway=geidea, integration_type=integration, operation=Operation.SALE,
        idempotency_key=f"idem-{uuid.uuid4().hex}", amount_minor=100, currency="SAR",
    )

    async def runner(adapter):
        return await adapter.direct_api_sale(
            OrderContext(
                merchant_reference=transaction.merchant_reference,
                amount_minor=100, currency="SAR",
            ),
            CARD,
        )

    await execution.execute(db, gateway=geidea, transaction=transaction, runner=runner)

    logs = list(
        (
            await db.execute(
                select(ApiCallLog).where(ApiCallLog.transaction_id == transaction.id)
            )
        ).scalars()
    )
    for log in logs:
        header = (log.request_headers_json or {}).get("Authorization", "")
        assert "******" in header, "the Authorization header was stored unmasked"
        assert "apipassword_test_987654" not in json.dumps(log.request_headers_json)


# ---------------------------------------------------------------------------
# Credentials at rest
# ---------------------------------------------------------------------------

async def test_credentials_are_stored_as_ciphertext(db, geidea):
    """§7b: verified by direct inspection of the column, not via the service."""
    rows = list(
        (
            await db.execute(
                select(GatewayCredential).where(
                    GatewayCredential.gateway_id == geidea.id
                )
            )
        ).scalars()
    )
    assert rows, "the fixture must have stored credentials"

    for row in rows:
        assert "pk_test_abcdef123456" not in row.encrypted_value
        assert "apipassword_test_987654" not in row.encrypted_value
        # Fernet tokens are urlsafe-base64 and start with the version byte 0x80,
        # which encodes as 'gAAAAA'.
        assert row.encrypted_value.startswith("gAAAAA")

    from app.security.credentials import CredentialStore, mask_for_display

    described = await CredentialStore(db).describe(geidea.id)
    for item in described:
        # The masked display shows the last four characters and nothing more.
        assert "pk_test_abcdef123456" != item["masked_value"]
        assert str(item["masked_value"]).startswith("*")

    assert mask_for_display("abcd1234efgh") == "********efgh"
    assert mask_for_display("abc") == "***"


async def test_decrypted_values_round_trip(db, geidea):
    from app.security.credentials import CredentialStore

    resolved = await CredentialStore(db).resolve(geidea.id)
    assert resolved["public_key"] == "pk_test_abcdef123456"
    assert resolved["api_password"] == "apipassword_test_987654"
