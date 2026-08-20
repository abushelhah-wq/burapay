"""
Adapter tests.

Every adapter is driven against a mock transport that answers its documented
endpoints with correctly shaped responses. That verifies request construction,
response parsing, call sequencing, the timed/setup split and the status mapping
without a single sandbox credential.

What it does not verify: that a real gateway accepts these requests. Only a live
sandbox run does that, which is why the adapters carry explicit notes about the
details that could not be confirmed from the public documentation.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import httpx
import pytest

from app.gateways.base import Card, PaymentRequest
from app.gateways.http import InstrumentedClient
from app.gateways.registry import ADAPTERS, build_adapter
from app.models.enums import NormalizedOperation, TransactionStatus

CARD = Card(number="4111111111111111", month="12", year="2030", cvc="123")


def request_for(**overrides: Any) -> PaymentRequest:
    payload = dict(amount=1.00, currency="SAR", reference="burapay-test-001",
                   description="test", return_url="https://busrapay.test/return",
                   webhook_url="https://busrapay.test/api/webhooks/x", card=CARD)
    payload.update(overrides)
    return PaymentRequest(**payload)


def client_for(adapter, handler: Callable[[httpx.Request], httpx.Response],
               recorder: List[httpx.Request] | None = None) -> InstrumentedClient:
    """An instrumented client with the adapter's real auth and headers, mocked transport."""
    def capture(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        return handler(request)

    return InstrumentedClient(
        gateway_code=adapter.code,
        client=httpx.AsyncClient(transport=httpx.MockTransport(capture),
                                 headers=adapter.default_headers(), auth=adapter.auth()),
        known_secrets=adapter.known_secrets())


def route(routes: Dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Match on a path fragment, optionally prefixed with a method: ``"GET /v1/x"``.

    The first key that matches wins, so a method-qualified key placed above a bare
    one lets a test answer a POST and a GET to the same path differently.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, response in routes.items():
            method, _, fragment = key.rpartition(" ")
            if method and method.upper() != request.method:
                continue
            if fragment in url:
                return response() if callable(response) else response
        return httpx.Response(404, json={"error": f"unmocked: {request.method} {url}"})
    return handler


class TestRegistry:
    def test_every_specified_gateway_has_an_adapter(self):
        for code in ("geidea", "stripe", "adyen", "checkout", "hyperpay", "moyasar"):
            assert code in ADAPTERS

    def test_mockpay_exists_for_credential_free_development(self):
        assert "mockpay" in ADAPTERS

    def test_credential_fields_are_declared_for_every_adapter(self):
        for code, cls in ADAPTERS.items():
            assert cls.credential_fields, f"{code} declares no credential fields"

    def test_secret_fields_are_marked_secret(self):
        """Anything that authenticates must be flagged, or it will be returned unmasked."""
        for code, cls in ADAPTERS.items():
            if code == "mockpay":
                continue
            secrets = cls.secret_field_names()
            assert secrets, f"{code} marks no field as secret"

    def test_unconfigured_adapter_reports_what_is_missing(self):
        adapter = build_adapter("geidea", {})
        assert adapter.is_configured() is False
        assert "merchant_public_key" in adapter.missing_credentials()

    def test_currency_support_is_declared_not_assumed(self):
        adapter = build_adapter("moyasar", {"secret_key": "sk"})
        assert adapter.supports_currency("SAR") is True
        assert adapter.supports_currency("GBP") is False


class TestGeidea:
    def adapter(self):
        return build_adapter("geidea", {"merchant_public_key": "pk_test",
                                        "api_password": "pw_test",
                                        "api_base": "https://geidea.test"})

    def test_signature_is_deterministic_and_keyed_by_the_api_password(self):
        first = self.adapter()._signature(amount=1.0, currency="SAR", reference="r",
                                          timestamp="2026-01-01T00:00:00")
        second = self.adapter()._signature(amount=1.0, currency="SAR", reference="r",
                                           timestamp="2026-01-01T00:00:00")
        assert first == second
        other = build_adapter("geidea", {"merchant_public_key": "pk_test",
                                         "api_password": "different"})
        assert other._signature(amount=1.0, currency="SAR", reference="r",
                                timestamp="2026-01-01T00:00:00") != first

    def test_region_selects_the_host(self):
        assert "ksamerchant" in build_adapter("geidea", {"region": "ksa"}).base
        assert "geidea.ae" in build_adapter("geidea", {"region": "uae"}).base
        assert build_adapter("geidea", {"api_base": "https://x.test"}).base == "https://x.test"

    async def test_hpp_session_creation(self):
        adapter = self.adapter()
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "/payment-intent/api/v2/direct/session": httpx.Response(200, json={
                "responseCode": "000",
                "session": {"id": "sess_1", "redirectUrl": "https://geidea.test/pay/1"}}),
        }), requests)

        session = await adapter.create_hpp_session(client, request_for())
        assert session.redirect_url == "https://geidea.test/pay/1"
        assert session.gateway_reference == "sess_1"

        body = json.loads(requests[0].content)
        assert body["merchantReferenceId"] == "burapay-test-001"
        assert body["signature"] and body["timestamp"]
        assert client.measurements[0].normalized_operation is NormalizedOperation.SESSION_CREATION
        await client.client.aclose()

    async def test_hpp_mounts_the_checkout_script_when_no_url_is_returned(self):
        """The real sandbox returns a session with no redirect URL — that is normal.

        Geidea's hosted page is a component started with the session id, not a
        destination, so the adapter hands the browser a widget rather than failing.
        """
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/payment-intent/api/v2/direct/session": httpx.Response(200, json={
                "responseCode": "000",
                # The field list a live KSA sandbox actually returns: an id, an order,
                # tokens, the echoed URLs — and no redirect URL anywhere.
                "session": {"id": "sess_live", "status": "Initiated",
                            "amount": 1.0, "currency": "SAR",
                            "callbackUrl": "https://busrapay.test/api/webhooks/geidea",
                            "returnUrl": "https://busrapay.test/return",
                            "merchantPublicKey": "pk_test", "order": None,
                            "tokenId": None, "cardOnFile": False}}),
        }))

        session = await adapter.create_hpp_session(client, request_for())
        assert session.mode == "widget"
        assert session.gateway_reference == "sess_live"
        assert session.context["session_id"] == "sess_live"
        assert session.context["script_url"].endswith("geideaCheckout.min.js")
        await client.client.aclose()

    async def test_hpp_still_redirects_when_an_account_returns_a_url(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/payment-intent/api/v2/direct/session": httpx.Response(200, json={
                "responseCode": "000",
                "session": {"id": "sess_1", "redirectUrl": "https://geidea.test/pay/1"}}),
        }))
        session = await adapter.create_hpp_session(client, request_for())
        assert session.mode == "redirect"
        assert session.redirect_url == "https://geidea.test/pay/1"
        await client.client.aclose()

    def test_the_script_url_follows_the_region_and_can_be_overridden(self):
        assert "ksamerchant" in build_adapter(
            "geidea", {"merchant_public_key": "p", "api_password": "p"})._hpp_script_url()
        assert "geidea.ae" in build_adapter(
            "geidea", {"merchant_public_key": "p", "api_password": "p",
                       "region": "uae"})._hpp_script_url()
        # The setting the error message points at must actually be read.
        assert build_adapter("geidea", {"merchant_public_key": "p", "api_password": "p",
                                        "hpp_url": "https://custom.test/x.js"}
                             )._hpp_script_url() == "https://custom.test/x.js"

    async def test_direct_runs_the_documented_four_call_sequence(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "sess_1"}}),
            "/authenticate/initiate": httpx.Response(200, json={"responseCode": "000",
                                                                "status": "challenge"}),
            "/authenticate/payer": httpx.Response(200, json={"responseCode": "000"}),
            "/direct/pay": httpx.Response(200, json={"responseCode": "000",
                                                     "order": {"orderId": "ord_1",
                                                               "status": "Success"}}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert result.gateway_reference == "ord_1"
        assert len(client.timed_measurements) == 4
        await client.client.aclose()

    async def test_frictionless_response_skips_authenticate_payer(self):
        """The undocumented question this platform exists to answer, measured not assumed."""
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "sess_1"}}),
            "/authenticate/initiate": httpx.Response(200, json={
                "responseCode": "000", "status": "Not Enrolled"}),
            "/direct/pay": httpx.Response(200, json={"responseCode": "000",
                                                     "order": {"orderId": "ord_1",
                                                               "status": "Success"}}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert len(client.timed_measurements) == 3
        await client.client.aclose()

    async def test_force_full_3ds_keeps_the_documented_four(self):
        adapter = build_adapter("geidea", {"merchant_public_key": "pk", "api_password": "pw",
                                           "api_base": "https://geidea.test",
                                           "force_full_3ds": "true"})
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "s"}}),
            "/authenticate/initiate": httpx.Response(200, json={"responseCode": "000",
                                                                "status": "Not Enrolled"}),
            "/authenticate/payer": httpx.Response(200, json={"responseCode": "000"}),
            "/direct/pay": httpx.Response(200, json={"responseCode": "000",
                                                     "order": {"orderId": "o",
                                                               "status": "Success"}}),
        }))
        await adapter.process_direct_payment(client, request_for())
        assert len(client.timed_measurements) == 4
        await client.client.aclose()

    async def test_a_200_carrying_a_decline_is_a_failure(self):
        """Geidea reports the outcome in responseCode, not in the HTTP status."""
        from app.core.errors import GatewayError
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "123",
                                                         "responseMessage": "Declined"}),
        }))
        with pytest.raises(GatewayError) as excinfo:
            await adapter.create_hpp_session(client, request_for())
        assert excinfo.value.gateway_code == "123"
        assert client.measurements[0].success is False
        await client.client.aclose()


