"""
Checkout.com adapter.

Auth: Bearer secret key. Bodies: JSON. Sandbox host ``https://api.sandbox.checkout.com``
— NAS accounts may carry a subdomain prefix, so the base URL is configurable.

Card tokenization runs client-side with the public key in any real integration, so it
is issued here as an untimed setup call and stays out of the merchant-server count.

Capture, refund and void all return ``202 Accepted``: what is measured is
time-to-acknowledgement, not time-to-settled. The authoritative outcome arrives on a
webhook, and the platform labels those measurements accordingly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Mapping, Optional

import httpx

from app.core.errors import ErrorCategory, GatewayError, NotConfigured
from app.gateways.base import (Card, CredentialField, HealthProbe, HppSession,
                               PaymentGatewayAdapter, PaymentRequest, PaymentResult)
from app.gateways.http import InstrumentedClient
from app.models.enums import NormalizedOperation, TransactionStatus

STATUS_MAP = {
    "Authorized": TransactionStatus.SUCCESS,
    "Captured": TransactionStatus.SUCCESS,
    "Card Verified": TransactionStatus.SUCCESS,
    "Paid": TransactionStatus.SUCCESS,
    "Pending": TransactionStatus.PENDING,
    "Declined": TransactionStatus.DECLINED,
    "Canceled": TransactionStatus.CANCELLED,
    "Cancelled": TransactionStatus.CANCELLED,
    "Expired": TransactionStatus.CANCELLED,
}


class CheckoutAdapter(PaymentGatewayAdapter):
    code = "checkout"
    display_name = "Checkout.com"
    supports_hpp = True
    supports_direct = True
    supported_currencies = ("USD", "EUR", "GBP", "AED", "SAR")
    docs_url = "https://www.checkout.com/docs/"

    credential_fields = (
        CredentialField("secret_key", "Secret Key", secret=True, placeholder="sk_sbox_…"),
        CredentialField("public_key", "Public Key", required=False, placeholder="pk_sbox_…",
                        help_text="Client-side key used to mint card tokens."),
        CredentialField("processing_channel_id", "Processing Channel ID", required=False,
                        placeholder="pc_…",
                        help_text="Required on NAS accounts; omitted when blank."),
        CredentialField("webhook_secret", "Webhook Secret", required=False, secret=True,
                        help_text="Enables Cko-Signature verification on the webhook endpoint."),
        CredentialField("api_base", "API Base URL", required=False,
                        default="https://api.sandbox.checkout.com"),
        CredentialField("country", "Billing country", required=False, default="GB"),
    )

    notes = (
        "Capture, refund and void return 202 Accepted. Those timings are "
        "time-to-acknowledgement; completion arrives on the payment_captured / "
        "payment_refunded / payment_voided webhook.",
        "Card tokenization is client-side and recorded as a setup call, keeping the "
        "merchant-server call count comparable with the other gateways.",
    )

    documented_calls = {
        "hpp": "2 (create hosted payment + confirm)",
        "direct": "1 frictionless (201), 2 with a challenge (202 + status GET)",
    }

    @property
    def base(self) -> str:
        return (self.get("api_base") or "https://api.sandbox.checkout.com").rstrip("/")

    def default_headers(self) -> Dict[str, str]:
        headers = super().default_headers()
        headers.update({"Authorization": f"Bearer {self.get('secret_key') or ''}",
                        "Content-Type": "application/json"})
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _annotate(self, client: InstrumentedClient, body: Dict[str, Any]) -> None:
        status = str(body.get("status") or "")
        summary = body.get("response_summary") or body.get("error_type")
        ok = STATUS_MAP.get(status) in (TransactionStatus.SUCCESS, TransactionStatus.PENDING)
        client.annotate_last(code=body.get("response_code") or status or None,
                             message=summary or status or None, success=bool(ok),
                             category=None if ok else ErrorCategory.GATEWAY_DECLINE)

    def _outcome(self, body: Dict[str, Any]) -> PaymentResult:
        status = str(body.get("status") or "")
        mapped = STATUS_MAP.get(status)
        if mapped is None:
            # An action_id with no status is an accepted async operation, not a failure.
            mapped = TransactionStatus.SUCCESS if body.get("action_id") else TransactionStatus.FAILED
        return PaymentResult(mapped, body.get("id") or body.get("action_id"),
                             str(body.get("response_code") or status),
                             f"status={status!r} summary={body.get('response_summary')!r}",
                             raw=body)

    @staticmethod
    def _id(body: Dict[str, Any]) -> str:
        value = body.get("id")
        if not value:
            raise GatewayError("Checkout.com: response carried no id", raw=body)
        return value

    def _payment_body(self, request: PaymentRequest, source: Dict[str, Any],
                      **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "source": source,
            "amount": self.minor_units(request.amount),
            "currency": request.currency,
            "reference": request.reference,
            "description": request.description[:100],
            "success_url": request.return_url,
            "failure_url": request.return_url,
        }
        channel = self.get("processing_channel_id")
        if channel:
            body["processing_channel_id"] = channel
        body.update(extra)
        return body

    # -- HPP -------------------------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        body: Dict[str, Any] = {
            "amount": self.minor_units(request.amount),
            "currency": request.currency,
            "reference": request.reference,
            "description": request.description[:100],
            "billing": {"address": {"country": self.get("country") or "GB"}},
            "success_url": request.return_url,
            "failure_url": request.return_url,
            "cancel_url": f"{request.return_url}{'&' if '?' in request.return_url else '?'}result=cancelled",
        }
        channel = self.get("processing_channel_id")
        if channel:
            body["processing_channel_id"] = channel
        response = await client.call(
            "POST /hosted-payments", "POST", self._url("/hosted-payments"),
            normalized=NormalizedOperation.SESSION_CREATION, json=body)
        created = await self.expect_ok(client, response, "create hosted payment",
                                       accept=(201, 202))
        client.annotate_last(code="created", message="hosted payment created", success=True)
        link = ((created.get("_links") or {}).get("redirect") or {}).get("href")
        if not link:
            raise GatewayError("Checkout.com: hosted payment carried no redirect link",
                               raw=created)
        return HppSession(link, self._id(created), context={"hosted_id": self._id(created)})

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        hosted_id = params.get("cko-payment-id") or context.get("hosted_id")
        if not hosted_id:
            raise GatewayError("Checkout.com: no hosted payment id to confirm against")
        if str(params.get("result", "")).lower() == "cancelled":
            return PaymentResult(TransactionStatus.CANCELLED, str(hosted_id), "cancelled",
                                 "Customer cancelled on the hosted page")
        response = await client.call(
            "GET /hosted-payments/{id}", "GET", self._url(f"/hosted-payments/{hosted_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = await self.expect_ok(client, response, "get hosted payment", accept=(200,))
        self._annotate(client, body)
        return self._outcome(body)

    # -- Direct ----------------------------------------------------------- #

    async def _token(self, client: InstrumentedClient, card: Card) -> str:
        public_key = self.get("public_key")
        if not public_key:
            raise NotConfigured(
                "Checkout.com needs the Public Key to mint a card token for the Direct "
                "flow. Add it on the Settings page.")
        response = await client.setup_call(
            "POST /tokens (client-side)", "POST", self._url("/tokens"),
            normalized=NormalizedOperation.TOKENIZATION,
            json={"type": "card", "number": card.number, "expiry_month": int(card.month),
                  "expiry_year": int(card.year), "cvv": card.cvc},
            headers={"Authorization": public_key})
        body = await self.expect_ok(client, response, "create token")
        return body["token"]

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        if request.card is None:
            raise GatewayError("Checkout.com Direct API needs card details.")
        token = await self._token(client, request.card)
        response = await client.call(
            "POST /payments", "POST", self._url("/payments"),
            normalized=NormalizedOperation.AUTHORIZATION,
            json=self._payment_body(request, {"type": "token", "token": token},
                                    capture=True))
        body = await self.expect_ok(client, response, "create payment", accept=(201, 202))
        self._annotate(client, body)

        three_ds = bool(body.get("status") == "Pending"
                        or (body.get("_links") or {}).get("redirect"))
        if three_ds:
            status_response = await client.call(
                "GET /payments/{id}", "GET", self._url(f"/payments/{self._id(body)}"),
                normalized=NormalizedOperation.STATUS_CHECK)
            body = await self.expect_ok(client, status_response, "get payment", accept=(200,))
            self._annotate(client, body)

        result = self._outcome(body)
        result.three_ds_required = three_ds
        return result

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        response = await client.call(
            "GET /payments/{id}", "GET", self._url(f"/payments/{payment_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = await self.expect_ok(client, response, "get payment", accept=(200,))
        self._annotate(client, body)
        return self._outcome(body)

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        payload: Dict[str, Any] = {}
        if amount is not None:
            payload["amount"] = self.minor_units(amount)
        response = await client.call(
            "POST /payments/{id}/refunds", "POST",
            self._url(f"/payments/{payment_id}/refunds"),
            normalized=NormalizedOperation.REFUND, json=payload)
        body = await self.expect_ok(client, response, "refund", accept=(202,))
        client.annotate_last(code="202", message="accepted; completion arrives by webhook",
                             success=True)
        return PaymentResult(TransactionStatus.SUCCESS, body.get("action_id"), "202",
                             "202 Accepted — completion confirmed by webhook", raw=body)

    # -- webhook ---------------------------------------------------------- #

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        data = payload.get("data") or {}
        secret = self.get("webhook_secret")
        provided = headers.get("cko-signature")
        verified: Optional[bool] = None
        if secret and provided:
            expected = hmac.new(secret.encode("utf-8"), body or b"",
                                hashlib.sha256).hexdigest()
            verified = hmac.compare_digest(expected, provided)
        return {
            "event_type": payload.get("type"),
            "reference": data.get("reference"),
            "gateway_reference": data.get("id") or data.get("payment_id"),
            "status": data.get("status"),
            "signature_verified": verified,
            "payload": payload,
        }

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """A lookup for a payment id that cannot exist: read-only, creates nothing."""
        started = time.perf_counter()
        try:
            response = await client.call(
                "GET /payments/{probe}", "GET",
                self._url("/payments/pay_burapayhealthprobe000000"),
                normalized=NormalizedOperation.HEALTH_CHECK)
        except httpx.HTTPError as exc:
            return HealthProbe(False, round((time.perf_counter() - started) * 1000, 3),
                               None, f"{type(exc).__name__}: {exc}")
        return HealthProbe(response.status_code < 500,
                           round((time.perf_counter() - started) * 1000, 3),
                           response.status_code, "payment lookup probe; creates nothing")
