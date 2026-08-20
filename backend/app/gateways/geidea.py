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

Direct API — 4 calls, and the three card-bearing calls do not agree on shape::

    POST /payment-intent/api/v2/direct/session
    POST /pgw/api/v6/direct/authenticate/initiate   cardNumber at the top level
    POST /pgw/api/v6/direct/authenticate/payer      card nested under paymentMethod
    POST /pgw/api/v2/direct/pay                     card optional; threeDSecureId binds it

That first difference is not cosmetic. Sending Initiate Authentication a
``paymentMethod`` object — the shape the next two calls want — means Geidea finds
neither a card number nor a token where it looks for them, and answers
``responseCode=100`` "General error" with ``detailedResponseCode=069`` *Missing Token
Id*. The error names the token branch of a check that failed for want of a card.

There is no server-side tokenize endpoint. A token is minted *by a payment*: send
``cardOnFile: true`` on the session, together with a ``cofAgreement`` whose id the
merchant chooses — Geidea does not issue one — and the token comes back on the
successful payment. That token plus that agreement id are what a later charge needs.

Stored-card charges go to ``POST /pgw/api/v2/direct/pay/token`` and differ only in
``initiatedBy``: ``Customer`` when the cardholder is present and picking a saved card
(CIT), ``Merchant`` for an unattended charge (MIT). The card networks treat those
differently, so it is a per-payment choice rather than a setting.

Authenticate Payer is where a 3DS challenge surfaces. When the response carries
somewhere to send the cardholder — a URL, or an auto-submitting form — the adapter
stops and returns ``requires_customer_action``. The customer does the OTP, the issuer
returns them to ``return_url``, and ``complete_direct_payment`` runs the Pay call. The
time in between is customer interaction time, not gateway latency.

Two details need confirming against a live sandbox
--------------------------------------------------
* **Whether Pay needs the card again after a challenge.** It is not sent: the card is
  not kept across the pause, and the payment is identified by ``sessionId``,
  ``orderId`` and ``threeDSecureId``, which Geidea already holds the card against.
  Holding card data between two requests to avoid this would be the wrong trade.
* **Signature payload order.** Implemented as the documented concatenation
  ``publicKey + amount(2dp) + currency + merchantReferenceId + timestamp``,
  HMAC-SHA256 keyed with the API password, base64-encoded. A signature rejection on
  the first call almost certainly means this field order is wrong.
* **Timestamp format.** Defaults to ISO-8601; some Geidea samples show a locale-style
  stamp. Configurable rather than hard-coded.

"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
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