class TestGeideaTokenisation:
    """Card on file: store a card in one payment, charge it in a later one."""

    def adapter(self, **extra):
        values = {"merchant_public_key": "pk_test", "api_password": "pw_test",
                  "api_base": "https://geidea.test"}
        values.update(extra)
        return build_adapter("geidea", values)

    def test_all_three_modes_are_declared(self):
        assert set(self.adapter().supported_payment_modes) == {
            "standard", "store_card", "token"}

    def test_tokenisation_fields_are_optional(self):
        """Geidea must still work for ordinary payments with no agreement configured."""
        adapter = self.adapter()
        assert adapter.is_configured() is True
        assert "agreement_id" not in adapter.missing_credentials()
        assert "token_id" not in adapter.missing_credentials()

    async def test_store_card_asks_the_session_to_keep_the_card(self):
        adapter = self.adapter()
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "s1"}}),
            "/authenticate/initiate": httpx.Response(200, json={"responseCode": "000",
                                                                "status": "Not Enrolled"}),
            "/direct/pay": httpx.Response(200, json={
                "responseCode": "000",
                "order": {"orderId": "ord_1", "status": "Success", "tokenId": "tok_9",
                          "paymentMethod": {"maskedCardNumber": "411111******1111",
                                            "brand": "Visa"}}}),
        }), requests)

        result = await adapter.process_direct_payment(
            client, request_for(payment_mode="store_card"))

        # cardOnFile has to be on the session — it cannot be added at authorisation.
        assert json.loads(requests[0].content)["cardOnFile"] is True
        assert result.status is TransactionStatus.SUCCESS
        assert result.stored_token == "tok_9"
        assert result.stored_token_hint == "Visa card ending 1111"
        await client.client.aclose()

    async def test_standard_mode_does_not_store_the_card(self):
        adapter = self.adapter()
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "s1"}}),
            "/authenticate/initiate": httpx.Response(200, json={"responseCode": "000",
                                                                "status": "Not Enrolled"}),
            "/direct/pay": httpx.Response(200, json={"responseCode": "000",
                                                     "order": {"orderId": "o",
                                                               "status": "Success"}}),
        }), requests)
        result = await adapter.process_direct_payment(client, request_for())
        assert "cardOnFile" not in json.loads(requests[0].content)
        assert result.stored_token is None
        await client.client.aclose()

    async def test_store_card_says_so_when_no_token_comes_back(self):
        """A payment that succeeds without tokenising leaves nothing to charge later."""
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "s1"}}),
            "/authenticate/initiate": httpx.Response(200, json={"responseCode": "000",
                                                                "status": "Not Enrolled"}),
            "/direct/pay": httpx.Response(200, json={"responseCode": "000",
                                                     "order": {"orderId": "o",
                                                               "status": "Success"}}),
        }))
        result = await adapter.process_direct_payment(
            client, request_for(payment_mode="store_card"))
        assert result.status is TransactionStatus.SUCCESS
        assert result.stored_token is None
        assert "no card token" in (result.gateway_message or "")
        await client.client.aclose()

    async def test_stored_token_payment_is_two_calls_and_sends_no_card(self):
        adapter = self.adapter(token_id="tok_9", agreement_id="agr_1",
                               agreement_type="unscheduled")
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "s2"}}),
            "/direct/pay/token": httpx.Response(200, json={
                "responseCode": "000",
                "order": {"orderId": "ord_2", "status": "Success"}}),
        }), requests)

        result = await adapter.process_direct_payment(
            client, request_for(payment_mode="token", card=None))

        assert result.status is TransactionStatus.SUCCESS
        # Geidea needs a fresh session per charge, so this is two calls, not one.
        assert len(client.timed_measurements) == 2
        body = json.loads(requests[1].content)
        assert body["tokenId"] == "tok_9"
        assert body["agreementId"] == "agr_1"
        assert body["agreementType"] == "unscheduled"
        assert body["initiatedBy"] == "Merchant"
        # The whole point: no card details travel with a merchant-initiated charge.
        for forbidden in ("cardNumber", "cvv", "paymentMethod"):
            assert forbidden not in body
        await client.client.aclose()

    async def test_stored_token_payment_refuses_without_the_agreement(self):
        from app.core.errors import NotConfigured
        adapter = self.adapter(token_id="tok_9")          # no agreement id
        client = client_for(adapter, route({}))
        with pytest.raises(NotConfigured) as excinfo:
            await adapter.process_direct_payment(
                client, request_for(payment_mode="token", card=None))
        assert "Agreement ID" in str(excinfo.value)
        # Nothing was sent: the refusal happens before any call.
        assert client.measurements == []
        await client.client.aclose()

    async def test_stored_token_payment_refuses_without_a_token(self):
        from app.core.errors import NotConfigured
        adapter = self.adapter(agreement_id="agr_1")      # no token
        client = client_for(adapter, route({}))
        with pytest.raises(NotConfigured) as excinfo:
            await adapter.process_direct_payment(
                client, request_for(payment_mode="token", card=None))
        assert "Card token ID" in str(excinfo.value)
        await client.client.aclose()

    async def test_the_initiator_is_configurable(self):
        """A cardholder picking a saved card is not a merchant-initiated charge."""
        adapter = self.adapter(token_id="t", agreement_id="a",
                               card_on_file_initiator="Customer")
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "/direct/session": httpx.Response(200, json={"responseCode": "000",
                                                         "session": {"id": "s"}}),
            "/direct/pay/token": httpx.Response(200, json={"responseCode": "000",
                                                           "order": {"orderId": "o",
                                                                     "status": "Success"}}),
        }), requests)
        await adapter.process_direct_payment(
            client, request_for(payment_mode="token", card=None))
        assert json.loads(requests[1].content)["initiatedBy"] == "Customer"
        await client.client.aclose()

    async def test_gateways_without_the_capability_refuse_the_mode(self):
        from app.core.errors import NotSupported
        adapter = build_adapter("stripe", {"secret_key": "sk"})
        with pytest.raises(NotSupported):
            adapter.require_supports_mode("token")


