"""
Geidea adapter — the baseline this project exists to measure.

Auth: HTTP Basic — Merchant Public Key as username, API Password as password.
Bodies: JSON.

Regional hosts (identical paths, different host), selected by GEIDEA_REGION:
    eg  -> api.merchant.geidea.net      Egypt / global
    ksa -> api.ksamerchant.geidea.net   Saudi Arabia
    uae -> api.geidea.ae                UAE
A SAR-denominated sandbox is almost always KSA-issued; GEIDEA_API_BASE overrides
the lookup entirely.

Documented call sequences (docs/02_api_flow_comparison.md):

    Hosted checkout — 2 calls
        POST /payment-intent/api/v2/direct/session
        GET  /pgw/api/v1/direct/order/{orderId}

    Direct API — 4 calls, documented as fixed regardless of 3DS outcome
        POST /payment-intent/api/v2/direct/session
        POST /pgw/api/v6/direct/authenticate/initiate
        POST /pgw/api/v6/direct/authenticate/payer
        POST /pgw/api/v2/direct/pay

NEEDS SANDBOX CONFIRMATION
--------------------------
Two signing details could not be pinned down from the public documentation:

  * **Signature payload order.** Implemented as the documented concatenation
    ``publicKey + amount(2dp) + currency + merchantReferenceId + timestamp``,
    HMAC-SHA256 keyed with the API password, base64-encoded. A signature rejection
    on the first call almost certainly means this field order is wrong.
  * **Timestamp format.** Defaults to ISO-8601. Some Geidea samples show a
    locale-style stamp; override with GEIDEA_TIMESTAMP_FORMAT (a strftime string)
    rather than editing this file.

Whether a frictionless (no-challenge) Direct run may skip Authenticate Payer is
undocumented — it is the open question this whole benchmark exists to settle. So
this adapter does not hard-code 4 calls: it reads the initiate response for a
frictionless marker and, finding one, completes in 3. GEIDEA_FORCE_FULL_3DS=1
forces the documented 4 regardless.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from app.adapters.base import Adapter, Card, Charge, Outcome, Started
from app.config import env, env_bool
from app.timing import GatewayError, MeasuredSession, NotConfigured, money

REGION_HOSTS = {
    "eg": "https://api.merchant.geidea.net",
    "egypt": "https://api.merchant.geidea.net",
    "global": "https://api.merchant.geidea.net",
    "ksa": "https://api.ksamerchant.geidea.net",
    "sa": "https://api.ksamerchant.geidea.net",
    "uae": "https://api.geidea.ae",
    "ae": "https://api.geidea.ae",
}

#: Signals in an Initiate Authentication response that no challenge will follow.
#: Undocumented, so the default is conservative — anything unrecognised is treated
#: as "challenge required" and the full 4-call sequence runs.
FRICTIONLESS_MARKERS = ("not enrolled", "notenrolled", "frictionless",
                        "authentication_not_required", "no authentication")


class GeideaAdapter(Adapter):
    name = "geidea"
    label = "Geidea"

    documented_calls = {
        "hosted_checkout": "2 (create session + confirm order)",
        "direct_api": "4 fixed (session → initiate → authenticate payer → pay)",
        "capture": "1", "refund": "1", "void": "1",
        "mit": "2 (fresh session per charge + pay/token)",
    }

    notes = [
        "Direct API is documented as a fixed 4-call sequence with no frictionless "
        "shortcut. This adapter records the number of calls the sandbox actually "
        "required, which is the single most important thing to confirm live.",
        "MIT needs a fresh session object on every recurring charge — the only "
        "gateway in the set that does.",
        "Signature field order and timestamp format are inferred from the docs. If a "
        "call is rejected for a bad signature, those are the two things to change.",
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        self.public_key = env("GEIDEA_PUBLIC_KEY")
        self.api_password = env("GEIDEA_API_PASSWORD")
        region = (env("GEIDEA_REGION", "ksa") or "ksa").lower()
        self.region = region
        self.base = (env("GEIDEA_API_BASE")
                     or REGION_HOSTS.get(region, REGION_HOSTS["ksa"])).rstrip("/")
        self.timestamp_format = env("GEIDEA_TIMESTAMP_FORMAT", "%Y-%m-%dT%H:%M:%S")
        self.force_full_3ds = env_bool("GEIDEA_FORCE_FULL_3DS", False)
        self.token_id = env("GEIDEA_TOKEN_ID")
        self.agreement_id = env("GEIDEA_AGREEMENT_ID")

    # -- configuration ---------------------------------------------------- #

    def missing_config(self) -> List[str]:
        missing = []
        if not self.public_key:
            missing.append("GEIDEA_PUBLIC_KEY")
        if not self.api_password:
            missing.append("GEIDEA_API_PASSWORD")
        return missing

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (self.public_key or "", self.api_password or "")
        session.headers["Content-Type"] = "application/json"
        return session

    # -- signing ---------------------------------------------------------- #

    def timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime(self.timestamp_format or "%Y-%m-%dT%H:%M:%S")

    def sign(self, *, amount: float, currency: str, reference: str, timestamp: str) -> str:
        """base64(HMAC-SHA256(apiPassword, publicKey + amount + currency + ref + timestamp))."""
        payload = f"{self.public_key}{money(amount)}{currency}{reference}{timestamp}"
        digest = hmac.new((self.api_password or "").encode("utf-8"),
                          payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    # -- helpers ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _card_payload(self, card: Card) -> Dict[str, Any]:
        return {
            "cardNumber": card.number,
            "cardholderName": card.holder,
            "expiryMonth": int(card.month),
            "expiryYear": int(card.year),
            "cvv": card.cvc,
        }

    @staticmethod
    def _check(body: Dict[str, Any], what: str) -> Dict[str, Any]:
        """Geidea reports outcome in responseCode ('000' = success), not only via HTTP status."""
        code = body.get("responseCode")
        if code not in (None, "000"):
            raise GatewayError(
                f"geidea: {what} responseCode={code!r} "
                f"message={body.get('responseMessage')!r} "
                f"detail={body.get('detailedResponseMessage')!r}")
        return body

    def _session_body(self, charge: Charge, **overrides: Any) -> Dict[str, Any]:
        stamp = self.timestamp()
        body: Dict[str, Any] = {
            "amount": round(charge.amount, 2),
            "currency": charge.currency,
            "merchantReferenceId": charge.reference,
            "timestamp": stamp,
            "signature": self.sign(amount=charge.amount, currency=charge.currency,
                                   reference=charge.reference, timestamp=stamp),
        }
        if charge.webhook_url:
            body["callbackUrl"] = charge.webhook_url
        if charge.return_url:
            body["returnUrl"] = charge.return_url
        body.update(overrides)
        return body

    def _create_session(self, s: MeasuredSession, timed: bool, charge: Charge,
                        **overrides: Any) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        response = issue("POST /payment-intent/api/v2/direct/session", "POST",
                         self._url("/payment-intent/api/v2/direct/session"),
                         json=self._session_body(charge, **overrides))
        return self._check(s.expect_ok(response, "create session"), "create session")

    @staticmethod
    def _session_id(body: Dict[str, Any]) -> str:
        session = body.get("session") or {}
        session_id = session.get("id") or body.get("sessionId")
        if not session_id:
            raise GatewayError("geidea: session response carried no session id")
        return session_id

    @staticmethod
    def _redirect_url(body: Dict[str, Any]) -> str:
        """The hosted payment page URL. Geidea has used a few field names for it."""
        session = body.get("session") or {}
        for candidate in ("redirectUrl", "redirect_url", "url", "paymentUrl", "checkoutUrl"):
            value = session.get(candidate) or body.get(candidate)
            if value:
                return value
        raise GatewayError(
            "geidea: session response carried no redirect URL. Fields present on "
            f"session: {sorted(session)}. If your account returns the hosted page "
            "under a different key, add it to _redirect_url().")

    @staticmethod
    def _order_id(body: Dict[str, Any]) -> str:
        order = body.get("order") or {}
        order_id = order.get("orderId") or order.get("id") or body.get("orderId")
        if not order_id:
            raise GatewayError("geidea: response carried no orderId")
        return order_id

    @staticmethod
    def _order_outcome(body: Dict[str, Any], gateway_ref: str) -> Outcome:
        order = body.get("order") or {}
        status = str(order.get("status") or body.get("status") or "").lower()
        mapping = {
            "success": "succeeded", "paid": "succeeded", "captured": "succeeded",
            "authorized": "succeeded", "refunded": "succeeded", "voided": "succeeded",
            "inprogress": "pending", "pending": "pending", "initiated": "pending",
        }
        return Outcome(mapping.get(status, "failed" if status else "pending"),
                       f"order status={order.get('status')!r}", gateway_ref, body)

    # -- hosted checkout -------------------------------------------------- #

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        """Call 1 of 2: create the session and hand the browser Geidea's hosted page."""
        body = self._create_session(s, True, charge)
        session_id = self._session_id(body)
        return Started(redirect_url=self._redirect_url(body), gateway_ref=session_id,
                       context={"session_id": session_id, "reference": charge.reference})

    def confirm_hosted(self, s: MeasuredSession, charge: Charge, context: Dict[str, Any],
                       params: Dict[str, str]) -> Outcome:
        """Call 2 of 2: the server-side order fetch that is the authoritative outcome.

        Geidea also POSTs a webhook to callbackUrl. The docs do not state whether the
        webhook or this pull is mandatory, so the pull is treated as authoritative —
        it is the leg a merchant controls and can retry.
        """
        order_id = (params.get("orderId") or params.get("order_id")
                    or context.get("order_id") or context.get("session_id"))
        if not order_id:
            raise GatewayError("geidea: no orderId or sessionId available to confirm against")

        body = self._check(
            s.expect_ok(s.call("GET /pgw/api/v1/direct/order/{orderId}", "GET",
                               self._url(f"/pgw/api/v1/direct/order/{order_id}")),
                        "fetch order", accept=(200,)),
            "fetch order")
        return self._order_outcome(body, order_id)

    # -- direct API ------------------------------------------------------- #

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        """The documented 4-call sequence — measured rather than assumed."""
        session_body = self._create_session(s, True, charge)
        session_id = self._session_id(session_body)

        initiate = self._check(
            s.expect_ok(
                s.call("POST /pgw/api/v6/direct/authenticate/initiate", "POST",
                       self._url("/pgw/api/v6/direct/authenticate/initiate"),
                       json={"sessionId": session_id,
                             "paymentMethod": self._card_payload(card)}),
                "initiate authentication"),
            "initiate authentication")

        if self.force_full_3ds or self._challenge_expected(initiate):
            payer = s.call("POST /pgw/api/v6/direct/authenticate/payer", "POST",
                           self._url("/pgw/api/v6/direct/authenticate/payer"),
                           json={"sessionId": session_id,
                                 "paymentMethod": self._card_payload(card),
                                 "returnUrl": charge.return_url})
            if not payer.ok:
                raise GatewayError(f"geidea: authenticate/payer returned HTTP "
                                   f"{payer.status_code}: {payer.text[:300]}")

        paid = self._check(
            s.expect_ok(
                s.call("POST /pgw/api/v2/direct/pay", "POST",
                       self._url("/pgw/api/v2/direct/pay"),
                       json={"sessionId": session_id,
                             "paymentMethod": self._card_payload(card),
                             "paymentOperation": "Pay"}),
                "pay"),
            "pay")
        return self._order_outcome(paid, self._order_id(paid))

    @staticmethod
    def _challenge_expected(initiate: Dict[str, Any]) -> bool:
        """True unless the initiate response explicitly says no challenge is coming."""
        blob = " ".join(str(value) for value in initiate.values()).lower()
        return not any(marker in blob for marker in FRICTIONLESS_MARKERS)

    # -- stand-alone operations ------------------------------------------- #

    def _authorised_order(self, s: MeasuredSession, charge: Charge, card: Card,
                          payment_operation: str) -> str:
        """Untimed setup: drive a full Direct sequence and return the resulting orderId."""
        session_body = self._create_session(s, False, charge)
        session_id = self._session_id(session_body)

        self._check(
            s.expect_ok(
                s.prep("POST /pgw/api/v6/direct/authenticate/initiate", "POST",
                       self._url("/pgw/api/v6/direct/authenticate/initiate"),
                       json={"sessionId": session_id,
                             "paymentMethod": self._card_payload(card)}),
                "initiate authentication"),
            "initiate authentication")

        s.prep("POST /pgw/api/v6/direct/authenticate/payer", "POST",
               self._url("/pgw/api/v6/direct/authenticate/payer"),
               json={"sessionId": session_id, "paymentMethod": self._card_payload(card),
                     "returnUrl": charge.return_url})

        paid = self._check(
            s.expect_ok(
                s.prep("POST /pgw/api/v2/direct/pay", "POST",
                       self._url("/pgw/api/v2/direct/pay"),
                       json={"sessionId": session_id,
                             "paymentMethod": self._card_payload(card),
                             "paymentOperation": payment_operation}),
                "pay"),
            "pay")
        return self._order_id(paid)

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        order_id = self._authorised_order(s, charge, card, "Authorize")
        body = self._check(
            s.expect_ok(
                s.call("POST /pgw/api/v1/direct/capture", "POST",
                       self._url("/pgw/api/v1/direct/capture"),
                       json={"orderId": order_id,
                             "captureAmount": round(charge.amount, 2)}),
                "capture"),
            "capture")
        return self._order_outcome(body, order_id)

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        order_id = self._authorised_order(s, charge, card, "Pay")
        stamp = self.timestamp()
        body = self._check(
            s.expect_ok(
                s.call("POST /pgw/api/v2/direct/refund", "POST",
                       self._url("/pgw/api/v2/direct/refund"),
                       json={"orderId": order_id,
                             "refundAmount": round(charge.amount, 2),
                             "currency": charge.currency,
                             "timestamp": stamp,
                             "signature": self.sign(amount=charge.amount,
                                                    currency=charge.currency,
                                                    reference=order_id,
                                                    timestamp=stamp)}),
                "refund"),
            "refund")
        return self._order_outcome(body, order_id)

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        order_id = self._authorised_order(s, charge, card, "Authorize")
        body = self._check(
            s.expect_ok(
                s.call("POST /pgw/api/v3/direct/void", "POST",
                       self._url("/pgw/api/v3/direct/void"),
                       json={"orderId": order_id}),
                "void"),
            "void")
        return self._order_outcome(body, order_id)

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        """Fresh session, then pay/token — both timed, because both recur per charge."""
        if not self.token_id:
            raise NotConfigured(
                "GEIDEA_TOKEN_ID is not set. A tokenId is minted only when the original "
                "session carried cardOnFile:true, so it has to be captured out of band "
                "before a recurring charge can be measured.")

        session_body = self._create_session(s, True, charge, cardOnFile=True)
        session_id = self._session_id(session_body)

        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "tokenId": self.token_id,
            "initiatedBy": "Merchant",
            "paymentOperation": "Pay",
        }
        if self.agreement_id:
            payload["agreementId"] = self.agreement_id
            payload["agreementType"] = env("GEIDEA_AGREEMENT_TYPE", "unscheduled")

        body = self._check(
            s.expect_ok(
                s.call("POST /pgw/api/v2/direct/pay/token", "POST",
                       self._url("/pgw/api/v2/direct/pay/token"), json=payload),
                "MIT pay/token"),
            "MIT pay/token")
        return self._order_outcome(body, self._order_id(body) if body.get("order") else session_id)
