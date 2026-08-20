"""
Geidea adapter — the first real gateway (specification section 53, phase 2).

Auth: HTTP Basic, Merchant Public Key as username, API Password as password.
Bodies: JSON.

Regional hosts share paths and differ only by host, selected by ``region``:

    eg  -> https://api.merchant.geidea.net      Egypt / global
    ksa -> https://api.ksamerchant.geidea.net   Saudi Arabia
    uae -> https://api.geidea.ae                UAE

A SAR sandbox is almost always KSA-issued. ``api_base`` overrides the lookup.

Documented call sequences
-------------------------
HPP — 2 calls::

    POST /payment-intent/api/v2/direct/session
    GET  /pgw/api/v1/direct/order/{orderId}

Direct API — 4 calls, documented as fixed regardless of the 3DS outcome::

    POST /payment-intent/api/v2/direct/session
    POST /pgw/api/v6/direct/authenticate/initiate
    POST /pgw/api/v6/direct/authenticate/payer
    POST /pgw/api/v2/direct/pay

Two details need confirming against a live sandbox
--------------------------------------------------
* **Signature payload order.** Implemented as the documented concatenation
  ``publicKey + amount(2dp) + currency + merchantReferenceId + timestamp``,
  HMAC-SHA256 keyed with the API password, base64-encoded. A signature rejection on
  the first call almost certainly means this field order is wrong.
* **Timestamp format.** Defaults to ISO-8601; some Geidea samples show a locale-style
  stamp. Configurable rather than hard-coded.

Whether a frictionless run may skip Authenticate Payer is undocumented, so the
adapter does not assume 4 calls: it reads the initiate response for a frictionless
marker and completes in 3 when it finds one. ``force_full_3ds`` forces the
documented 4. The measured count is what gets recorded either way — that divergence
is exactly what this platform exists to expose.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import httpx

from app.core.errors import ErrorCategory, GatewayError, NotConfigured
from app.gateways.base import (Card, CredentialField, HealthProbe, HppSession,
                               PaymentGatewayAdapter, PaymentRequest, PaymentResult)
from app.gateways.http import InstrumentedClient
from app.models.enums import NormalizedOperation, TransactionStatus

REGION_HOSTS = {
    "ksa": "https://api.ksamerchant.geidea.net",
    "egypt": "https://api.merchant.geidea.net",
    "uae": "https://api.geidea.ae",
}

#: Where the hosted-checkout component is served from, per region. Geidea's hosted
#: page is not a URL to redirect to — it is a script mounted in the merchant's own
#: page and started with a session id. These mirror the API hosts; an account served
#: from somewhere else can override the whole URL on the Settings page.
REGION_HPP_SCRIPTS = {
    "ksa": "https://www.ksamerchant.geidea.net/hpp/geideaCheckout.min.js",
    "egypt": "https://www.merchant.geidea.net/hpp/geideaCheckout.min.js",
    "uae": "https://www.geidea.ae/hpp/geideaCheckout.min.js",
}

#: Markers in an Initiate Authentication response that mean no challenge follows.
#: Undocumented, so the default is conservative: anything unrecognised is treated as
#: "challenge required" and the full sequence runs.
FRICTIONLESS_MARKERS = ("not enrolled", "notenrolled", "frictionless",
                        "authentication_not_required", "no authentication")

#: Geidea's own success code. Anything else on a 200 is a decline or an error.
SUCCESS_CODE = "000"

STATUS_MAP = {
    "success": TransactionStatus.SUCCESS,
    "paid": TransactionStatus.SUCCESS,
    "captured": TransactionStatus.SUCCESS,
    "authorized": TransactionStatus.SUCCESS,
    "refunded": TransactionStatus.SUCCESS,
    "voided": TransactionStatus.SUCCESS,
    "inprogress": TransactionStatus.PENDING,
    "pending": TransactionStatus.PENDING,
    "initiated": TransactionStatus.PENDING,
    "cancelled": TransactionStatus.CANCELLED,
    "canceled": TransactionStatus.CANCELLED,
    "failed": TransactionStatus.FAILED,
    "declined": TransactionStatus.DECLINED,
}


class GeideaAdapter(PaymentGatewayAdapter):
    code = "geidea"
    display_name = "Geidea"
    supports_hpp = True
    supports_direct = True
    # Geidea stores cards against an agreement and charges them through
    # /pay/token, so all three modes are real here.
    supported_payment_modes = ("standard", "store_card", "token")
    supported_currencies = ("SAR", "AED", "EGP", "USD")
    docs_url = "https://docs.geidea.net/"

    credential_fields = (
        CredentialField("merchant_public_key", "Merchant Public Key", secret=True,
                        placeholder="Basic-auth username",
                        help_text="Used as the HTTP Basic username and as the first field of the signature payload."),
        CredentialField("api_password", "API Password / Secret", secret=True,
                        help_text="HTTP Basic password, and the HMAC-SHA256 key for request signatures."),
        CredentialField("merchant_id", "Merchant ID", required=False,
                        help_text="Recorded for reference; not sent on the calls this adapter makes."),
        CredentialField("region", "Region", required=False, default="ksa",
                        choices=tuple(REGION_HOSTS),
                        help_text="Selects the regional API host when no explicit base URL is given."),
        CredentialField("api_base", "Base API URL", required=False,
                        placeholder="https://api.ksamerchant.geidea.net",
                        help_text="Overrides the regional host entirely."),
        CredentialField("hpp_url", "HPP URL", required=False,
                        help_text="Only needed if your account returns the hosted page under a non-standard host."),
        CredentialField("timestamp_format", "Signature timestamp format", required=False,
                        default="%Y-%m-%dT%H:%M:%S",
                        help_text="strftime format for the signed timestamp. Change this first if signatures are rejected."),
        CredentialField("force_full_3ds", "Always run Authenticate Payer", required=False,
                        default="false", choices=("true", "false"),
                        help_text="Forces the documented 4-call Direct sequence instead of detecting a frictionless run."),
        CredentialField("webhook_secret", "Callback signature secret", required=False,
                        secret=True,
                        help_text="Used to verify callback signatures when your account signs them."),

        # -- tokenisation / card on file ---------------------------------- #
        # Needed only for the stored-token payment mode. Left blank, Geidea still works
        # for ordinary card payments; the Run Benchmark page says what is missing if a
        # stored-token payment is attempted without them.
        CredentialField("agreement_id", "Agreement ID", required=False,
                        placeholder="agr_…",
                        help_text="The agreement a stored card was saved under. Required for stored-token payments; Geidea returns it when the card is first stored."),
        CredentialField("agreement_type", "Agreement type", required=False,
                        default="unscheduled",
                        choices=("unscheduled", "recurring", "installment"),
                        help_text="How the stored card may be charged. Must match the agreement that was created with the card."),
        CredentialField("token_id", "Card token ID", required=False,
                        placeholder="tok_…",
                        help_text="The stored card to charge. Run a Store card (tokenize) payment first — the token it returns is shown on the transaction page, ready to paste here."),
        CredentialField("card_on_file_initiator", "Initiated by", required=False,
                        default="Merchant", choices=("Merchant", "Customer"),
                        help_text="Merchant for an unattended recurring charge; Customer when the cardholder is present and picking a saved card."),
    )

    notes = (
        "Geidea documents Direct API as a fixed 4-call sequence with no frictionless "
        "shortcut. This adapter records the number of calls the sandbox actually "
        "required, which is the single most valuable thing to confirm live.",
        "Signature field order and timestamp format are inferred from the documentation. "
        "A rejected signature on the first call means one of those two is wrong; both "
        "are configurable on this page.",
        "Geidea reports the outcome in responseCode, not only in the HTTP status. A 200 "
        "with responseCode != 000 is a failure and is recorded as one.",
        "Hosted checkout is a component, not a redirect: the session response carries "
        "an id and Geidea's own script is mounted in this application to run the "
        "payment. Time spent in it is customer interaction time, not gateway latency.",
        "Tokenisation is a two-step affair: a Store card payment mints the token, and a "
        "later Stored token payment charges it. The token belongs to a cardholder, so "
        "it is shown once on the transaction that created it and is never stored as a "
        "credential automatically — paste it into Settings deliberately.",
    )

    documented_calls = {
        "hpp": "2 (create session + confirm order); the page itself is a mounted script",
        "direct": "4 fixed (session -> initiate -> authenticate payer -> pay)",
        "direct/token": "2 (fresh session + pay/token) — a session is required per charge",
    }

    # -- configuration ---------------------------------------------------- #

    @property
    def base(self) -> str:
        explicit = self.get("api_base")
        if explicit:
            return explicit.rstrip("/")
        region = (self.get("region") or "ksa").lower()
        return REGION_HOSTS.get(region, REGION_HOSTS["ksa"]).rstrip("/")

    def auth(self) -> Optional[httpx.Auth]:
        return httpx.BasicAuth(self.get("merchant_public_key") or "",
                               self.get("api_password") or "")

    def default_headers(self) -> Dict[str, str]:
        headers = super().default_headers()
        headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    # -- signing ---------------------------------------------------------- #

    def _timestamp(self) -> str:
        fmt = self.get("timestamp_format") or "%Y-%m-%dT%H:%M:%S"
        return datetime.now(timezone.utc).strftime(fmt)

    def _signature(self, *, amount: float, currency: str, reference: str,
                   timestamp: str) -> str:
        """base64(HMAC-SHA256(apiPassword, publicKey + amount + currency + ref + ts))."""
        payload = (f"{self.get('merchant_public_key') or ''}{self.decimal_amount(amount)}"
                   f"{currency}{reference}{timestamp}")
        digest = hmac.new((self.get("api_password") or "").encode("utf-8"),
                          payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    # -- response handling ------------------------------------------------ #

    def _check(self, client: InstrumentedClient, body: Dict[str, Any],
               what: str) -> Dict[str, Any]:
        """Read Geidea's own response code and attach it to the call that carried it."""
        code = body.get("responseCode")
        message = body.get("responseMessage") or body.get("detailedResponseMessage")
        ok = code in (None, SUCCESS_CODE)
        client.annotate_last(code=code, message=message, success=ok,
                             category=None if ok else ErrorCategory.GATEWAY_DECLINE)
        if not ok:
            raise GatewayError(
                f"Geidea: {what} responseCode={code!r} message={message!r}",
                gateway_code=str(code), raw=body)
        return body

    @staticmethod
    def _session_id(body: Dict[str, Any]) -> str:
        session = body.get("session") or {}
        value = session.get("id") or body.get("sessionId")
        if not value:
            raise GatewayError("Geidea: session response carried no session id", raw=body)
        return value

    @staticmethod
    def _redirect_url(body: Dict[str, Any]) -> Optional[str]:
        """A hosted page URL, if this account returns one.

        Most Geidea accounts do not: the documented hosted checkout is a script that
        mounts in the merchant's page and is started with the session id. Some
        configurations do hand back a URL, so it is used when present rather than
        ignored — hence returning None instead of raising.
        """
        session = body.get("session") or {}
        for candidate in ("redirectUrl", "redirect_url", "url", "paymentUrl", "checkoutUrl"):
            value = session.get(candidate) or body.get(candidate)
            if value:
                return str(value)
        return None

    def _hpp_script_url(self) -> str:
        explicit = self.get("hpp_url")
        if explicit:
            return explicit
        region = (self.get("region") or "ksa").lower()
        return REGION_HPP_SCRIPTS.get(region, REGION_HPP_SCRIPTS["ksa"])

    @staticmethod
    def _order_id(body: Dict[str, Any]) -> Optional[str]:
        order = body.get("order") or {}
        return order.get("orderId") or order.get("id") or body.get("orderId")

    def _outcome(self, body: Dict[str, Any], gateway_ref: Optional[str]) -> PaymentResult:
        order = body.get("order") or {}
        raw_status = str(order.get("status") or body.get("status") or "")
        status = STATUS_MAP.get(raw_status.lower(),
                                TransactionStatus.PENDING if not raw_status
                                else TransactionStatus.FAILED)
        return PaymentResult(status=status, gateway_reference=gateway_ref,
                             gateway_code=str(body.get("responseCode") or ""),
                             gateway_message=f"order status={raw_status!r}",
                             raw=body)

    # -- session creation ------------------------------------------------- #

    def _session_body(self, request: PaymentRequest, **overrides: Any) -> Dict[str, Any]:
        stamp = self._timestamp()
        body: Dict[str, Any] = {
            "amount": round(request.amount, 2),
            "currency": request.currency,
            "merchantReferenceId": request.reference,
            "timestamp": stamp,
            "signature": self._signature(amount=request.amount, currency=request.currency,
                                         reference=request.reference, timestamp=stamp),
        }
        if request.webhook_url:
            body["callbackUrl"] = request.webhook_url
        if request.return_url:
            body["returnUrl"] = request.return_url
        body.update(overrides)
        return body

    async def _create_session(self, client: InstrumentedClient, request: PaymentRequest,
                              *, setup: bool = False, **overrides: Any) -> Dict[str, Any]:
        issue = client.setup_call if setup else client.call
        response = await issue(
            "POST /payment-intent/api/v2/direct/session", "POST",
            self._url("/payment-intent/api/v2/direct/session"),
            normalized=NormalizedOperation.SESSION_CREATION,
            json=self._session_body(request, **overrides))
        body = await self.expect_ok(client, response, "create session")
        return self._check(client, body, "create session")

    @staticmethod
    def _card_payload(card: Card) -> Dict[str, Any]:
        return {
            "cardNumber": card.number,
            "cardholderName": card.holder,
            "expiryMonth": int(card.month),
            "expiryYear": int(card.year),
            "cvv": card.cvc,
        }

    # -- HPP -------------------------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        """Create the session the hosted checkout runs against.

        Geidea's hosted page is a component, not a destination: the session response
        carries an id, and the browser starts Geidea's own checkout script with it.
        That is the same shape as Adyen's Drop-in and HyperPay's Copy&Pay, so the
        platform mounts it in a page of its own and the customer never leaves the
        origin until Geidea's script decides they should.

        An account configured to return a hosted URL still gets a plain redirect.
        """
        body = await self._create_session(client, request)
        session_id = self._session_id(body)
        context = {"session_id": session_id, "reference": request.reference}

        redirect = self._redirect_url(body)
        if redirect:
            return HppSession(redirect_url=redirect, gateway_reference=session_id,
                              mode="redirect", context=context)

        return HppSession(redirect_url=f"/hpp/widget/geidea/{session_id}",
                          gateway_reference=session_id, mode="widget",
                          context={**context, "script_url": self._hpp_script_url()})

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        """The server-side order fetch, which is the authoritative outcome.

        Geidea also POSTs a callback. The pull is treated as authoritative because it
        is the leg the merchant controls and can retry; the callback's arrival time is
        recorded separately as webhook latency.
        """
        order_id = (params.get("orderId") or params.get("order_id")
                    or context.get("order_id") or context.get("session_id"))
        if not order_id:
            raise GatewayError("Geidea: no orderId or sessionId available to confirm against")
        response = await client.call(
            "GET /pgw/api/v1/direct/order/{orderId}", "GET",
            self._url(f"/pgw/api/v1/direct/order/{order_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = self._check(client, await self.expect_ok(client, response, "fetch order",
                                                        accept=(200,)), "fetch order")
        return self._outcome(body, self._order_id(body) or str(order_id))

    # -- Direct API ------------------------------------------------------- #

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        if request.payment_mode == "token":
            return await self._pay_with_stored_token(client, request)

        if request.card is None:
            raise GatewayError("Geidea Direct API needs card details to authenticate.")
        card = request.card

        # Storing the card is a property of the session, so the mode has to be decided
        # before the first call rather than bolted on at authorisation time.
        store_card = request.payment_mode == "store_card"
        session_body = await self._create_session(
            client, request, **({"cardOnFile": True} if store_card else {}))
        session_id = self._session_id(session_body)

        initiate_response = await client.call(
            "POST /pgw/api/v6/direct/authenticate/initiate", "POST",
            self._url("/pgw/api/v6/direct/authenticate/initiate"),
            normalized=NormalizedOperation.AUTHENTICATION,
            json={"sessionId": session_id, "paymentMethod": self._card_payload(card)})
        initiate = self._check(
            client, await self.expect_ok(client, initiate_response, "initiate authentication"),
            "initiate authentication")

        challenge = self._force_full_3ds or self._challenge_expected(initiate)
        if challenge:
            payer_response = await client.call(
                "POST /pgw/api/v6/direct/authenticate/payer", "POST",
                self._url("/pgw/api/v6/direct/authenticate/payer"),
                normalized=NormalizedOperation.AUTHENTICATION,
                json={"sessionId": session_id, "paymentMethod": self._card_payload(card),
                      "returnUrl": request.return_url})
            self._check(client, await self.expect_ok(client, payer_response,
                                                     "authenticate payer"),
                        "authenticate payer")

        pay_response = await client.call(
            "POST /pgw/api/v2/direct/pay", "POST", self._url("/pgw/api/v2/direct/pay"),
            normalized=NormalizedOperation.AUTHORIZATION,
            json={"sessionId": session_id, "paymentMethod": self._card_payload(card),
                  "paymentOperation": "Pay"})
        paid = self._check(client, await self.expect_ok(client, pay_response, "pay"), "pay")

        result = self._outcome(paid, self._order_id(paid) or session_id)
        result.three_ds_required = challenge
        if store_card:
            token, hint = self._stored_token(paid)
            result.stored_token = token
            result.stored_token_hint = hint
            if token is None:
                # Worth saying plainly: the payment succeeded, the tokenisation did
                # not, and a later stored-token charge has nothing to charge.
                result.gateway_message = (
                    (result.gateway_message or "")
                    + " — no card token was returned. The account may not have card "
                      "storage enabled for this agreement.").strip()
        return result

    async def _pay_with_stored_token(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        """Charge a card Geidea already holds — the merchant-initiated flow.

        Two calls, and both recur on every charge: Geidea requires a fresh session
        object per payment even when the card is already stored, which is the only
        gateway in this set that does. No card details are sent, so there is no
        authentication step and nothing to enter.
        """
        token_id = self.get("token_id")
        agreement_id = self.get("agreement_id")
        missing = [label for label, value in
                   (("Card token ID", token_id), ("Agreement ID", agreement_id))
                   if not value]
        if missing:
            raise NotConfigured(
                f"A stored-token payment needs {' and '.join(missing)} on Geidea's "
                "settings. Run a Store card (tokenize) payment first — it returns the "
                "token and the agreement, and the transaction page shows both.")

        session_body = await self._create_session(client, request, cardOnFile=True)
        session_id = self._session_id(session_body)

        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "tokenId": token_id,
            "agreementId": agreement_id,
            "agreementType": self.get("agreement_type") or "unscheduled",
            "initiatedBy": self.get("card_on_file_initiator") or "Merchant",
            "paymentOperation": "Pay",
        }
        response = await client.call(
            "POST /pgw/api/v2/direct/pay/token", "POST",
            self._url("/pgw/api/v2/direct/pay/token"),
            normalized=NormalizedOperation.AUTHORIZATION, json=payload)
        body = self._check(client, await self.expect_ok(client, response, "pay by token"),
                           "pay by token")
        return self._outcome(body, self._order_id(body) or session_id)

    @staticmethod
    def _stored_token(body: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """Pull a minted card token out of a payment response.

        Geidea has returned this under more than one shape, so several are accepted.
        Only the token id and a masked hint are taken — never a PAN, and the sanitizer
        would truncate one anyway.
        """
        order = body.get("order") or {}
        containers = (order.get("tokenId"), body.get("tokenId"),
                      (order.get("paymentMethod") or {}).get("tokenId"),
                      (body.get("paymentMethod") or {}).get("tokenId"))
        token = next((value for value in containers if value), None)
        payment_method = order.get("paymentMethod") or body.get("paymentMethod") or {}
        masked = payment_method.get("maskedCardNumber") or payment_method.get("cardNumber")
        hint = None
        if masked:
            digits = "".join(ch for ch in str(masked) if ch.isdigit())
            if len(digits) >= 4:
                hint = f"card ending {digits[-4:]}"
        brand = payment_method.get("brand") or payment_method.get("cardBrand")
        if brand:
            hint = f"{brand} {hint}" if hint else str(brand)
        return token, hint

    @property
    def _force_full_3ds(self) -> bool:
        return str(self.get("force_full_3ds") or "false").lower() in ("1", "true", "yes")

    @staticmethod
    def _challenge_expected(initiate: Dict[str, Any]) -> bool:
        """True unless the initiate response explicitly says no challenge is coming."""
        blob = json.dumps(initiate, default=str).lower()
        return not any(marker in blob for marker in FRICTIONLESS_MARKERS)

    # -- status and refund ------------------------------------------------ #

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        response = await client.call(
            "GET /pgw/api/v1/direct/order/{orderId}", "GET",
            self._url(f"/pgw/api/v1/direct/order/{payment_id}"),
            normalized=NormalizedOperation.STATUS_CHECK)
        body = self._check(client, await self.expect_ok(client, response, "fetch order",
                                                        accept=(200,)), "fetch order")
        return self._outcome(body, payment_id)

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        stamp = self._timestamp()
        payload: Dict[str, Any] = {"orderId": payment_id, "timestamp": stamp}
        if amount is not None and currency:
            payload.update({
                "refundAmount": round(amount, 2),
                "currency": currency,
                "signature": self._signature(amount=amount, currency=currency,
                                             reference=payment_id, timestamp=stamp),
            })
        response = await client.call(
            "POST /pgw/api/v2/direct/refund", "POST",
            self._url("/pgw/api/v2/direct/refund"),
            normalized=NormalizedOperation.REFUND, json=payload)
        body = self._check(client, await self.expect_ok(client, response, "refund"), "refund")
        return self._outcome(body, payment_id)

    # -- webhook ---------------------------------------------------------- #

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        order = payload.get("order") or payload
        secret = self.get("webhook_secret")
        provided = headers.get("x-geidea-signature") or headers.get("signature")
        verified: Optional[bool] = None
        if secret and provided:
            expected = base64.b64encode(
                hmac.new(secret.encode("utf-8"), body or b"", hashlib.sha256).digest()
            ).decode("utf-8")
            verified = hmac.compare_digest(expected, provided)
        return {
            "event_type": payload.get("type") or "order.update",
            "reference": order.get("merchantReferenceId"),
            "gateway_reference": order.get("orderId") or order.get("id"),
            "status": order.get("status"),
            "signature_verified": verified,
            "payload": payload,
        }

    # -- health ----------------------------------------------------------- #

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """Reachability only: an order lookup for an id that does not exist.

        No payment is created. A 4xx is a perfectly good answer here — it proves the
        API is up, authenticating and routing, which is exactly what is being probed.
        """
        started = time.perf_counter()
        try:
            response = await client.call(
                "GET /pgw/api/v1/direct/order/{probe}", "GET",
                self._url("/pgw/api/v1/direct/order/burapay-health-probe"),
                normalized=NormalizedOperation.HEALTH_CHECK)
        except httpx.HTTPError as exc:
            return HealthProbe(False, round((time.perf_counter() - started) * 1000, 3),
                               None, f"{type(exc).__name__}: {exc}")
        return HealthProbe(available=response.status_code < 500,
                           response_time_ms=round((time.perf_counter() - started) * 1000, 3),
                           http_status=response.status_code,
                           detail="order lookup probe; no payment created")