class TestStripe:
    def adapter(self):
        return build_adapter("stripe", {"secret_key": "sk_test_x",
                                        "api_base": "https://stripe.test",
                                        "webhook_secret": "whsec_test"})

    async def test_checkout_session_carries_the_placeholder_success_url(self):
        adapter = self.adapter()
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "/v1/checkout/sessions": httpx.Response(200, json={
                "id": "cs_1", "url": "https://stripe.test/c/cs_1"}),
        }), requests)
        session = await adapter.create_hpp_session(client, request_for())
        assert session.redirect_url == "https://stripe.test/c/cs_1"
        assert "CHECKOUT_SESSION_ID" in requests[0].content.decode()
        await client.client.aclose()

    async def test_direct_charges_a_token_and_excludes_tokenization_from_latency(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/v1/payment_methods": httpx.Response(200, json={"id": "pm_1"}),
            "/v1/payment_intents": httpx.Response(200, json={"id": "pi_1",
                                                             "status": "succeeded"}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert len(client.measurements) == 2
        # Tokenization is a browser step in a real integration, so it must not be
        # charged to Stripe's server-side latency.
        assert len(client.timed_measurements) == 1
        await client.client.aclose()

    async def test_decline_is_mapped_and_the_gateway_code_kept(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/v1/payment_methods": httpx.Response(200, json={"id": "pm_1"}),
            "/v1/payment_intents": httpx.Response(200, json={
                "id": "pi_1", "status": "requires_payment_method",
                "last_payment_error": {"decline_code": "insufficient_funds",
                                       "message": "Card declined"}}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.DECLINED
        assert result.gateway_code == "insufficient_funds"
        await client.client.aclose()

    def test_webhook_signature_verification(self):
        import hashlib
        import hmac
        adapter = self.adapter()
        body = b'{"type":"checkout.session.completed","data":{"object":{"id":"cs_1"}}}'
        signature = hmac.new(b"whsec_test", b"12345." + body, hashlib.sha256).hexdigest()
        parsed = adapter.parse_webhook({"stripe-signature": f"t=12345,v1={signature}"}, body)
        assert parsed["signature_verified"] is True
        assert parsed["event_type"] == "checkout.session.completed"

    def test_bad_signature_is_rejected(self):
        adapter = self.adapter()
        parsed = adapter.parse_webhook({"stripe-signature": "t=1,v1=deadbeef"}, b"{}")
        assert parsed["signature_verified"] is False

    def test_missing_secret_reads_as_unknown_not_verified(self):
        adapter = build_adapter("stripe", {"secret_key": "sk"})
        assert adapter.parse_webhook({}, b"{}")["signature_verified"] is None


class TestAdyen:
    def adapter(self):
        return build_adapter("adyen", {"api_key": "key", "merchant_account": "Merchant",
                                       "api_base": "https://adyen.test"})

    async def test_sessions_flow_hands_back_a_widget_not_a_redirect(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/sessions": httpx.Response(200, json={"id": "sess_1", "sessionData": "abc"}),
        }))
        session = await adapter.create_hpp_session(client, request_for())
        assert session.mode == "widget"
        assert "adyen" in session.redirect_url
        await client.client.aclose()

    async def test_confirmation_makes_no_call_because_none_is_documented(self):
        """Adyen documents no status endpoint for Sessions; inventing one would lie."""
        adapter = self.adapter()
        client = client_for(adapter, route({}))
        result = await adapter.confirm_hpp_payment(client, request_for(),
                                                   {"session_id": "sess_1"}, {})
        assert result.status is TransactionStatus.PENDING
        assert client.measurements == []
        assert "webhook" in result.gateway_message.lower()
        await client.client.aclose()

    async def test_direct_authorisation(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/payments": httpx.Response(200, json={"resultCode": "Authorised",
                                                   "pspReference": "psp_1"}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert result.gateway_reference == "psp_1"
        await client.client.aclose()

    async def test_refusal_is_a_decline_with_the_reason_preserved(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/payments": httpx.Response(200, json={"resultCode": "Refused",
                                                   "refusalReason": "Not enough balance",
                                                   "pspReference": "psp_2"}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.DECLINED
        assert "Not enough balance" in result.gateway_message
        await client.client.aclose()

    def test_hmac_verification_uses_the_documented_field_order(self):
        adapter = build_adapter("adyen", {"api_key": "k", "merchant_account": "M",
                                          "hmac_key": "00112233445566778899aabbccddeeff"})
        import base64
        import hashlib
        import hmac as hmac_module
        payload = "psp_1::M:ref_1:100:SAR:AUTHORISATION:true"
        signature = base64.b64encode(hmac_module.new(
            bytes.fromhex("00112233445566778899aabbccddeeff"),
            payload.encode(), hashlib.sha256).digest()).decode()
        body = json.dumps({"notificationItems": [{"NotificationRequestItem": {
            "pspReference": "psp_1", "originalReference": "",
            "merchantAccountCode": "M", "merchantReference": "ref_1",
            "amount": {"value": 100, "currency": "SAR"},
            "eventCode": "AUTHORISATION", "success": "true",
            "additionalData": {"hmacSignature": signature}}}]}).encode()
        assert adapter.parse_webhook({}, body)["signature_verified"] is True


class TestCheckout:
    def adapter(self):
        return build_adapter("checkout", {"secret_key": "sk_sbox", "public_key": "pk_sbox",
                                          "api_base": "https://checkout.test"})

    async def test_hosted_payment_link(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/hosted-payments": httpx.Response(201, json={
                "id": "hp_1", "_links": {"redirect": {"href": "https://checkout.test/pay"}}}),
        }))
        session = await adapter.create_hpp_session(client, request_for())
        assert session.redirect_url == "https://checkout.test/pay"
        await client.client.aclose()

    async def test_direct_frictionless_is_one_timed_call(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/tokens": httpx.Response(201, json={"token": "tok_1"}),
            "/payments": httpx.Response(201, json={"id": "pay_1", "status": "Authorized",
                                                   "response_code": "10000"}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert len(client.timed_measurements) == 1     # tokenization is client-side
        await client.client.aclose()

    async def test_pending_triggers_the_documented_status_read(self):
        adapter = self.adapter()
        calls = {"payments": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/tokens" in url:
                return httpx.Response(201, json={"token": "tok_1"})
            calls["payments"] += 1
            if calls["payments"] == 1:
                return httpx.Response(202, json={"id": "pay_1", "status": "Pending"})
            return httpx.Response(200, json={"id": "pay_1", "status": "Authorized"})

        client = client_for(adapter, handler)
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert result.three_ds_required is True
        assert len(client.timed_measurements) == 2
        await client.client.aclose()


class TestHyperPay:
    def adapter(self):
        return build_adapter("hyperpay", {"entity_id": "ent", "access_token": "tok",
                                          "api_base": "https://oppwa.test"})

    async def test_prepare_checkout_returns_a_widget(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/v1/checkouts": httpx.Response(200, json={"id": "chk_1",
                                                       "result": {"code": "000.200.100",
                                                                  "description": "created"}}),
        }))
        session = await adapter.create_hpp_session(client, request_for())
        assert session.mode == "widget"
        assert session.gateway_reference == "chk_1"
        await client.client.aclose()

    async def test_result_code_decides_the_outcome_not_the_http_status(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/v1/payments": httpx.Response(200, json={
                "id": "pay_1", "result": {"code": "800.100.153",
                                          "description": "invalid CVV"}}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.DECLINED
        assert result.gateway_code == "800.100.153"
        await client.client.aclose()

    async def test_success_codes_are_recognised(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/v1/payments": httpx.Response(200, json={
                "id": "pay_1", "result": {"code": "000.100.110", "description": "ok"}}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        await client.client.aclose()

    def test_backoffice_path_is_switchable(self):
        flat = build_adapter("hyperpay", {"entity_id": "e", "access_token": "t"})
        nested = build_adapter("hyperpay", {"entity_id": "e", "access_token": "t",
                                            "backoffice_path": "nested"})
        assert (flat.get("backoffice_path") or "flat") == "flat"
        assert nested.get("backoffice_path") == "nested"


