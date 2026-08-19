"""
Adyen adapter.

Auth: ``x-API-key`` header. Bodies: JSON. Test host ``https://checkout-test.adyen.com``
with a versioned path (``/v71``, ``/v72``…).

HPP uses the Sessions flow, which is a single outbound call. Adyen documents no
pull-based status endpoint for it — confirmation is the AUTHORISATION webhook — so
``confirm_hpp_payment`` reports what the redirect carried and marks the transaction
pending rather than inventing a status GET that Adyen does not offer. That is a real,
measurable difference between gateways and the platform records it as one.

Direct uses the Advanced flow. Raw card fields are accepted in the test environment
for API-only integrations, which is what this sends; Adyen's recommended integration
encrypts card data client-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Mapping, Optional

import httpx

from app.core.errors import ErrorCategory, GatewayError
from app.gateways.base import (Card, CredentialField, HealthProbe, HppSession,
                               PaymentGatewayAdapter, PaymentRequest, PaymentResult)
from app.gateways.http import InstrumentedClient
from app.models.enums import NormalizedOperation, TransactionStatus

RESULT_MAP = {
    "Authorised": TransactionStatus.SUCCESS,
    "Received": TransactionStatus.SUCCESS,
    "received": TransactionStatus.SUCCESS,
    "Pending": TransactionStatus.PENDING,
    "RedirectShopper": TransactionStatus.PENDING,
    "IdentifyShopper": TransactionStatus.PENDING,
    "ChallengeShopper": TransactionStatus.PENDING,
    "Refused": TransactionStatus.DECLINED,
    "Error": TransactionStatus.ERROR,
    "Cancelled": TransactionStatus.CANCELLED,
}


class AdyenAdapter(PaymentGatewayAdapter):
    code = "adyen"
    display_name = "Adyen"
    supports_hpp = True
    supports_direct = True
    supported_currencies = ("EUR", "USD", "GBP", "SAR", "AED")
    docs_url = "https://docs.adyen.com/api-explorer/"

    credential_fields = (
        CredentialField("api_key", "API Key", secret=True,
                        help_text="Sent as the x-API-key header on every request."),
        CredentialField("merchant_account", "Merchant Account",
                        help_text="Adyen merchant account name, not the company account."),
        CredentialField("client_key", "Client Key", required=False,
                        help_text="Safe to expose; used by Adyen's Drop-in in the browser."),
        CredentialField("hmac_key", "HMAC Key", required=False, secret=True,
                        help_text="Hex HMAC key from the standard notification webhook, for signature verification."),
        CredentialField("api_base", "Checkout API URL", required=False,
                        default="https://checkout-test.adyen.com"),
        CredentialField("api_version", "API version", required=False, default="v71"),
        CredentialField("country_code", "Country code", required=False, default="NL",
                        help_text="Determines which payment methods Adyen offers on the session."),
    )

    notes = (
        "The Sessions flow has no documented pull-based status endpoint — the "
        "AUTHORISATION webhook is the only documented confirmation path. Time to "
        "authoritative status is therefore not something the merchant can measure, and "
        "the platform reports it as pending rather than guessing.",
        "Capture, refund and cancel return 'received'; the final outcome arrives by "
        "webhook, so those measurements are time-to-acknowledgement.",
        "Direct sends raw card fields, which Adyen accepts for API-only test "
        "integrations. Adyen's recommended production integration encrypts card data "
        "client-side.",
    )

    documented_calls = {
        "hpp": "1 outbound + 1 webhook",
        "direct": "2 frictionless, 3 with a challenge",
    }

    @property
    def base(self) -> str:
        return (self.get("api_base") or "https://checkout-test.adyen.com").rstrip("/")

    def default_headers(self) -> Dict[str, str]:
        headers = super().default_headers()
        headers.update({"x-API-key": self.get("api_key") or "",
                        "Content-Type": "application/json"})
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base}/{self.get('api_version') or 'v71'}{path}"

    def _amount(self, request: PaymentRequest) -> Dict[str, Any]:
        return {"value": self.minor_units(request.amount), "currency": request.currency}

    @staticmethod
    def _card(card: Card) -> Dict[str, str]:
        return {"type": "scheme", "number": card.number, "expiryMonth": card.month,
                "expiryYear": card.year, "cvc": card.cvc, "holderName": card.holder}

    def _annotate(self, client: InstrumentedClient, body: Dict[str, Any]) -> None:
        result = body.get("resultCode") or body.get("status")
        refusal = body.get("refusalReason")
        ok = RESULT_MAP.get(str(result)) in (TransactionStatus.SUCCESS,
                                             TransactionStatus.PENDING)
        client.annotate_last(code=str(result) if result else None,
                             message=refusal or (str(result) if result else None),
                             success=bool(ok),
                             category=None if ok else ErrorCategory.GATEWAY_DECLINE)

    def _outcome(self, body: Dict[str, Any]) -> PaymentResult:
        result = str(body.get("resultCode") or body.get("status") or "")
        status = RESULT_MAP.get(result, TransactionStatus.FAILED)
        message = f"resultCode={result!r}"
        if body.get("refusalReason"):
            message += f" refusal={body['refusalReason']!r}"
        return PaymentResult(status, body.get("pspReference"), result, message, raw=body)

    # -- HPP -------------------------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        response = await client.call(
            "POST /sessions", "POST", self._url("/sessions"),
            normalized=NormalizedOperation.SESSION_CREATION,
            json={"merchantAccount": self.get("merchant_account"),
                  "amount": self._amount(request),
                  "reference": request.reference,
                  "returnUrl": request.return_url,
                  "countryCode": self.get("country_code") or "NL"})
        body = await self.expect_ok(client, response, "create session", accept=(200, 201))
        client.annotate_last(code="created", message="session created", success=True)
        session_id = body.get("id")
        if not session_id:
            raise GatewayError("Adyen: session response carried no id", raw=body)
        # Adyen's Sessions flow is Drop-in driven: the browser mounts Adyen's SDK with
        # this session rather than being redirected to a hosted URL, so the platform
        # hands the browser a widget host page instead of a 303.
        return HppSession(redirect_url=f"/hpp/widget/adyen/{session_id}",
                          gateway_reference=session_id, mode="widget",
                          context={"session_id": session_id,
                                   "session_data": body.get("sessionData", ""),
                                   "client_key": self.get("client_key") or ""})

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        """No timed call here, on purpose.

        Adyen documents no status lookup for the Sessions flow. Inventing a GET would
        produce a latency figure for an operation that does not exist and would make
        Adyen look comparable on a dimension where it genuinely is not.
        """
        result = params.get("sessionResult") or params.get("redirectResult")
        session_id = str(context.get("session_id") or "")
        if not result:
            return PaymentResult(
                TransactionStatus.PENDING, session_id, None,
                "Adyen confirms the Sessions flow by AUTHORISATION webhook; no "
                "pull-based status endpoint is documented, so the outcome is not "
                "knowable from a merchant-initiated call.")
        return PaymentResult(
            TransactionStatus.PENDING, session_id, "sessionResult",
            "Redirect carried a sessionResult; the authoritative status still arrives "
            "on the AUTHORISATION webhook.")

    # -- Direct ----------------------------------------------------------- #

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        if request.card is None:
            raise GatewayError("Adyen Direct API needs card details.")
        response = await client.call(
            "POST /payments", "POST", self._url("/payments"),
            normalized=NormalizedOperation.AUTHORIZATION,
            json={"merchantAccount": self.get("merchant_account"),
                  "amount": self._amount(request),
                  "reference": request.reference,
                  "paymentMethod": self._card(request.card),
                  "returnUrl": request.return_url,
                  "channel": "Web"})
        body = await self.expect_ok(client, response, "create payment")
        self._annotate(client, body)

        action = body.get("action")
        if action:
            # A challenge means an additional documented round trip; measured as such.
            details_response = await client.call(
                "POST /payments/details", "POST", self._url("/payments/details"),
                normalized=NormalizedOperation.AUTHENTICATION,
                json={"details": {"redirectResult": action.get("paymentData", "")}})
            if details_response.status_code < 400:
                body = await self.json_body(client, details_response, "payment details")
                self._annotate(client, body)

        result = self._outcome(body)
        result.three_ds_required = bool(action)
        return result

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        raise GatewayError(
            "Adyen documents no pull-based payment status endpoint on the Checkout "
            "API; status arrives on the AUTHORISATION webhook.")

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        payload: Dict[str, Any] = {"merchantAccount": self.get("merchant_account")}
        if amount is not None and currency:
            payload["amount"] = {"value": self.minor_units(amount), "currency": currency}
        response = await client.call(
            "POST /payments/{pspReference}/refunds", "POST",
            self._url(f"/payments/{payment_id}/refunds"),
            normalized=NormalizedOperation.REFUND, json=payload)
        body = await self.expect_ok(client, response, "refund", accept=(200, 201))
        self._annotate(client, body)
        return self._outcome(body)

    # -- webhook ---------------------------------------------------------- #

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        items = payload.get("notificationItems") or []
        item = (items[0].get("NotificationRequestItem") if items else {}) or {}
        return {
            "event_type": item.get("eventCode"),
            "reference": item.get("merchantReference"),
            "gateway_reference": item.get("pspReference"),
            "status": "success" if str(item.get("success")).lower() == "true" else "failed",
            "signature_verified": self._verify_hmac(item),
            "payload": payload,
        }

    def _verify_hmac(self, item: Mapping[str, Any]) -> Optional[bool]:
        """Adyen's documented HMAC: colon-joined fields, escaped, over the hex key."""
        key_hex = self.get("hmac_key")
        provided = ((item.get("additionalData") or {}).get("hmacSignature"))
        if not key_hex or not provided:
            return None
        amount = item.get("amount") or {}
        fields = [item.get("pspReference", ""), item.get("originalReference", "") or "",
                  item.get("merchantAccountCode", ""), item.get("merchantReference", ""),
                  str(amount.get("value", "")), str(amount.get("currency", "")),
                  item.get("eventCode", ""), str(item.get("success", ""))]
        escaped = [str(f).replace("\\", "\\\\").replace(":", "\\:") for f in fields]
        payload = ":".join(escaped)
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            return False
        expected = base64.b64encode(
            hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
        return hmac.compare_digest(expected, str(provided))

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """``/paymentMethods`` is Adyen's own read-only reachability call.

        It lists what is available to the merchant account and creates nothing.
        """
        started = time.perf_counter()
        try:
            response = await client.call(
                "POST /paymentMethods", "POST", self._url("/paymentMethods"),
                normalized=NormalizedOperation.HEALTH_CHECK,
                json={"merchantAccount": self.get("merchant_account"),
                      "countryCode": self.get("country_code") or "NL", "channel": "Web"})
        except httpx.HTTPError as exc:
            return HealthProbe(False, round((time.perf_counter() - started) * 1000, 3),
                               None, f"{type(exc).__name__}: {exc}")
        return HealthProbe(response.status_code < 500,
                           round((time.perf_counter() - started) * 1000, 3),
                           response.status_code,
                           "payment methods listing; no payment created")
