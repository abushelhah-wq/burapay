"""
Moyasar adapter.

Auth: HTTP Basic, secret key as username, empty password. Bodies: form-encoded.
Amounts are in halalas (minor units); sandboxes are SAR-first.

HPP uses Invoices: create, redirect to the hosted URL, fetch to confirm.

Direct has two documented minimums that contradict each other, so which one is
measured is a setting rather than an assumption: ``token`` follows the recommended
Custom UI guide (mint a token, then charge it), ``card`` follows what the API
reference schema allows (raw card fields straight to /v1/payments). Measuring one and
calling it "Moyasar's Direct latency" without saying which would be misleading.
"""

from __future__ import annotations

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
    "paid": TransactionStatus.SUCCESS,
    "captured": TransactionStatus.SUCCESS,
    "authorized": TransactionStatus.SUCCESS,
    "refunded": TransactionStatus.SUCCESS,
    "voided": TransactionStatus.SUCCESS,
    "initiated": TransactionStatus.PENDING,
    "pending": TransactionStatus.PENDING,
    "failed": TransactionStatus.DECLINED,
}


class MoyasarAdapter(PaymentGatewayAdapter):
    code = "moyasar"
    display_name = "Moyasar"
    supports_hpp = True
    supports_direct = True
    supported_currencies = ("SAR", "USD")
    docs_url = "https://docs.moyasar.com/"

    credential_fields = (
        CredentialField("secret_key", "Secret Key", secret=True, placeholder="sk_test_…"),
        CredentialField("publishable_key", "Publishable Key", required=False,
                        placeholder="pk_test_…",
                        help_text="Needed for the tokenize-first Direct mode."),
        CredentialField("api_base", "API Base URL", required=False,
                        default="https://api.moyasar.com"),
        CredentialField("direct_mode", "Direct API mode", required=False, default="token",
                        choices=("token", "card"),
                        help_text="token = mint a token then charge it (recommended guide); card = raw card fields (API reference)."),
    )

    notes = (
        "Moyasar's API reference and its recommended Custom UI guide disagree on "
        "whether raw card fields or tokenize-first is the production path. The mode is "
        "configurable so the measurement always says which one it measured.",
        "Void has roughly a two-hour post-capture window; after that only refund works.",
        "Amounts are transmitted in halalas. A 1.00 SAR benchmark is sent as 100.",
    )

    documented_calls = {
        "hpp": "2 (create invoice + fetch)",
        "direct": "2 raw-card / 3 tokenize-first",
    }

    @property
    def base(self) -> str:
        return (self.get("api_base") or "https://api.moyasar.com").rstrip("/")

    def auth(self) -> Optional[httpx.Auth]:
        return httpx.BasicAuth(self.get("secret_key") or "", "")

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _annotate(self, client: InstrumentedClient, body: Dict[str, Any]) -> None:
        status = str(body.get("status") or "").lower()
        source = body.get("source") or {}
        message = source.get("message") or body.get("message")
        ok = STATUS_MAP.get(status) in (TransactionStatus.SUCCESS, TransactionStatus.PENDING)
        client.annotate_last(code=str(source.get("gateway_id") or status or ""),
                             message=message or status, success=bool(ok),
                             category=None if ok else ErrorCategory.GATEWAY_DECLINE)

    def _outcome(self, body: Dict[str, Any]) -> PaymentResult:
        status = str(body.get("status") or "").lower()
        source = body.get("source") or {}
        message = source.get("message")
        return PaymentResult(STATUS_MAP.get(status, TransactionStatus.FAILED),
                             body.get("id"), status,
                             f"status={status!r}" + (f" message={message!r}" if message else ""),
                             raw=body)

    @staticmethod
    def _id(body: Dict[str, Any]) -> str:
        value = body.get("id")
        if not value:
            raise GatewayError("Moyasar: response carried no id", raw=body)
        return value

    # -- HPP -------------------------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        response = await client.call(
            "POST /v1/invoices", "POST", self._url("/v1/invoices"),
            normalized=NormalizedOperation.SESSION_CREATION,
            data={"amount": str(self.minor_units(request.amount)),
                  "currency": request.currency,
                  "description": request.description[:255],
                  "callback_url": request.webhook_url or request.return_url,
                  "success_url": request.return_url,
                  "back_url": request.return_url})
        body = await self.expect_ok(client, response, "create invoice", accept=(200, 201))
        self._annotate(client, body)
        url = body.get("url")
        if not url:
            raise GatewayError("Moyasar: invoice response carried no hosted url", raw=body)
        return HppSession(url, self._id(body), context={"invoice_id": self._id(body)})

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        invoice_id = params.get("invoice_id") or context.get("invoice_id")
        if not invoice_id:
            raise GatewayError("Moyasar: no invoice id to confirm against")
        response = await client.call(
            "GET /v1/invoices/{id}", "GET", self._url(f"/v1/invoices/{invoice_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = await self.expect_ok(client, response, "fetch invoice", accept=(200,))
        self._annotate(client, body)
        return self._outcome(body)

    # -- Direct ----------------------------------------------------------- #

    async def _token(self, client: InstrumentedClient, card: Card) -> str:
        publishable = self.get("publishable_key")
        if not publishable:
            raise NotConfigured(
                "Moyasar's tokenize-first Direct mode needs the Publishable Key. Add it "
                "on the Settings page, or switch Direct API mode to 'card'.")
        response = await client.call(
            "POST /v1/tokens", "POST", self._url("/v1/tokens"),
            normalized=NormalizedOperation.TOKENIZATION,
            data={"publishable_api_key": publishable, "name": card.holder,
                  "number": card.number, "month": card.month, "year": card.year,
                  "cvc": card.cvc, "save_only": "true"})
        body = await self.expect_ok(client, response, "create token")
        return body["id"]

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        if request.card is None:
            raise GatewayError("Moyasar Direct API needs card details.")
        card = request.card
        data: Dict[str, str] = {
            "amount": str(self.minor_units(request.amount)),
            "currency": request.currency,
            "description": request.description[:255],
            "callback_url": request.webhook_url or request.return_url,
        }
        if (self.get("direct_mode") or "token").lower() == "token":
            token = await self._token(client, card)
            data.update({"source[type]": "token", "source[token]": token})
        else:
            data.update({"source[type]": "creditcard", "source[name]": card.holder,
                         "source[number]": card.number, "source[month]": card.month,
                         "source[year]": card.year, "source[cvc]": card.cvc})

        response = await client.call(
            "POST /v1/payments", "POST", self._url("/v1/payments"),
            normalized=NormalizedOperation.AUTHORIZATION, data=data)
        body = await self.expect_ok(client, response, "create payment", accept=(200, 201))
        self._annotate(client, body)

        # Moyasar returns 3DS as a transaction_url on the payment rather than as a
        # separate call, so this fetch is required either way and is always measured.
        status_response = await client.call(
            "GET /v1/payments/{id}", "GET", self._url(f"/v1/payments/{self._id(body)}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        fetched = await self.expect_ok(client, status_response, "fetch payment", accept=(200,))
        self._annotate(client, fetched)
        result = self._outcome(fetched)
        result.three_ds_required = bool((fetched.get("source") or {}).get("transaction_url"))
        return result

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        response = await client.call(
            "GET /v1/payments/{id}", "GET", self._url(f"/v1/payments/{payment_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = await self.expect_ok(client, response, "fetch payment", accept=(200,))
        self._annotate(client, body)
        return self._outcome(body)

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        data = {"amount": str(self.minor_units(amount))} if amount is not None else {}
        response = await client.call(
            "POST /v1/payments/{id}/refund", "POST",
            self._url(f"/v1/payments/{payment_id}/refund"),
            normalized=NormalizedOperation.REFUND, data=data)
        body = await self.expect_ok(client, response, "refund", accept=(200, 201))
        self._annotate(client, body)
        return self._outcome(body)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        data = payload.get("data") or payload
        return {
            "event_type": payload.get("type") or "payment.updated",
            "reference": data.get("description"),
            "gateway_reference": data.get("id"),
            "status": data.get("status"),
            # Moyasar authenticates callbacks with a shared secret token in the body,
            # which this deployment does not configure; left unknown rather than assumed.
            "signature_verified": None,
            "payload": payload,
        }

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """A one-item payments listing: read-only, creates nothing."""
        started = time.perf_counter()
        try:
            response = await client.call(
                "GET /v1/payments?per=1", "GET", self._url("/v1/payments"),
                normalized=NormalizedOperation.HEALTH_CHECK, params={"per": "1"})
        except httpx.HTTPError as exc:
            return HealthProbe(False, round((time.perf_counter() - started) * 1000, 3),
                               None, f"{type(exc).__name__}: {exc}")
        return HealthProbe(response.status_code < 500,
                           round((time.perf_counter() - started) * 1000, 3),
                           response.status_code, "payments listing; creates nothing")
