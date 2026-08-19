"""
HyperPay adapter (OPPWA engine).

Auth: Bearer access token plus an ``entityId`` on every request. Bodies:
form-encoded. Test host ``https://eu-test.oppwa.com`` — HyperPay issues a branded
equivalent at onboarding, so the base URL is configurable.

Two things to know before reading the numbers
---------------------------------------------
* OPPWA carries the real outcome in ``result.code``, not the HTTP status. A 200 with
  ``800.100.153`` is a decline and is recorded as one.
* The back-office URL shape for capture, refund and reversal could not be confirmed
  on a HyperPay-branded page during research. The default posts to
  ``/v1/payments/{id}``; Peach Payments, running the same engine, documents
  ``/v1/payments/{id}/payments``. Configurable rather than guessed at silently.

The Copy&Pay status read is throttled to 2 calls/minute per checkout, and the
checkout id becomes single-use once it returns a final result. Benchmark runs against
HyperPay HPP must respect that; the platform's per-run interval limit is what keeps
it inside the throttle.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Mapping, Optional

import httpx

from app.core.errors import ErrorCategory, GatewayError
from app.gateways.base import (Card, CredentialField, HealthProbe, HppSession,
                               PaymentGatewayAdapter, PaymentRequest, PaymentResult)
from app.gateways.http import InstrumentedClient
from app.models.enums import NormalizedOperation, TransactionStatus


class HyperPayAdapter(PaymentGatewayAdapter):
    code = "hyperpay"
    display_name = "HyperPay"
    supports_hpp = True
    supports_direct = True
    supported_currencies = ("SAR", "AED", "USD", "EUR")
    docs_url = "https://wordpresshyperpay.docs.oppwa.com/"

    credential_fields = (
        CredentialField("entity_id", "Entity ID", secret=True,
                        help_text="Channel identifier sent on every request. Treated as a secret because it identifies your account."),
        CredentialField("access_token", "Access Token", secret=True,
                        help_text="Bearer token issued by HyperPay."),
        CredentialField("api_base", "API Base URL", required=False,
                        default="https://eu-test.oppwa.com",
                        help_text="HyperPay issues a branded host at onboarding; override the OPPWA default here."),
        CredentialField("backoffice_path", "Back-office URL shape", required=False,
                        default="flat", choices=("flat", "nested"),
                        help_text="flat = /v1/payments/{id}; nested = /v1/payments/{id}/payments. Switch if refunds are rejected."),
        CredentialField("card_brand", "Test card brand", required=False, default="VISA"),
    )

    notes = (
        "OPPWA reports the outcome in result.code, not the HTTP status: 000.000.* and "
        "000.100.1* are successes, 000.200.* means awaiting the shopper, everything "
        "else is a failure.",
        "The back-office URL shape for capture/refund/reversal is inferred from Peach "
        "Payments on the same OPPWA engine; HyperPay's own tutorial pages were "
        "unreachable during research. Switch it on this page if a refund is rejected.",
        "The Copy&Pay status read is throttled to 2 calls per minute per checkout and "
        "the checkout id is single-use once final — keep HPP benchmark intervals above "
        "the default when running against HyperPay.",
    )

    documented_calls = {
        "hpp": "2 (prepare checkout + mandatory status read)",
        "direct": "1 frictionless, 2 with a challenge (unverified)",
    }

    @property
    def base(self) -> str:
        return (self.get("api_base") or "https://eu-test.oppwa.com").rstrip("/")

    def default_headers(self) -> Dict[str, str]:
        headers = super().default_headers()
        headers["Authorization"] = f"Bearer {self.get('access_token') or ''}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _base_data(self, request: PaymentRequest, **extra: Any) -> Dict[str, str]:
        data = {"entityId": self.get("entity_id") or "",
                "amount": self.decimal_amount(request.amount),
                "currency": request.currency}
        data.update({key: str(value) for key, value in extra.items()})
        return data

    def _card_data(self, card: Card) -> Dict[str, str]:
        return {"paymentBrand": self.get("card_brand") or "VISA",
                "card.number": card.number, "card.holder": card.holder,
                "card.expiryMonth": card.month, "card.expiryYear": card.year,
                "card.cvv": card.cvc}

    @staticmethod
    def _code(body: Mapping[str, Any]) -> str:
        return str((body.get("result") or {}).get("code") or "")

    def _status_for(self, code: str) -> TransactionStatus:
        if code.startswith("000.000.") or code.startswith("000.100.1"):
            return TransactionStatus.SUCCESS
        if code.startswith("000.200."):
            return TransactionStatus.PENDING
        if code.startswith(("800.", "100.", "900.")):
            return TransactionStatus.DECLINED
        return TransactionStatus.FAILED

    def _outcome(self, client: InstrumentedClient, body: Dict[str, Any]) -> PaymentResult:
        code = self._code(body)
        description = (body.get("result") or {}).get("description")
        status = self._status_for(code)
        ok = status in (TransactionStatus.SUCCESS, TransactionStatus.PENDING)
        client.annotate_last(code=code, message=description, success=ok,
                             category=None if ok else ErrorCategory.GATEWAY_DECLINE)
        return PaymentResult(status, body.get("id"), code,
                             f"result.code={code!r} ({description!r})", raw=body)

    def _require_ok(self, result: PaymentResult, what: str) -> PaymentResult:
        if result.status in (TransactionStatus.FAILED, TransactionStatus.DECLINED,
                             TransactionStatus.ERROR):
            raise GatewayError(f"HyperPay: {what} {result.gateway_message}",
                               gateway_code=result.gateway_code, raw=result.raw)
        return result

    @staticmethod
    def _id(body: Dict[str, Any]) -> str:
        value = body.get("id")
        if not value:
            raise GatewayError("HyperPay: response carried no id", raw=body)
        return value

    # -- HPP -------------------------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        response = await client.call(
            "POST /v1/checkouts", "POST", self._url("/v1/checkouts"),
            normalized=NormalizedOperation.SESSION_CREATION,
            data=self._base_data(request, paymentType="DB",
                                 merchantTransactionId=request.reference,
                                 shopperResultUrl=request.return_url))
        body = await self.expect_ok(client, response, "prepare checkout", accept=(200, 201))
        self._require_ok(self._outcome(client, body), "prepare checkout")
        checkout_id = self._id(body)
        # Copy&Pay is a widget mounted in the merchant's own page rather than a hosted
        # URL to redirect to, so the browser gets a widget host page.
        return HppSession(redirect_url=f"/hpp/widget/hyperpay/{checkout_id}",
                          gateway_reference=checkout_id, mode="widget",
                          context={"checkout_id": checkout_id})

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        checkout_id = context.get("checkout_id")
        resource_path = params.get("resourcePath") or (
            f"/v1/checkouts/{checkout_id}/payment" if checkout_id else None)
        if not resource_path:
            raise GatewayError("HyperPay: no resourcePath or checkout id to confirm against")
        response = await client.call(
            "GET {resourcePath}", "GET", self._url(resource_path),
            normalized=NormalizedOperation.STATUS_CHECK,
            params={"entityId": self.get("entity_id") or ""})
        body = await self.expect_ok(client, response, "checkout status", accept=(200,))
        return self._outcome(client, body)

    # -- Direct ----------------------------------------------------------- #

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        if request.card is None:
            raise GatewayError("HyperPay Direct API needs card details.")
        data = self._base_data(request, paymentType="DB",
                               merchantTransactionId=request.reference)
        data.update(self._card_data(request.card))
        response = await client.call(
            "POST /v1/payments (DB)", "POST", self._url("/v1/payments"),
            normalized=NormalizedOperation.AUTHORIZATION, data=data)
        body = await self.expect_ok(client, response, "create payment", accept=(200, 201))
        result = self._outcome(client, body)

        three_ds = bool(body.get("redirect")) or self._code(body).startswith("000.200.")
        if three_ds:
            resource_path = body.get("resourcePath") or f"/v1/payments/{self._id(body)}"
            status_response = await client.call(
                "GET {resourcePath}", "GET", self._url(resource_path),
                normalized=NormalizedOperation.STATUS_CHECK,
                params={"entityId": self.get("entity_id") or ""})
            body = await self.expect_ok(client, status_response, "payment status",
                                        accept=(200,))
            result = self._outcome(client, body)
        result.three_ds_required = three_ds
        return result

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        response = await client.call(
            "GET /v1/payments/{id}", "GET", self._url(f"/v1/payments/{payment_id}"),
            normalized=NormalizedOperation.STATUS_CHECK,
            params={"entityId": self.get("entity_id") or ""})
        body = await self.expect_ok(client, response, "payment status", accept=(200,))
        return self._outcome(client, body)

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        url = (self._url(f"/v1/payments/{payment_id}/payments")
               if (self.get("backoffice_path") or "flat").lower() == "nested"
               else self._url(f"/v1/payments/{payment_id}"))
        data: Dict[str, str] = {"entityId": self.get("entity_id") or "", "paymentType": "RF"}
        if amount is not None and currency:
            data.update({"amount": self.decimal_amount(amount), "currency": currency})
        response = await client.call("POST refund (paymentType=RF)", "POST", url,
                                     normalized=NormalizedOperation.REFUND, data=data)
        body = await self.expect_ok(client, response, "refund", accept=(200, 201))
        return self._outcome(client, body)

    # -- webhook ---------------------------------------------------------- #

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        """HyperPay/OPPWA webhooks are AES-encrypted with a key from the dashboard.

        Decryption is not implemented, so the payload is recorded as opaque and the
        arrival time — the thing that matters for benchmarking asynchronous completion
        — is captured regardless.
        """
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {"encrypted": True, "bytes": len(body or b"")}
        return {
            "event_type": payload.get("type") or "oppwa.notification",
            "reference": payload.get("merchantTransactionId"),
            "gateway_reference": payload.get("id"),
            "status": (payload.get("result") or {}).get("code"),
            # OPPWA authenticates by encrypting the body, not by signing it; without
            # decryption this stays unknown rather than claiming verification.
            "signature_verified": None,
            "payload": payload,
        }

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """Status read for a payment id that cannot exist: read-only, creates nothing."""
        started = time.perf_counter()
        try:
            response = await client.call(
                "GET /v1/payments/{probe}", "GET",
                self._url("/v1/payments/burapay-health-probe"),
                normalized=NormalizedOperation.HEALTH_CHECK,
                params={"entityId": self.get("entity_id") or ""})
        except httpx.HTTPError as exc:
            return HealthProbe(False, round((time.perf_counter() - started) * 1000, 3),
                               None, f"{type(exc).__name__}: {exc}")
        return HealthProbe(response.status_code < 500,
                           round((time.perf_counter() - started) * 1000, 3),
                           response.status_code, "payment lookup probe; creates nothing")
