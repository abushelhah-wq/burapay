"""
Adyen adapter.

Auth: ``x-API-key`` header. Bodies: JSON. Test host: https://checkout-test.adyen.com,
with a versioned path (/v71, /v72...).

Hosted checkout uses the Sessions flow, which is a single outbound call. Adyen
documents no pull-based status endpoint for it — confirmation is the AUTHORISATION
webhook — so ``confirm_hosted`` reports what the redirect carried rather than
pretending to have fetched an authoritative status.

Direct uses the Advanced flow. Raw card fields are accepted in the test
environment for API-only integrations, which is what this sends; Adyen's
recommended integration encrypts card data client-side.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from app.adapters.base import Adapter, Card, Charge, Outcome, Started
from app.config import env
from app.timing import GatewayError, MeasuredSession, NotConfigured, minor_units


class AdyenAdapter(Adapter):
    name = "adyen"
    label = "Adyen"

    documented_calls = {
        "hosted_checkout": "1 outbound + 1 webhook",
        "direct_api": "2 frictionless, 3 with a challenge",
        "capture": "1", "refund": "1", "void": "1", "mit": "1",
    }

    notes = [
        "The Sessions flow has no documented pull-based status endpoint — the "
        "AUTHORISATION webhook is the only documented confirmation path, so "
        "time-to-authoritative-status is not something the merchant can time.",
        "Capture, refund and cancel return 'received'; the final outcome arrives by "
        "webhook, so these are time-to-ack.",
        "The card-on-file token arrives on the recurring.token.created webhook, so "
        "MIT needs ADYEN_STORED_PAYMENT_METHOD_ID captured out of band.",
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        self.api_key = env("ADYEN_API_KEY")
        self.merchant_account = env("ADYEN_MERCHANT_ACCOUNT")
        self.base = (env("ADYEN_API_BASE", "https://checkout-test.adyen.com")
                     or "https://checkout-test.adyen.com").rstrip("/")
        self.version = env("ADYEN_API_VERSION", "v71")
        self.country = env("ADYEN_COUNTRY_CODE", "NL")
        self.stored_payment_method_id = env("ADYEN_STORED_PAYMENT_METHOD_ID")
        self.shopper_reference = env("ADYEN_SHOPPER_REFERENCE", "busrapay-shopper")

    def missing_config(self) -> List[str]:
        missing = []
        if not self.api_key:
            missing.append("ADYEN_API_KEY")
        if not self.merchant_account:
            missing.append("ADYEN_MERCHANT_ACCOUNT")
        return missing

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"x-API-key": self.api_key or "",
                                "Content-Type": "application/json"})
        return session

    def _url(self, path: str) -> str:
        return f"{self.base}/{self.version}{path}"

    def _amount(self, charge: Charge) -> Dict[str, Any]:
        return {"value": minor_units(charge.amount), "currency": charge.currency}

    def _card(self, card: Card) -> Dict[str, str]:
        return {"type": "scheme", "number": card.number, "expiryMonth": card.month,
                "expiryYear": card.year, "cvc": card.cvc, "holderName": card.holder}

    def _payment_body(self, charge: Charge, card: Card, **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "merchantAccount": self.merchant_account,
            "amount": self._amount(charge),
            "reference": charge.reference,
            "paymentMethod": self._card(card),
            "returnUrl": charge.return_url,
        }
        body.update(extra)
        return body

    def _authorise(self, s: MeasuredSession, timed: bool, charge: Charge, card: Card,
                   **extra: Any) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        body = s.expect_ok(
            issue("POST /payments", "POST", self._url("/payments"),
                  json=self._payment_body(charge, card, **extra)), "POST /payments")
        result = body.get("resultCode")
        if result not in ("Authorised", "Received", "Pending", "RedirectShopper"):
            raise GatewayError(f"adyen: /payments resultCode={result!r} "
                               f"refusal={body.get('refusalReason')!r}")
        return body

    @staticmethod
    def _psp(body: Dict[str, Any]) -> str:
        psp = body.get("pspReference")
        if not psp:
            raise GatewayError("adyen: response carried no pspReference")
        return psp

    @staticmethod
    def _outcome(body: Dict[str, Any]) -> Outcome:
        result = body.get("resultCode") or body.get("status")
        mapping = {"Authorised": "succeeded", "Received": "succeeded",
                   "received": "succeeded", "Pending": "pending",
                   "RedirectShopper": "pending"}
        return Outcome(mapping.get(result, "failed"),
                       f"resultCode={result!r}" +
                       (f" refusal={body.get('refusalReason')!r}"
                        if body.get("refusalReason") else ""),
                       body.get("pspReference"), body)

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        body = s.expect_ok(
            s.call("POST /sessions", "POST", self._url("/sessions"),
                   json={"merchantAccount": self.merchant_account,
                         "amount": self._amount(charge),
                         "reference": charge.reference,
                         "returnUrl": charge.return_url,
                         "countryCode": self.country}),
            "POST /sessions", accept=(200, 201))
        session_id = body.get("id")
        if not session_id:
            raise GatewayError("adyen: session response carried no id")
        # Adyen's Sessions flow is Drop-in driven: the browser mounts their SDK with
        # this session rather than being 303'd to a hosted URL. The app renders a small
        # page that does exactly that, so the redirect target is our own handoff route.
        return Started(redirect_url=f"/handoff/adyen/{session_id}",
                       gateway_ref=session_id,
                       context={"session_id": session_id,
                                "session_data": body.get("sessionData", "")})

    def confirm_hosted(self, s: MeasuredSession, charge: Charge, context: Dict[str, Any],
                       params: Dict[str, str]) -> Outcome:
        # Adyen documents no pull-based status lookup for the Sessions flow, so there is
        # deliberately no timed call here: inventing a GET would misrepresent the flow.
        result = params.get("sessionResult") or params.get("redirectResult")
        if not result:
            return Outcome("pending",
                           "Adyen confirms the Sessions flow by AUTHORISATION webhook; "
                           "no pull-based status endpoint is documented, so the outcome "
                           "is not knowable from a merchant-initiated call.",
                           context.get("session_id"), {})
        return Outcome("pending",
                       f"redirect carried sessionResult; authoritative status still "
                       f"arrives by webhook", context.get("session_id"), {})

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        s.expect_ok(
            s.call("POST /paymentMethods", "POST", self._url("/paymentMethods"),
                   json={"merchantAccount": self.merchant_account,
                         "amount": self._amount(charge),
                         "countryCode": self.country, "channel": "Web"}),
            "POST /paymentMethods")
        body = self._authorise(s, True, charge, card)
        if body.get("action"):
            s.call("POST /payments/details", "POST", self._url("/payments/details"),
                   json={"details": {"redirectResult":
                                     body.get("action", {}).get("paymentData", "")}})
        return self._outcome(body)

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        psp = self._psp(self._authorise(s, False, charge, card, captureDelayHours=-1))
        body = s.expect_ok(
            s.call("POST /payments/{psp}/captures", "POST",
                   self._url(f"/payments/{psp}/captures"),
                   json={"merchantAccount": self.merchant_account,
                         "amount": self._amount(charge), "reference": charge.reference}),
            "capture", accept=(200, 201))
        return self._outcome(body)

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        psp = self._psp(self._authorise(s, False, charge, card))
        body = s.expect_ok(
            s.call("POST /payments/{psp}/refunds", "POST",
                   self._url(f"/payments/{psp}/refunds"),
                   json={"merchantAccount": self.merchant_account,
                         "amount": self._amount(charge), "reference": charge.reference}),
            "refund", accept=(200, 201))
        return self._outcome(body)

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        psp = self._psp(self._authorise(s, False, charge, card, captureDelayHours=-1))
        body = s.expect_ok(
            s.call("POST /payments/{psp}/cancels", "POST",
                   self._url(f"/payments/{psp}/cancels"),
                   json={"merchantAccount": self.merchant_account,
                         "reference": charge.reference}),
            "cancel", accept=(200, 201))
        return self._outcome(body)

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        if not self.stored_payment_method_id:
            raise NotConfigured(
                "ADYEN_STORED_PAYMENT_METHOD_ID is not set. Adyen delivers the "
                "card-on-file token on the recurring.token.created webhook rather than "
                "in the store-card response, so it cannot be minted inline.")
        body = s.expect_ok(
            s.call("POST /payments (ContAuth)", "POST", self._url("/payments"),
                   json=self._payment_body(
                       charge, card,
                       paymentMethod={"type": "scheme",
                                      "storedPaymentMethodId": self.stored_payment_method_id},
                       shopperReference=self.shopper_reference,
                       shopperInteraction="ContAuth",
                       recurringProcessingModel="UnscheduledCardOnFile")),
            "MIT payment")
        return self._outcome(body)
