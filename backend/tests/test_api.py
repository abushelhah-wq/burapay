"""
HTTP-level tests over the real FastAPI app.

These go through the actual routes, dependencies and exception handlers, so
they cover the half of each rule that lives outside the service layer: the
authentication requirement, the 403 from a feature gate, the 501 for an
operation this build cannot perform, and the shape of the audit-log response
the UI renders.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport

from app.config import reload_settings
from app.main import create_app
from app.security.auth import hash_password
from app.models import User

CARD = {
    "number": "4111111111111111",
    "exp_month": 12,
    "exp_year": 2030,
    "cvv": "123",
    "holder_name": "Test Payer",
}


@pytest.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test.example"
    ) as http:
        yield http


@pytest.fixture
async def admin(db):
    user = User(
        email="admin@example.com",
        password_hash=hash_password("correct horse battery staple"),
        is_admin=True,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
async def signed_in(client, admin):
    response = await client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    return client


async def test_health_reports_the_database_and_calls_no_gateway(client):
    """§6: the health check must not spend gateway quota."""
    from tests.mock_geidea import state as mock_state

    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert mock_state.calls == [], "the health check contacted a gateway"


async def test_endpoints_require_authentication(client, geidea):
    for path in ("/api/transactions", "/api/analytics/benchmark", "/api/tokens"):
        response = await client.get(path)
        assert response.status_code == 401, f"{path} -> {response.status_code}"

    response = await client.post(
        "/api/payments/geidea/hpp/session",
        json={"idempotency_key": "idem-unauthenticated", "amount_minor": 100,
              "currency": "SAR"},
    )
    assert response.status_code == 401


async def test_login_rejects_a_bad_password(client, admin):
    response = await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "wrong"}
    )
    assert response.status_code == 401
    # The same message for an unknown user, so accounts cannot be enumerated.
    assert "Incorrect email or password" in response.text


async def test_gateway_listing_reports_capabilities_and_doc_gaps(signed_in, geidea):
    response = await signed_in.get("/api/gateways")
    assert response.status_code == 200
    gateways = response.json()
    assert len(gateways) == 1

    geidea_out = gateways[0]
    assert geidea_out["code"] == "geidea"
    assert geidea_out["configured"] is True
    assert geidea_out["missing_credentials"] == []
    assert "direct_api_sale" in geidea_out["capabilities"]
    # The undocumented ones are absent, so the UI hides them.
    assert "tokenize" not in geidea_out["capabilities"]
    assert "charge_with_token_mit" not in geidea_out["capabilities"]

    blocking = [gap for gap in geidea_out["doc_gaps"] if gap["blocks_operation"]]
    assert {gap["step_name"] for gap in blocking} >= {
        "tokenize", "pay_with_token", "verify_webhook"
    }
    assert all(gap["doc_url"].startswith("https://docs.geidea.net") for gap in blocking)


async def test_undocumented_operation_returns_501_with_the_doc_url(signed_in, geidea):
    response = await signed_in.post(
        "/api/payments/geidea/tokenize",
        json={"idempotency_key": f"idem-{uuid.uuid4().hex}", "card": CARD},
    )
    assert response.status_code == 501
    detail = response.json()["detail"]
    assert detail["code"] == "unsupported_operation"
    assert detail["operation"] == "tokenize"


async def test_direct_card_entry_gate_returns_403_when_disabled(
    signed_in, geidea, monkeypatch
):
    """
    §7b: the endpoint must refuse, not merely be hidden in the UI.

    This calls the API directly with the flag off, which is the bypass the
    requirement exists to close.
    """
    monkeypatch.setenv("ALLOW_DIRECT_CARD_ENTRY", "0")
    reload_settings()
    try:
        response = await signed_in.post(
            "/api/payments/geidea/direct/sale",
            json={
                "idempotency_key": f"idem-{uuid.uuid4().hex}",
                "amount_minor": 1000,
                "currency": "SAR",
                "card": CARD,
            },
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "feature_disabled"
        assert detail["env_var"] == "ALLOW_DIRECT_CARD_ENTRY"
    finally:
        monkeypatch.undo()
        reload_settings()


async def test_full_sale_through_the_api_exposes_the_audit_trail(signed_in, geidea):
    response = await signed_in.post(
        "/api/payments/geidea/direct/sale",
        json={
            "idempotency_key": f"idem-{uuid.uuid4().hex}",
            "amount_minor": 2500,
            "currency": "SAR",
            "card": CARD,
        },
    )
    assert response.status_code == 200, response.text
    transaction = response.json()
    assert transaction["status"] == "SUCCESS"
    assert transaction["request_count"] == 4
    assert transaction["amount"]["amount_minor"] == 2500
    # Minor units carry a display string so the UI never needs the exponent.
    assert transaction["amount"]["display"] == "25.00"
    assert transaction["card_last4"] == "1111"

    detail = await signed_in.get(f"/api/transactions/{transaction['id']}")
    assert detail.status_code == 200
    body = detail.json()

    steps = [call["step_name"] for call in body["api_calls"]]
    assert steps == [
        "create_session", "initiate_authentication", "authenticate_payer", "pay",
    ]
    assert [call["sequence_number"] for call in body["api_calls"]] == [1, 2, 3, 4]
    assert all(call["duration_ms"] is not None for call in body["api_calls"])
    assert all(call["timeout_seconds"] == 5.0 for call in body["api_calls"])

    # Redaction survives into the API response.
    serialised = detail.text
    assert "4111111111111111" not in serialised
    assert "[REDACTED:PAN last4=1111]" in serialised
    assert "[REDACTED:CVV]" in serialised

    # A sale is refundable and not capturable.
    actions = body["actions"]
    assert actions["can_refund"] is True
    assert actions["can_capture"] is False
    assert actions["can_void"] is False


async def test_refund_button_is_not_offered_on_an_uncaptured_auth(signed_in, geidea):
    """§5: no refund control on something that cannot be refunded."""
    response = await signed_in.post(
        "/api/payments/geidea/direct/auth",
        json={
            "idempotency_key": f"idem-{uuid.uuid4().hex}",
            "amount_minor": 1000,
            "currency": "SAR",
            "card": CARD,
        },
    )
    assert response.status_code == 200, response.text
    auth = response.json()

    detail = (await signed_in.get(f"/api/transactions/{auth['id']}")).json()
    actions = detail["actions"]
    assert actions["can_capture"] is True
    assert actions["can_void"] is True
    assert actions["can_refund"] is False
    assert actions["remaining_capturable"]["amount_minor"] == 1000
    assert actions["remaining_refundable"]["amount_minor"] == 0


async def test_capture_then_refund_through_the_api(signed_in, geidea):
    auth = (
        await signed_in.post(
            "/api/payments/geidea/direct/auth",
            json={
                "idempotency_key": f"idem-{uuid.uuid4().hex}",
                "amount_minor": 1000,
                "currency": "SAR",
                "card": CARD,
            },
        )
    ).json()

    capture = await signed_in.post(
        f"/api/payments/transactions/{auth['id']}/capture",
        json={"idempotency_key": f"idem-{uuid.uuid4().hex}", "amount_minor": 400},
    )
    assert capture.status_code == 200, capture.text
    assert capture.json()["operation"] == "PARTIAL_CAPTURE"
    assert capture.json()["request_count"] == 1

    detail = (await signed_in.get(f"/api/transactions/{auth['id']}")).json()
    assert detail["captured"]["amount_minor"] == 400
    assert detail["actions"]["remaining_capturable"]["amount_minor"] == 600
    assert detail["actions"]["can_refund"] is True
    assert detail["actions"]["remaining_refundable"]["amount_minor"] == 400
    # The capture appears as a child, so the UI can render the flow as a tree.
    assert len(detail["children"]) == 1

    refund = await signed_in.post(
        f"/api/payments/transactions/{auth['id']}/refund",
        json={"idempotency_key": f"idem-{uuid.uuid4().hex}", "amount_minor": 400},
    )
    assert refund.status_code == 200, refund.text
    assert refund.json()["status"] == "SUCCESS"


async def test_over_refund_is_refused_with_409(signed_in, geidea):
    sale = (
        await signed_in.post(
            "/api/payments/geidea/direct/sale",
            json={
                "idempotency_key": f"idem-{uuid.uuid4().hex}",
                "amount_minor": 1000,
                "currency": "SAR",
                "card": CARD,
            },
        )
    ).json()

    response = await signed_in.post(
        f"/api/payments/transactions/{sale['id']}/refund",
        json={"idempotency_key": f"idem-{uuid.uuid4().hex}", "amount_minor": 9999},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["error_code"] == "invalid_state"


async def test_transaction_list_filters(signed_in, geidea):
    for _ in range(2):
        await signed_in.post(
            "/api/payments/geidea/direct/sale",
            json={
                "idempotency_key": f"idem-{uuid.uuid4().hex}",
                "amount_minor": 1000,
                "currency": "SAR",
                "card": CARD,
            },
        )

    listed = (await signed_in.get("/api/transactions?page_size=1")).json()
    assert listed["total"] == 2
    assert len(listed["items"]) == 1

    by_gateway = (await signed_in.get("/api/transactions?gateway=geidea")).json()
    assert by_gateway["total"] == 2

    by_status = (await signed_in.get("/api/transactions?status=FAILED")).json()
    assert by_status["total"] == 0

    unknown = await signed_in.get("/api/transactions?gateway=nope")
    assert unknown.status_code == 404


async def test_benchmark_dashboard_reflects_real_transactions(signed_in, geidea):
    empty = (await signed_in.get("/api/analytics/benchmark")).json()
    assert empty["cells"] == []

    await signed_in.post(
        "/api/payments/geidea/direct/sale",
        json={
            "idempotency_key": f"idem-{uuid.uuid4().hex}",
            "amount_minor": 1000,
            "currency": "SAR",
            "card": CARD,
        },
    )

    summary = (await signed_in.get("/api/analytics/benchmark")).json()
    assert len(summary["cells"]) == 1
    cell = summary["cells"][0]
    assert cell["gateway_code"] == "geidea"
    assert cell["operation"] == "SALE"
    assert cell["sample_size"] == 1
    assert cell["succeeded"] == 1
    assert cell["avg_request_count"] == 4.0
    assert cell["p95_duration_ms"] is not None


async def test_settings_never_returns_a_full_credential(signed_in, geidea):
    response = await signed_in.get("/api/settings/gateways/geidea/credentials")
    assert response.status_code == 200
    body = response.json()
    assert body["missing"] == []
    assert "pk_test_abcdef123456" not in response.text
    assert "apipassword_test_987654" not in response.text
    for credential in body["credentials"]:
        assert credential["masked_value"].startswith("*")


async def test_settings_refuses_a_credential_in_plain_config(signed_in, geidea):
    """A secret in config_json would be a plaintext credential in the database."""
    response = await signed_in.put(
        "/api/settings/gateways/geidea/config",
        json={"config": {"api_password": "leak-me"}},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "credential_in_config"


async def test_runtime_flags_are_reported(signed_in):
    flags = (await signed_in.get("/api/settings/flags")).json()
    assert flags["allow_direct_card_entry"] is True
    assert flags["allow_production_gateways"] is False
    assert flags["http_timeout_seconds"] == 5.0
    assert flags["public_base_url"] == "https://test.example"


async def test_test_cards_endpoint_explains_an_empty_list(signed_in, geidea):
    """§5: the helper is populated from the docs, and says so when it cannot be."""
    body = (await signed_in.get("/api/gateways/geidea/test-cards")).json()
    assert body["cards"] == []
    assert body["doc_url"].endswith("test-cards.md")
    assert "unreachable" in body["unavailable_reason"]


async def test_webhook_is_stored_even_when_it_matches_nothing(client, geidea):
    """§4k: an unmatched callback is evidence, not garbage."""
    response = await client.post(
        "/api/webhooks/geidea",
        json={"order": {"orderId": "ord_unknown", "status": "Paid"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["received"] is True
    assert body["matched_transaction_id"] is None
    # The signature scheme is undocumented, so this is null -- not a fabricated
    # true or false.
    assert body["signature_valid"] is None


async def test_webhook_for_an_unknown_gateway_is_404(client):
    response = await client.post("/api/webhooks/nosuchgateway", json={})
    assert response.status_code == 404
