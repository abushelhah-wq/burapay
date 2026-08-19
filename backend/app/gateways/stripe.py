"""
Stripe adapter.

Auth: HTTP Basic, secret key as username, empty password.
Bodies: form-encoded — Stripe does not accept JSON request bodies.

Card handling: this adapter charges Stripe *test tokens* rather than raw PANs.
Stripe blocks raw card numbers on ``/v1/payment_methods`` for accounts without PCI
enablement, so a raw-PAN path would fail on most sandboxes and would put this
application in PCI scope for no benefit. Tokenization is a browser-side step in any
real integration, so it is issued as an untimed setup call and stays out of the
reported API time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Mapping, Optional

import httpx

from app.core.errors import ErrorCategory, GatewayError
from app.gateways.base import (CredentialField, HealthProbe, HppSession,
                               PaymentGatewayAdapter, PaymentRequest, PaymentResult)
from app.gateways.http import InstrumentedClient
from app.models.enums import NormalizedOperation, TransactionStatus


class StripeAdapter(PaymentGatewayAdapter):
    code = "stripe"
    display_name = "Stripe"
    supports_hpp = True
    supports_direct = True
    supported_currencies = ("USD", "EUR", "GBP", "AED", "SAR")
    docs_url = "https://docs.stripe.com/api"

    credential_fields = (
        CredentialField("secret_key", "Secret Key", secret=True, placeholder="sk_test_…",
                        help_text="Server-side key. Never sent to the browser."),
        CredentialField("publishable_key", "Publishable Key", required=False,
                        placeholder="pk_test_…",
                        help_text="Safe to expose; used by Stripe's browser components."),
        CredentialField("webhook_secret", "Webhook Secret", required=False, secret=True,
                        placeholder="whsec_…",
                        help_text="Enables signature verification on /api/webhooks/stripe."),
        CredentialField("api_base", "API Base URL", required=False,
                        default="https://api.stripe.com"),
        CredentialField("api_version", "Stripe-Version header", required=False,
                        help_text="Pin an API version for reproducible measurements across dates."),
        CredentialField("test_token", "Test card token", required=False, default="tok_visa",
                        help_text="Stripe test token charged by the Direct flow, e.g. tok_visa."),
    )

    notes = (
        "Stripe's own quickstart steers integrators toward Checkout Sessions rather "
        "than raw PaymentIntents, so the low Direct call count is a real capability "
        "but not the path Stripe recommends.",
        "Fulfilment is webhook-mandatory per Stripe's documentation. The status GET "
        "measured on confirmation is the defensive re-check Stripe's own sample code "
        "performs.",
        "The Direct flow charges a test token rather than a raw PAN: Stripe blocks raw "
        "card numbers for accounts without PCI enablement.",
    )

    documented_calls = {
        "hpp": "1 outbound + 1 webhook (+1 optional status GET)",
        "direct": "1-2 (create+confirm PaymentIntent; +1 to mint a PaymentMethod)",
    }

    @property
    def base(self) -> str:
        return (self.get("api_base") or "https://api.stripe.com").rstrip("/")

    def auth(self) -> Optional[httpx.Auth]:
        return httpx.BasicAuth(self.get("secret_key") or "", "")

    def default_headers(self) -> Dict[str, str]:
        headers = super().default_headers()
        version = self.get("api_version")
        if version:
            headers["Stripe-Version"] = version
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    @staticmethod
    def _currency(request: PaymentRequest) -> str:
        return (request.currency or "usd").lower()

    def _annotate(self, client: InstrumentedClient, body: Dict[str, Any]) -> None:
        """Stripe puts declines in ``error.code`` / ``last_payment_error``."""
        error = body.get("error") or body.get("last_payment_error") or {}
        if error:
            client.annotate_last(code=error.get("decline_code") or error.get("code"),
                                 message=error.get("message"), success=False,
                                 category=ErrorCategory.GATEWAY_DECLINE)
        else:
            client.annotate_last(code=body.get("status"), message=body.get("status"),
                                 success=True)

    @staticmethod
    def _status(intent: Dict[str, Any]) -> TransactionStatus:
        return {
            "succeeded": TransactionStatus.SUCCESS,
            "processing": TransactionStatus.PENDING,
            "requires_capture": TransactionStatus.SUCCESS,
            "requires_action": TransactionStatus.PENDING,
            "requires_payment_method": TransactionStatus.DECLINED,
            "canceled": TransactionStatus.CANCELLED,
        }.get(str(intent.get("status")), TransactionStatus.FAILED)

    # -- HPP -------------------------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        separator = "&" if "?" in request.return_url else "?"
        success_url = f"{request.return_url}{separator}session_id={{CHECKOUT_SESSION_ID}}"
        response = await client.call(
            "POST /v1/checkout/sessions", "POST", self._url("/v1/checkout/sessions"),
            normalized=NormalizedOperation.SESSION_CREATION,
            data={
                "mode": "payment",
                "success_url": success_url,
                "cancel_url": f"{request.return_url}{separator}result=cancelled",
                "client_reference_id": request.reference,
                "line_items[0][price_data][currency]": self._currency(request),
                "line_items[0][price_data][product_data][name]": request.description[:120],
                "line_items[0][price_data][unit_amount]": str(self.minor_units(request.amount)),
                "line_items[0][quantity]": "1",
            })
        body = await self.expect_ok(client, response, "create Checkout Session")
        self._annotate(client, body)
        session_id, url = body.get("id"), body.get("url")
        if not session_id or not url:
            raise GatewayError("Stripe: Checkout Session response carried no id/url", raw=body)
        return HppSession(redirect_url=url, gateway_reference=session_id,
                          context={"session_id": session_id})

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        session_id = params.get("session_id") or context.get("session_id")
        if not session_id:
            raise GatewayError("Stripe: no session_id to confirm against")
        if str(params.get("result", "")).lower() == "cancelled":
            return PaymentResult(TransactionStatus.CANCELLED, str(session_id),
                                 "cancelled", "Customer cancelled on Stripe Checkout")
        response = await client.call(
            "GET /v1/checkout/sessions/{id}", "GET",
            self._url(f"/v1/checkout/sessions/{session_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = await self.expect_ok(client, response, "retrieve Checkout Session",
                                    accept=(200,))
        self._annotate(client, body)
        payment_status = body.get("payment_status")
        status = {"paid": TransactionStatus.SUCCESS,
                  "no_payment_required": TransactionStatus.SUCCESS}.get(
            str(payment_status),
            TransactionStatus.PENDING if body.get("status") == "open"
            else TransactionStatus.FAILED)
        return PaymentResult(status, str(session_id), str(payment_status),
                             f"payment_status={payment_status}, status={body.get('status')}",
                             raw=body)

    # -- Direct ----------------------------------------------------------- #

    async def _payment_method(self, client: InstrumentedClient, *, setup: bool) -> str:
        issue = client.setup_call if setup else client.call
        response = await issue(
            "POST /v1/payment_methods", "POST", self._url("/v1/payment_methods"),
            normalized=NormalizedOperation.TOKENIZATION,
            data={"type": "card", "card[token]": self.get("test_token") or "tok_visa"})
        body = await self.expect_ok(client, response, "create PaymentMethod")
        return body["id"]

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        # Tokenization is a browser step in a real integration, so it is setup, not
        # part of the merchant-server latency being compared.
        payment_method = await self._payment_method(client, setup=True)
        response = await client.call(
            "POST /v1/payment_intents", "POST", self._url("/v1/payment_intents"),
            normalized=NormalizedOperation.PAYMENT_INITIATION,
            data={
                "amount": str(self.minor_units(request.amount)),
                "currency": self._currency(request),
                "payment_method": payment_method,
                "confirm": "true",
                # Card-only, no redirect methods: keeps the sandbox path frictionless
                # and comparable with the other gateways' non-3DS flows.
                "automatic_payment_methods[enabled]": "true",
                "automatic_payment_methods[allow_redirects]": "never",
                "description": request.description[:200],
                "metadata[reference]": request.reference,
            })
        intent = await self.expect_ok(client, response, "create+confirm PaymentIntent")
        self._annotate(client, intent)
        status = self._status(intent)
        error = intent.get("last_payment_error") or {}
        return PaymentResult(status, intent.get("id"),
                             error.get("decline_code") or error.get("code") or intent.get("status"),
                             error.get("message") or f"status={intent.get('status')}",
                             three_ds_required=intent.get("status") == "requires_action",
                             raw=intent)

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        response = await client.call(
            "GET /v1/payment_intents/{id}", "GET",
            self._url(f"/v1/payment_intents/{payment_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        intent = await self.expect_ok(client, response, "retrieve PaymentIntent",
                                      accept=(200,))
        self._annotate(client, intent)
        return PaymentResult(self._status(intent), intent.get("id"),
                             str(intent.get("status")), f"status={intent.get('status')}",
                             raw=intent)

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        data = {"payment_intent": payment_id}
        if amount is not None:
            data["amount"] = str(self.minor_units(amount))
        response = await client.call("POST /v1/refunds", "POST", self._url("/v1/refunds"),
                                     normalized=NormalizedOperation.REFUND, data=data)
        body = await self.expect_ok(client, response, "create Refund")
        self._annotate(client, body)
        status = (TransactionStatus.SUCCESS if body.get("status") in ("succeeded", "pending")
                  else TransactionStatus.FAILED)
        return PaymentResult(status, body.get("id"), str(body.get("status")),
                             f"status={body.get('status')}", raw=body)

    # -- webhook ---------------------------------------------------------- #

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        """Verify the ``Stripe-Signature`` header per Stripe's documented scheme."""
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        verified = self._verify_signature(headers.get("stripe-signature", ""), body)
        obj = ((payload.get("data") or {}).get("object")) or {}
        return {
            "event_type": payload.get("type"),
            "reference": obj.get("client_reference_id") or (obj.get("metadata") or {}).get("reference"),
            "gateway_reference": obj.get("id"),
            "status": obj.get("status") or obj.get("payment_status"),
            "signature_verified": verified,
            "payload": payload,
        }

    def _verify_signature(self, header: str, body: bytes) -> Optional[bool]:
        secret = self.get("webhook_secret")
        if not secret or not header:
            return None          # unknown, never optimistically "verified"
        parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
        timestamp, signature = parts.get("t"), parts.get("v1")
        if not timestamp or not signature:
            return False
        expected = hmac.new(secret.encode("utf-8"),
                            f"{timestamp}.".encode("utf-8") + (body or b""),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """A read-only list call with ``limit=1``. Creates nothing, charges nothing."""
        started = time.perf_counter()
        try:
            response = await client.call(
                "GET /v1/balance", "GET", self._url("/v1/balance"),
                normalized=NormalizedOperation.HEALTH_CHECK)
        except httpx.HTTPError as exc:
            return HealthProbe(False, round((time.perf_counter() - started) * 1000, 3),
                               None, f"{type(exc).__name__}: {exc}")
        return HealthProbe(response.status_code < 500,
                           round((time.perf_counter() - started) * 1000, 3),
                           response.status_code, "balance read; no payment created")