class TestMoyasar:
    def adapter(self, **extra):
        values = {"secret_key": "sk_test", "publishable_key": "pk_test",
                  "api_base": "https://moyasar.test"}
        values.update(extra)
        return build_adapter("moyasar", values)

    async def test_invoice_hosted_flow(self):
        adapter = self.adapter()
        client = client_for(adapter, route({
            "/v1/invoices": httpx.Response(201, json={"id": "inv_1",
                                                      "url": "https://moyasar.test/i/1",
                                                      "status": "initiated"}),
        }))
        session = await adapter.create_hpp_session(client, request_for())
        assert session.redirect_url == "https://moyasar.test/i/1"
        await client.client.aclose()

    async def test_token_mode_is_three_calls(self):
        adapter = self.adapter(direct_mode="token")
        client = client_for(adapter, route({
            "/v1/tokens": httpx.Response(201, json={"id": "tok_1"}),
            "GET /v1/payments/": httpx.Response(200, json={"id": "pay_1", "status": "paid"}),
            "POST /v1/payments": httpx.Response(201, json={"id": "pay_1", "status": "paid"}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert len(client.timed_measurements) == 3
        await client.client.aclose()

    async def test_card_mode_is_two_calls(self):
        """Moyasar's own documentation contradicts itself here; the mode is recorded."""
        adapter = self.adapter(direct_mode="card")
        client = client_for(adapter, route({
            "GET /v1/payments/": httpx.Response(200, json={"id": "pay_1", "status": "paid"}),
            "POST /v1/payments": httpx.Response(201, json={"id": "pay_1", "status": "paid"}),
        }))
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert len(client.timed_measurements) == 2
        await client.client.aclose()

    async def test_amount_is_sent_in_minor_units(self):
        adapter = self.adapter(direct_mode="card")
        requests: List[httpx.Request] = []
        client = client_for(adapter, route({
            "GET /v1/payments/": httpx.Response(200, json={"id": "p", "status": "paid"}),
            "POST /v1/payments": httpx.Response(201, json={"id": "p", "status": "paid"}),
        }), requests)
        await adapter.process_direct_payment(client, request_for(amount=1.00))
        assert "amount=100" in requests[0].content.decode()
        await client.client.aclose()


class TestMockPay:
    async def test_success_scenario_completes(self):
        adapter = build_adapter("mockpay", {"scenario": "success", "latency_ms": "5",
                                            "jitter_ms": "0"})
        client = adapter.build_client()
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.SUCCESS
        assert len(client.timed_measurements) == 2
        await client.client.aclose()

    async def test_decline_scenario(self):
        adapter = build_adapter("mockpay", {"scenario": "decline", "latency_ms": "5"})
        client = adapter.build_client()
        result = await adapter.process_direct_payment(client, request_for())
        assert result.status is TransactionStatus.DECLINED
        await client.client.aclose()

    async def test_timeout_scenario_raises_and_is_recorded(self):
        adapter = build_adapter("mockpay", {"scenario": "timeout", "latency_ms": "5"})
        client = adapter.build_client()
        with pytest.raises(httpx.TimeoutException):
            await adapter.process_direct_payment(client, request_for())
        assert client.measurements[0].timed_out is True
        await client.client.aclose()

    async def test_server_error_scenario(self):
        from app.core.errors import GatewayError
        adapter = build_adapter("mockpay", {"scenario": "server_error", "latency_ms": "5"})
        client = adapter.build_client()
        with pytest.raises(GatewayError):
            await adapter.process_direct_payment(client, request_for())
        assert client.measurements[0].http_status == 500
        await client.client.aclose()

    async def test_three_ds_scenario_adds_an_authentication_call(self):
        adapter = build_adapter("mockpay", {"scenario": "three_ds", "latency_ms": "5"})
        client = adapter.build_client()
        result = await adapter.process_direct_payment(client, request_for())
        assert result.three_ds_required is True
        assert len(client.timed_measurements) == 3
        await client.client.aclose()

    async def test_configured_latency_is_honoured(self):
        adapter = build_adapter("mockpay", {"scenario": "success", "latency_ms": "60",
                                            "jitter_ms": "0"})
        client = adapter.build_client()
        await adapter.process_direct_payment(client, request_for())
        assert client.measurements[0].duration_ms >= 50
        await client.client.aclose()
