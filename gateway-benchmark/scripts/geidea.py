"""
Geidea timing module - the benchmark's baseline.

Docs mapped in docs/02_api_flow_comparison.md (section "Geidea").

Auth: HTTP Basic - Merchant Public Key as the username, API Password as the password.
Bodies: JSON. Regional hosts (same paths, different host):
  * api.merchant.geidea.net    Egypt / global
  * api.ksamerchant.geidea.net KSA
  * api.geidea.ae              UAE
Select with GEIDEA_REGION=eg|ksa|uae, or override wholesale with GEIDEA_API_BASE.

NEEDS SANDBOX CONFIRMATION - read before trusting these numbers
---------------------------------------------------------------
Two request-signing details could not be pinned down from the public docs:

  * **Signature payload order.** Implemented as the documented concatenation
    ``publicKey + amount(2dp) + currency + merchantReferenceId + timestamp``, HMAC-SHA256
    keyed with the API password, base64-encoded. If your sandbox rejects it, the field
    order is the first thing to check.
  * **Timestamp format.** Defaults to ISO-8601 (``2026-08-19T10:30:00``). Some Geidea
    samples show a locale-style stamp instead; override with GEIDEA_TIMESTAMP_FORMAT
    (a strftime string) without touching this file.

The Direct API flow is the headline hypothesis of the whole benchmark: Geidea documents
a *fixed* 4-call sequence (Session -> Initiate Authentication -> Authenticate Payer ->
Pay) with no documented frictionless shortcut, where every competitor documents 1-2 calls
when no 3DS challenge fires. Whether step 3 can be skipped when the initiate response
comes back frictionless is exactly what a sandbox run settles - so this module *detects*
that case and records a 3-call run rather than forcing the 4th call. Set
GEIDEA_FORCE_FULL_3DS=1 to always run all four regardless.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from harness import (Gateway, GatewayError, MeasuredSession, SkipFlow, env,
                     money, new_reference)

REGION_HOSTS = {
    "eg": "https://api.merchant.geidea.net",
    "egypt": "https://api.merchant.geidea.net",
    "global": "https://api.merchant.geidea.net",
    "ksa": "https://api.ksamerchant.geidea.net",
    "sa": "https://api.ksamerchant.geidea.net",
    "uae": "https://api.geidea.ae",
    "ae": "https://api.geidea.ae",
}


class Geidea(Gateway):
    name = "geidea"
    label = "Geidea"

    documented_calls = {
        "hosted_checkout": "2 (create session + confirm order)",
        "direct_api": "4 fixed (session -> initiate -> authenticate payer -> pay)",
        "capture": "1",
        "refund": "1",
        "void": "1",
        "mit": "2 (fresh session + pay/token)",
    }

    notes = [
        "Direct API is documented as a fixed 4-call sequence with no frictionless "
        "shortcut. This module records the actual number of calls the sandbox required, "
        "which is the single most important thing to confirm in a live run.",
        "MIT needs a fresh session object per charge - the only gateway in the set other "
        "than Moyasar that does not charge a stored token in one call.",
        "Signature field order and timestamp format are inferred from the docs and are "
        "the first things to check if the sandbox rejects a request (see module docstring).",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.public_key = env("GEIDEA_PUBLIC_KEY")
        self.api_password = env("GEIDEA_API_PASSWORD")
        region = (env("GEIDEA_REGION", "eg") or "eg").lower()
        self.base = (env("GEIDEA_API_BASE")
                     or REGION_HOSTS.get(region, REGION_HOSTS["eg"])).rstrip("/")
        self.timestamp_format = env("GEIDEA_TIMESTAMP_FORMAT", "%Y-%m-%dT%H:%M:%S")
        self.callback_url = env("GEIDEA_CALLBACK_URL", "https://example.com/geidea/callback")
        self.return_url = env("GEIDEA_RETURN_URL", "https://example.com/geidea/return")
        self.force_full_3ds = (env("GEIDEA_FORCE_FULL_3DS", "0") or "0") not in ("0", "false", "")
        self.token_id = env("GEIDEA_TOKEN_ID")
        self.agreement_id = env("GEIDEA_AGREEMENT_ID")
        self.card = {
            "cardNumber": env("GEIDEA_TEST_CARD_NUMBER", "5123450000000008"),
            "cardholderName": env("GEIDEA_TEST_CARD_HOLDER", "Benchmark Runner"),
            "expiryMonth": int(env("GEIDEA_TEST_CARD_MONTH", "1") or 1),
            "expiryYear": int(env("GEIDEA_TEST_CARD_YEAR", "39") or 39),
            "cvv": env("GEIDEA_TEST_CARD_CVV", "100"),
        }

    # -- config ----------------------------------------------------------- #

    def configured(self) -> bool:
        return bool(self.public_key and self.api_password)

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
        """HMAC-SHA256(apiPassword, publicKey+amount+currency+reference+timestamp), base64."""
        payload = f"{self.public_key}{money(amount)}{currency}{reference}{timestamp}"
        digest = hmac.new((self.api_password or "").encode("utf-8"),
                          payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    # -- helpers ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _session_body(self, reference: str, **overrides: Any) -> Dict[str, Any]:
        stamp = self.timestamp()
        body: Dict[str, Any] = {
            "amount": round(self.amount, 2),
            "currency": self.currency or "EGP",
            "merchantReferenceId": reference,
            "timestamp": stamp,
            "signature": self.sign(amount=self.amount, currency=self.currency or "EGP",
                                   reference=reference, timestamp=stamp),
            "callbackUrl": self.callback_url,
            "returnUrl": self.return_url,
        }
        body.update(overrides)
        return body

    def _create_session(self, s: MeasuredSession, timed: bool, *,
                        reference: str | None = None, **overrides: Any) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        reference = reference or new_reference("geidea")
        response = issue("POST /payment-intent/.../session", "POST",
                         self._url("/payment-intent/api/v2/direct/session"),
                         json=self._session_body(reference, **overrides))
        body = s.expect_ok(response, "create session")
        self._require_success(body, "create session")
        return body

    @staticmethod
    def _require_success(body: Dict[str, Any], what: str) -> None:
        """Geidea reports outcome in responseCode ('000' = success), not only via HTTP status."""
        code = body.get("responseCode")
        if code not in (None, "000"):
            raise GatewayError(f"geidea: {what} responseCode={code!r} "
                               f"message={body.get('responseMessage')!r} "
                               f"detail={body.get('detailedResponseMessage')!r}")

    @staticmethod
    def _session_id(body: Dict[str, Any]) -> str:
        session = body.get("session") or {}
        session_id = session.get("id") or body.get("sessionId")
        if not session_id:
            raise GatewayError("geidea: session response carried no session id")
        return session_id

    @staticmethod
    def _order_id(body: Dict[str, Any]) -> str:
        order = body.get("order") or {}
        order_id = order.get("orderId") or order.get("id") or body.get("orderId")
        if not order_id:
            raise GatewayError("geidea: response carried no orderId")
        return order_id

    def _pay(self, s: MeasuredSession, timed: bool, session_id: str, *,
             payment_operation: str = "Pay") -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        body = {
            "sessionId": session_id,
            "paymentMethod": dict(self.card),
            "paymentOperation": payment_operation,
        }
        response = issue("POST /pgw/.../pay", "POST",
                         self._url("/pgw/api/v2/direct/pay"), json=body)
        parsed = s.expect_ok(response, "pay")
        self._require_success(parsed, "pay")
        return parsed

    def _authorised_order(self, s: MeasuredSession, *, payment_operation: str) -> str:
        """Untimed setup: drive a full Direct API sequence and return the resulting orderId."""
        session = self._create_session(s, timed=False)
        session_id = self._session_id(session)
        initiate = s.expect_ok(
            s.prep("POST /pgw/.../authenticate/initiate", "POST",
                   self._url("/pgw/api/v6/direct/authenticate/initiate"),
                   json={"sessionId": session_id, "paymentMethod": dict(self.card)}),
            "initiate authentication")
        self._require_success(initiate, "initiate authentication")

        s.prep("POST /pgw/.../authenticate/payer", "POST",
               self._url("/pgw/api/v6/direct/authenticate/payer"),
               json={"sessionId": session_id, "paymentMethod": dict(self.card),
                     "returnUrl": self.return_url})

        paid = self._pay(s, timed=False, session_id=session_id,
                         payment_operation=payment_operation)
        return self._order_id(paid)

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        """Create session, then the server-side order fetch that confirms the outcome."""
        session = self._create_session(s, timed=True)
        # The hosted page has not been paid by a browser here, so the order may not exist
        # yet; the session id doubles as the order identifier for the fetch in that case.
        order_id = (session.get("session") or {}).get("id") or self._session_id(session)
        s.call("GET /pgw/.../order/{orderId}", "GET",
               self._url(f"/pgw/api/v1/direct/order/{order_id}"))

    def flow_direct_api(self, s: MeasuredSession) -> None:
        """The documented fixed 4-call sequence - measured, not assumed."""
        session = self._create_session(s, timed=True)
        session_id = self._session_id(session)

        initiate = s.expect_ok(
            s.call("POST /pgw/.../authenticate/initiate", "POST",
                   self._url("/pgw/api/v6/direct/authenticate/initiate"),
                   json={"sessionId": session_id, "paymentMethod": dict(self.card)}),
            "initiate authentication")
        self._require_success(initiate, "initiate authentication")

        # Gap #2 in the comparison doc: can a frictionless result skip Authenticate Payer?
        # If the initiate response already says no challenge is needed, this run answers
        # "yes" with 3 calls; otherwise it runs the documented 4.
        challenge_required = self.force_full_3ds or self._challenge_expected(initiate)
        if challenge_required:
            payer = s.call("POST /pgw/.../authenticate/payer", "POST",
                           self._url("/pgw/api/v6/direct/authenticate/payer"),
                           json={"sessionId": session_id, "paymentMethod": dict(self.card),
                                 "returnUrl": self.return_url})
            if not payer.ok:
                raise GatewayError(
                    f"geidea: authenticate/payer returned HTTP {payer.status_code}: "
                    f"{payer.text[:300]}")

        self._pay(s, timed=True, session_id=session_id)

    @staticmethod
    def _challenge_expected(initiate: Dict[str, Any]) -> bool:
        """Read the initiate response for any signal that a challenge is *not* needed.

        The docs do not specify a frictionless marker, so the default is conservative:
        assume the challenge step is required unless the response explicitly says the
        card is not enrolled / no authentication is required.
        """
        blob = " ".join(str(v) for v in initiate.values()).lower()
        frictionless_markers = ("not enrolled", "frictionless", "authentication_not_required",
                                "notenrolled", "no authentication")
        return not any(marker in blob for marker in frictionless_markers)

    def flow_capture(self, s: MeasuredSession) -> None:
        order_id = self._authorised_order(s, payment_operation="Authorize")
        s.expect_ok(
            s.call("POST /pgw/.../capture", "POST",
                   self._url("/pgw/api/v1/direct/capture"),
                   json={"orderId": order_id, "captureAmount": round(self.amount, 2)}),
            "capture")

    def flow_refund(self, s: MeasuredSession) -> None:
        order_id = self._authorised_order(s, payment_operation="Pay")
        stamp = self.timestamp()
        s.expect_ok(
            s.call("POST /pgw/.../refund", "POST",
                   self._url("/pgw/api/v2/direct/refund"),
                   json={
                       "orderId": order_id,
                       "refundAmount": round(self.amount, 2),
                       "currency": self.currency or "EGP",
                       "timestamp": stamp,
                       "signature": self.sign(amount=self.amount,
                                              currency=self.currency or "EGP",
                                              reference=order_id, timestamp=stamp),
                   }),
            "refund")

    def flow_void(self, s: MeasuredSession) -> None:
        order_id = self._authorised_order(s, payment_operation="Authorize")
        s.expect_ok(
            s.call("POST /pgw/.../void", "POST", self._url("/pgw/api/v3/direct/void"),
                   json={"orderId": order_id}),
            "void")

    def flow_mit(self, s: MeasuredSession) -> None:
        """Fresh session per charge, then pay/token - the 2-call sequence, both timed."""
        if not self.token_id:
            raise SkipFlow(
                "GEIDEA_TOKEN_ID not set - a tokenId is minted only when the original "
                "session carried cardOnFile:true, so it must be captured out of band")

        reference = new_reference("geidea-mit")
        session = self._create_session(s, timed=True, reference=reference,
                                       cardOnFile=True)
        session_id = self._session_id(session)

        body: Dict[str, Any] = {
            "sessionId": session_id,
            "tokenId": self.token_id,
            "initiatedBy": "Merchant",
            "paymentOperation": "Pay",
        }
        if self.agreement_id:
            body["agreementId"] = self.agreement_id
            body["agreementType"] = env("GEIDEA_AGREEMENT_TYPE", "unscheduled")

        response = s.call("POST /pgw/.../pay/token", "POST",
                          self._url("/pgw/api/v2/direct/pay/token"), json=body)
        self._require_success(s.expect_ok(response, "MIT pay/token"), "MIT pay/token")


GATEWAY = Geidea