#: What Geidea calls the two card-on-file initiators. A customer-initiated charge is
#: ``Internet`` — the cardholder is present, online — and not the word "Customer",
#: which is what the UI says because it is what the distinction means to a person.
CIT_INITIATOR = "Internet"
MIT_INITIATOR = "Merchant"

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
        CredentialField("source", "Channel", required=False, default="DirectAPI",
                        choices=("DirectAPI", "HPP", "InApp"),
                        help_text="Sent as `source` on the Direct API calls. Geidea's own samples use DirectAPI here; this is not a free-text field and an unrecognised value is rejected."),
        CredentialField("merchant_name", "Merchant name", required=False,
                        help_text="Shown to the cardholder during 3DS when your account does not already carry one."),
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
                        placeholder="burapay-agr-…",
                        help_text="The agreement a stored card was saved under. You choose this value, not Geidea: it is sent on the card-on-file payment and quoted again on every later charge. Leave blank and a Store card payment mints one and shows it on the transaction."),
        CredentialField("agreement_type", "Agreement type", required=False,
                        default="unscheduled",
                        choices=("unscheduled", "recurring", "installment"),
                        help_text="How the stored card may be charged. Must match the agreement that was created with the card."),
        CredentialField("token_id", "Card token ID", required=False,
                        placeholder="tok_…",
                        help_text="The stored card to charge. Run a Store card (tokenize) payment first — the token it returns is shown on the transaction page, ready to paste here."),
        CredentialField("card_on_file_initiator", "Initiated by (default)", required=False,
                        default="Merchant", choices=("Merchant", "Customer"),
                        help_text="The default for stored-token payments: Merchant for an unattended charge (MIT), Customer when the cardholder is present and picking a saved card (CIT). The payment form overrides it per transaction."),
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
        "The three card-bearing Direct API calls do not agree on shape: Initiate "
        "Authentication takes a bare cardNumber at the top level, while Authenticate "
        "Payer and Pay take a nested paymentMethod with the expiry as an object. "
        "Sending the nested shape to the first one is answered with detailed code 069, "
        "Missing Token Id, which names the wrong half of the check that failed.",
        "A card token is minted by a payment, not by a separate tokenize call: the "
        "session carries cardOnFile and an agreement id that the merchant chooses, and "
        "the token comes back on the successful payment. Both are shown on that "
        "transaction, and either can be pasted into a later charge.",
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
        """Read Geidea's own response code and attach it to the call that carried it.

        Both levels of Geidea's error reporting are reported. ``responseMessage`` is
        frequently the useless "General error" while ``detailedResponseMessage`` says
        what actually went wrong, so preferring one over the other — as this did —
        throws away the informative half exactly when it is needed.
        """
        code = body.get("responseCode")
        message = body.get("responseMessage")
        detailed_code = body.get("detailedResponseCode")
        detailed_message = body.get("detailedResponseMessage")
        ok = code in (None, SUCCESS_CODE)

        parts = [f"responseCode={code!r}"]
        if message:
            parts.append(f"message={message!r}")
        if detailed_code and detailed_code != code:
            parts.append(f"detailedResponseCode={detailed_code!r}")
        if detailed_message and detailed_message != message:
            parts.append(f"detailedResponseMessage={detailed_message!r}")
        summary = " ".join(parts)

        client.annotate_last(code=detailed_code or code,
                             message=detailed_message or message, success=ok,
                             category=None if ok else ErrorCategory.GATEWAY_DECLINE)
        if not ok:
            raise GatewayError(f"Geidea: {what} {summary}{self._hint(what, code)}",
                               gateway_code=str(detailed_code or code), raw=body)
        return body

    @staticmethod
    def _hint(what: str, code: Any) -> str:
        """A short note on what to check, for the errors that say nothing themselves.

        Geidea answers 100/"General error" for several unrelated conditions, and the
        detailed code underneath it is often the only thing that identifies the cause.
        These point at what to change rather than at the code itself.
        """
        if str(code) == "013":
            return (
                ". Geidea answers this when it could not parse or accept the request "
                "body, and it does not say which field. Open the failed call on the "
                "transaction page and expand it: the request that was sent is recorded "
                "there. Check `source` first — it is an enum, not free text — then the "
                "card expiry, which Geidea wants as two zero-padded strings")
        if str(code) == "069":
            return (
                ". Geidea reports this when Initiate Authentication finds neither a "
                "card number nor a token id — the card number belongs at the top level "
                "of that request, not inside paymentMethod, which is the shape the two "
                "calls after it want")
        if "initiate authentication" not in what or str(code) != "100":
            return ""
        return (
            ". This code is Geidea's generic one and does not identify the cause; the "
            "detailed code underneath it usually does, and is reported above when "
            "Geidea sends one. Failing that, the usual cause is an account that is not "
            "enabled for Direct API card payments — many sandbox accounts are not. "
            "Hosted checkout and stored-token payments do not use this step, so either "
            "will run on an account that cannot do Direct")

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
        """The nested shape Authenticate Payer and Pay want.

        The expiry is an object holding two zero-padded *strings* — Geidea's samples
        show ``{"month": "01", "year": "39"}``. A four-digit year is trimmed to two,
        and single digits keep their leading zero, because "1" and "01" are not the
        same string and only one of them is what the sample shows.
        """
        return {
            "cardholderName": card.holder,
            "cardNumber": card.number,
            "cvv": card.cvc,
            "expiryDate": {"month": f"{int(card.month):02d}",
                           "year": f"{int(str(card.year)[-2:]):02d}"},
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
            raise GatewayError(
                "Geidea Direct API needs card details. Enter a card on the payment "
                "form, or choose the stored-token payment mode to charge a card Geidea "
                "already holds.")
        card = request.card

        # Card on file is a property of the *session*, so tokenisation has to be asked
        # for before the first call rather than bolted on at authorisation time. The
        # agreement is minted here, by us: Geidea does not hand one out. It comes back
        # on the transaction page next to the token, and the two together are what a
        # later merchant-initiated charge needs.
        store_card = request.payment_mode == "store_card"
        agreement_id = self._agreement_id(request) if store_card else None
        agreement_type = self.get("agreement_type") or "unscheduled"

        overrides: Dict[str, Any] = {}
        if store_card:
            overrides = {
                "cardOnFile": True,
                # The payment that mints a token is customer-initiated by definition:
                # the cardholder is here, entering a card.
                "initiatedBy": CIT_INITIATOR,
                "cofAgreement": {"id": agreement_id, "type": agreement_type},
            }
        session_body = await self._create_session(client, request, **overrides)
        session_id = self._session_id(session_body)

        initiate = await self._initiate_authentication(client, request, session_id,
                                                       card, store_card=store_card)
        order_id = self._order_id(initiate) or session_id
        three_ds_id = self._three_ds_id(initiate)

        challenge = self._force_full_3ds or self._challenge_expected(initiate)
        if challenge:
            payer = await self._authenticate_payer(client, request, session_id,
                                                   order_id, card)
            three_ds_id = self._three_ds_id(payer) or three_ds_id

            action = self._customer_action(payer)
            if action:
                # The issuer wants the cardholder. Stop here rather than calling Pay
                # against an unauthenticated session: the OTP happens in the browser
                # and the flow resumes at complete_direct_payment.
                #
                # The card deliberately does not survive this pause. It is not written
                # down, not held between requests, and not put in the transaction's
                # context — so the Pay call on the way back identifies the payment by
                # its session, order and 3DS ids instead.
                return PaymentResult(
                    status=TransactionStatus.PENDING,
                    gateway_reference=order_id,
                    gateway_code=str(payer.get("responseCode") or ""),
                    gateway_message="3DS challenge required; awaiting the cardholder",
                    three_ds_required=True, requires_customer_action=True,
                    action_url=action.get("url"),
                    context={"session_id": session_id, "order_id": order_id,
                             "three_ds_id": three_ds_id, "store_card": store_card,
                             "agreement_id": agreement_id,
                             "agreement_type": agreement_type,
                             "challenge_url": action.get("url"),
                             "challenge_html": action.get("html")},
                    raw=payer)

        return await self._pay(client, request, session_id, order_id=order_id,
                               three_ds_id=three_ds_id, card=card,
                               store_card=store_card, agreement_id=agreement_id,
                               agreement_type=agreement_type,
                               three_ds_required=challenge)

    async def complete_direct_payment(self, client: InstrumentedClient,
                                      request: PaymentRequest,
                                      context: Mapping[str, Any],
                                      params: Mapping[str, str]) -> PaymentResult:
        """Run the Pay call once the cardholder has finished the 3DS challenge.

        No card is sent, because none was kept. Geidea already holds the card against
        the session from the two authentication calls, and the payment is identified by
        ``sessionId``, ``orderId`` and the 3DS id the challenge produced.

        Only this call is timed, which is the point: everything between the Authenticate
        Payer response and the issuer sending the browser back was the customer's time.
        """
        session_id = context.get("session_id")
        if not session_id:
            raise GatewayError(
                "Geidea: cannot resume this payment — the session it paused on was not "
                "recorded.")
        return await self._pay(
            client, request, str(session_id),
            order_id=str(context.get("order_id") or session_id),
            three_ds_id=context.get("three_ds_id"), card=None,
            store_card=bool(context.get("store_card")),
            agreement_id=context.get("agreement_id"),
            agreement_type=str(context.get("agreement_type") or "unscheduled"),
            three_ds_required=True)

    async def _initiate_authentication(self, client: InstrumentedClient,
                                       request: PaymentRequest, session_id: str,
                                       card: Card, *, store_card: bool) -> Dict[str, Any]:
        """Step 2. The card number goes at the top level here, not under paymentMethod.

        This is the call that answered ``069 Missing Token Id`` when it was sent a
        ``paymentMethod`` object: Geidea found neither a card number nor a token where
        it looks for them, and reported the token half of that check.
        """
        # Every field Geidea's own samples carry is sent, including the ones that
        # look optional. The four booleans are always present rather than omitted:
        # a request that leaves them out is not a request Geidea has an example of,
        # and this call answers a malformed body with 013 "Internal Server Error"
        # rather than saying which field it disliked.
        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "cardNumber": card.number,
            "amount": round(request.amount, 2),
            "currency": request.currency,
            "merchantName": self.get("merchant_name") or "BuraPay Benchmark",
            "paymentOperation": "Pay",
            "source": self._source,
            "deviceIdentification": self._device(request),
            "cardOnFile": store_card,
            "isSetPaymentMethodEnabled": False,
            "isCreateCustomerEnabled": False,
            "restrictPaymentMethods": False,
        }
        if request.return_url:
            payload["ReturnUrl"] = request.return_url
        if request.webhook_url:
            payload["callbackUrl"] = request.webhook_url

        response = await client.call(
            "POST /pgw/api/v6/direct/authenticate/initiate", "POST",
            self._url("/pgw/api/v6/direct/authenticate/initiate"),
            normalized=NormalizedOperation.AUTHENTICATION, json=payload)
        return self._check(
            client, await self.expect_ok(client, response, "initiate authentication"),
            "initiate authentication")

    async def _authenticate_payer(self, client: InstrumentedClient,
                                  request: PaymentRequest, session_id: str,
                                  order_id: str, card: Card) -> Dict[str, Any]:
        """Step 3. Here the card *is* nested, and the expiry is an object."""
        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "orderId": order_id,
            "source": self._source,
            "paymentMethod": self._card_payload(card),
            "deviceIdentification": self._device(request),
        }
        time_zone = self._time_zone(request)
        if time_zone is not None:
            payload["timeZone"] = time_zone
        if request.return_url:
            payload["ReturnUrl"] = request.return_url

        response = await client.call(
            "POST /pgw/api/v6/direct/authenticate/payer", "POST",
            self._url("/pgw/api/v6/direct/authenticate/payer"),
            normalized=NormalizedOperation.AUTHENTICATION, json=payload)
        return self._check(
            client, await self.expect_ok(client, response, "authenticate payer"),
            "authenticate payer")

    async def _pay(self, client: InstrumentedClient, request: PaymentRequest,
                   session_id: str, *, order_id: str, three_ds_id: Optional[str],
                   card: Optional[Card], store_card: bool,
                   agreement_id: Optional[str], agreement_type: str,
                   three_ds_required: bool) -> PaymentResult:
        """Step 4. The card is included when we still have it, and omitted when we do not."""
        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "orderId": order_id,
            "paymentOperation": "Pay",
            "source": self._source,
        }
        if three_ds_id:
            payload["threeDSecureId"] = three_ds_id
        if card is not None:
            payload["paymentMethod"] = self._card_payload(card)
        if request.return_url:
            payload["ReturnUrl"] = request.return_url

        response = await client.call(
            "POST /pgw/api/v2/direct/pay", "POST", self._url("/pgw/api/v2/direct/pay"),
            normalized=NormalizedOperation.AUTHORIZATION, json=payload)
        paid = self._check(client, await self.expect_ok(client, response, "pay"), "pay")

        result = self._outcome(paid, self._order_id(paid) or order_id)
        result.three_ds_required = three_ds_required
        token, hint = self._stored_token(paid)
        if token:
            # Surfaced from any payment that produced one, not only from the mode
            # named "store card": a token is what a later charge needs, and hiding it
            # because of which button was pressed helps nobody.
            result.stored_token = token
            result.stored_token_hint = hint
            result.agreement_id = agreement_id
            result.agreement_type = agreement_type
        elif store_card:
            # Worth saying plainly: the payment succeeded, the tokenisation did not,
            # and a later stored-token charge has nothing to charge.
            result.gateway_message = (
                (result.gateway_message or "")
                + " — no card token was returned. Geidea mints one on a card-on-file "
                  "payment that completed 3DS successfully; the account may not have "
                  "card storage enabled.").strip()
        return result

    async def _pay_with_stored_token(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        """Charge a card Geidea already holds.

        Two calls, and both recur on every charge: Geidea requires a fresh session
        object per payment even when the card is already stored, which is the only
        gateway in this set that does. No card details are sent, so there is no
        authentication step and nothing to enter.

        Merchant-initiated and customer-initiated differ only in ``initiatedBy``. A
        customer-initiated charge is the cardholder picking a saved card; a
        merchant-initiated one is an unattended charge with nobody watching, and the
        card networks treat the two differently, so it is chosen per payment rather
        than fixed in configuration.
        """
        # The payment form's values win over the stored credentials, so a token minted
        # by an earlier payment can be charged straight away without a settings detour.
        token_id = request.token_id or self.get("token_id")
        agreement_id = self._agreement_id(request, mint=False) or self.get("agreement_id")
        missing = [label for label, value in
                   (("Card token ID", token_id), ("Agreement ID", agreement_id))
                   if not value]
        if missing:
            raise NotConfigured(
                f"A stored-token payment needs {' and '.join(missing)}. Both come from "
                "the payment that stored the card — run a Store card (tokenize) "
                "payment first and its transaction page shows them, ready to paste "
                "into the payment form or into Geidea's settings.")

        initiated_by = self._initiator(request)
        agreement_type = self.get("agreement_type") or "unscheduled"
        session_body = await self._create_session(
            client, request, cardOnFile=True, initiatedBy=initiated_by,
            cofAgreement={"id": agreement_id, "type": agreement_type})
        session_id = self._session_id(session_body)

        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "tokenId": token_id,
            "agreementId": agreement_id,
            "agreementType": agreement_type,
            "initiatedBy": initiated_by,
            "paymentOperation": "Pay",
            "source": self._source,
        }
        if request.return_url:
            payload["ReturnUrl"] = request.return_url
        if request.webhook_url:
            payload["callbackUrl"] = request.webhook_url

        response = await client.call(
            "POST /pgw/api/v2/direct/pay/token", "POST",
            self._url("/pgw/api/v2/direct/pay/token"),
            normalized=NormalizedOperation.AUTHORIZATION, json=payload)
        body = self._check(client, await self.expect_ok(client, response, "pay by token"),
                           "pay by token")
        result = self._outcome(body, self._order_id(body) or session_id)
        result.stored_token = token_id
        result.agreement_id = agreement_id
        result.agreement_type = agreement_type
        return result

    # -- Direct API helpers ----------------------------------------------- #

    @property
    def _source(self) -> str:
        return self.get("source") or "DirectAPI"

    def _agreement_id(self, request: PaymentRequest, *, mint: bool = True) -> Optional[str]:
        """The agreement this card is being stored under.

        Geidea does not issue one — the merchant chooses it and sends it on the
        card-on-file session, then quotes the same value on every later charge. So a
        Store card payment mints one here when nothing was supplied, and it is shown
        beside the token afterwards.
        """
        supplied = request.metadata.get("agreement_id") or self.get("agreement_id")
        if supplied:
            return str(supplied)
        return f"burapay-agr-{uuid.uuid4().hex[:20]}" if mint else None

    def _initiator(self, request: PaymentRequest) -> str:
        """Translate the choice a person made into the value Geidea wants.

        The UI offers "Merchant" and "Customer" because that is what the distinction
        means: nobody watching, or the cardholder picking a saved card. Geidea spells
        the second one ``Internet``.
        """
        chosen = str(request.metadata.get("initiated_by")
                     or self.get("card_on_file_initiator") or "Merchant").lower()
        return CIT_INITIATOR if chosen.startswith(("cust", "internet", "cit")) else MIT_INITIATOR

    def _device(self, request: PaymentRequest) -> Dict[str, Any]:
        """The browser data 3DS wants, forwarded from the browser that started this.

        A server-to-server call cannot know the cardholder's user agent or language,
        and an issuer asked to risk-score a payment without them will reach for a
        challenge more often. The front end sends what it can see; whatever is missing
        stays missing rather than being invented.
        """
        browser = request.browser or {}
        # Geidea's samples show a 32-character hex id here, so a browser id that is not
        # already in that shape is hashed into it rather than sent as-is. The hash is
        # over a value the browser generated for itself — it is not derived from
        # anything about the machine or the person.
        supplied = str(browser.get("device_id") or request.reference)
        device: Dict[str, Any] = {
            "providerDeviceId": hashlib.md5(supplied.encode("utf-8")).hexdigest(),
        }
        if browser.get("user_agent"):
            device["userAgent"] = browser["user_agent"]
        if browser.get("language"):
            device["language"] = browser["language"]
        return device

    @staticmethod
    def _time_zone(request: PaymentRequest) -> Optional[int]:
        value = (request.browser or {}).get("time_zone_offset_minutes")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _customer_action(payer: Dict[str, Any]) -> Optional[Dict[str, Optional[str]]]:
        """Where to send the cardholder for a 3DS challenge, if there is anywhere.

        Read out of the response rather than assumed: an issuer challenge arrives
        either as a URL to navigate to or as an auto-submitting form to render, and
        which one depends on the issuer, not on Geidea. Returns ``None`` for a
        frictionless authentication, which is the case that should just carry on to
        Pay. No field name here is ever *sent* — these are only read.
        """
        url_keys = ("redirectUrl", "redirecturl", "acsUrl", "acsurl", "challengeUrl",
                    "challengeurl", "threeDSecureUrl", "threedsecureurl")
        html_keys = ("redirectHtml", "redirecthtml", "htmlBodyContent", "htmlbodycontent",
                     "challengeHtml", "challengehtml", "html")
        found: Dict[str, Optional[str]] = {"url": None, "html": None}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and value.strip():
                        if found["url"] is None and key in url_keys:
                            found["url"] = value.strip()
                        elif found["html"] is None and key in html_keys:
                            found["html"] = value
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payer)
        return found if (found["url"] or found["html"]) else None

    @staticmethod
    def _three_ds_id(body: Dict[str, Any]) -> Optional[str]:
        """The id that ties a completed authentication to the payment that follows."""
        for container in (body, body.get("threeDSecure") or {},
                          body.get("order") or {}, body.get("authentication") or {}):
            if not isinstance(container, dict):
                continue
            for key in ("threeDSecureId", "threeDSecureID", "id", "transactionId"):
                if key == "id" and container is body:
                    continue          # the top-level id is the response's, not the 3DS one
                value = container.get(key)
                if value:
                    return str(value)
        return None

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
