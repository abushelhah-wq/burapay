"""
Adyen timing module.

Docs mapped in docs/02_api_flow_comparison.md (section "Adyen").

Auth: ``x-API-key`` header. Bodies: JSON.
Test host: https://checkout-test.adyen.com, versioned path (/v71, /v72...).

Two integration paths are measured separately, matching Adyen's own split:
  * Sessions flow  -> Hosted Checkout (1 outbound call, webhook-confirmed)
  * Advanced flow  -> Direct API (/paymentMethods then /payments, +details on a challenge)

Card data: Adyen's test environment accepts raw card fields in ``paymentMethod`` for
API-only integrations, which is what this module sends. Adyen's *recommended*
integration encrypts card data client-side, so treat this as the raw-PCI-scope path -
the same path the comparison doc flags as not call-count-verified against the
client-encrypted one.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from harness import (Gateway, GatewayError, MeasuredSession, SkipFlow, env,
                     env_int, new_reference)

TEST_BASE = "https://checkout-test.adyen.com"


class Adyen(Gateway):
    name = "adyen"
    label = "Adyen"

    documented_calls = {
        "hosted_checkout": "1 out + 1 webhook",
        "direct_api": "2 frictionless, 3 with a challenge",
        "capture": "1",
        "refund": "1",
        "void": "1",
        "mit": "1",
    }

    notes = [
        "No pull-based status endpoint is documented for the Sessions flow - the "
        "AUTHORISATION webhook is the only documented confirmation path, so the hosted "
        "figure here is 1 timed call and time-to-authoritative-status is not merchant-timed.",
        "Capture/refund/cancel return 'received' - the final outcome arrives by webhook, "
        "so these timings are time-to-ack, not time-to-settled.",
        "The card-on-file token is delivered asynchronously via the "
        "recurring.token.created webhook, so MIT needs ADYEN_STORED_PAYMENT_METHOD_ID "
        "to be captured out of band before it can be timed.",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.api_key = env("ADYEN_API_KEY")
        self.merchant_account = env("ADYEN_MERCHANT_ACCOUNT")
        self.base = (env("ADYEN_API_BASE", TEST_BASE) or TEST_BASE).rstrip("/")
        self.version = env("ADYEN_API_VERSION", "v71")
        self.return_url = env("ADYEN_RETURN_URL", "https://example.com/adyen/return")
        self.stored_payment_method_id = env("ADYEN_STORED_PAYMENT_METHOD_ID")
        self.shopper_reference = env("ADYEN_SHOPPER_REFERENCE", "gateway-benchmark-shopper")
        self.card = {
            "type": "scheme",
            "number": env("ADYEN_TEST_CARD_NUMBER", "4111111111111111"),
            "expiryMonth": env("ADYEN_TEST_CARD_MONTH", "03"),
            "expiryYear": env("ADYEN_TEST_CARD_YEAR", "2030"),
            "cvc": env("ADYEN_TEST_CARD_CVC", "737"),
            "holderName": env("ADYEN_TEST_CARD_HOLDER", "Benchmark Runner"),
        }

    # -- config ----------------------------------------------------------- #

    def configured(self) -> bool:
        return bool(self.api_key and self.merchant_account)

    def missing_config(self) -> List[str]:
        missing = []
        if not self.api_key:
            missing.append("ADYEN_API_KEY")
        if not self.merchant_account:
            missing.append("ADYEN_MERCHANT_ACCOUNT")
        return missing

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "x-API-key": self.api_key or "",
            "Content-Type": "application/json",
        })
        return session

    # -- helpers ---------------------------------------------------------- #

    @property
    def minor_amount(self) -> int:
        return int(round(self.amount * 100))

    def _url(self, path: str) -> str:
        return f"{self.base}/{self.version}{path}"

    def _amount(self) -> Dict[str, Any]:
        return {"value": self.minor_amount, "currency": self.currency or "EUR"}

    def _payment_body(self, reference: str, **overrides: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "merchantAccount": self.merchant_account,
            "amount": self._amount(),
            "reference": reference,
            "paymentMethod": dict(self.card),
            "returnUrl": self.return_url,
        }
        body.update(overrides)
        return body

    def _authorise(self, s: MeasuredSession, timed: bool, *, capture_delay: str | None = None
                   ) -> Dict[str, Any]:
        """One /payments authorisation. ``capture_delay='manual'`` leaves it uncaptured."""
        issue = s.call if timed else s.prep
        overrides: Dict[str, Any] = {}
        if capture_delay:
            overrides["captureDelayHours"] = -1 if capture_delay == "manual" else 0
        response = issue("POST /payments", "POST", self._url("/payments"),
                         json=self._payment_body(new_reference("adyen"), **overrides))
        body = s.expect_ok(response, "POST /payments")
        result = body.get("resultCode")
        if result not in ("Authorised", "Received", "Pending"):
            raise GatewayError(f"adyen: /payments returned resultCode={result!r} "
                               f"refusal={body.get('refusalReason')!r}")
        return body

    @staticmethod
    def _psp_reference(body: Dict[str, Any]) -> str:
        psp = body.get("pspReference")
        if not psp:
            raise GatewayError("adyen: response carried no pspReference")
        return psp

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        """Sessions flow: a single POST /sessions. Confirmation is webhook-only."""
        s.expect_ok(
            s.call("POST /sessions", "POST", self._url("/sessions"),
                   json={
                       "merchantAccount": self.merchant_account,
                       "amount": self._amount(),
                       "reference": new_reference("adyen-hpp"),
                       "returnUrl": self.return_url,
                       "countryCode": env("ADYEN_COUNTRY_CODE", "NL"),
                   }),
            "POST /sessions")

    def flow_direct_api(self, s: MeasuredSession) -> None:
        """Advanced flow: /paymentMethods, /payments, and /payments/details on a challenge."""
        s.expect_ok(
            s.call("POST /paymentMethods", "POST", self._url("/paymentMethods"),
                   json={
                       "merchantAccount": self.merchant_account,
                       "amount": self._amount(),
                       "countryCode": env("ADYEN_COUNTRY_CODE", "NL"),
                       "channel": "Web",
                   }),
            "POST /paymentMethods")

        body = self._authorise(s, timed=True)

        action = body.get("action")
        if action:
            # Conditional third call. In sandbox with a frictionless test card this should
            # not fire; when it does, the extra call is measured so the 2-vs-3 split shows
            # up in the per-call sample counts rather than being hidden.
            payload = {"details": {"redirectResult": action.get("paymentData", "")}}
            s.call("POST /payments/details", "POST", self._url("/payments/details"),
                   json=payload)

    def flow_capture(self, s: MeasuredSession) -> None:
        psp = self._psp_reference(self._authorise(s, timed=False, capture_delay="manual"))
        s.expect_ok(
            s.call("POST /payments/{psp}/captures", "POST",
                   self._url(f"/payments/{psp}/captures"),
                   json={"merchantAccount": self.merchant_account,
                         "amount": self._amount(),
                         "reference": new_reference("adyen-cap")}),
            "capture")

    def flow_refund(self, s: MeasuredSession) -> None:
        # Adyen requires the payment to be captured first. With the account's default
        # capture delay the capture is asynchronous, so the refund may be accepted only
        # after settlement; a refund failure here usually means capture had not landed yet.
        psp = self._psp_reference(self._authorise(s, timed=False))
        s.expect_ok(
            s.call("POST /payments/{psp}/refunds", "POST",
                   self._url(f"/payments/{psp}/refunds"),
                   json={"merchantAccount": self.merchant_account,
                         "amount": self._amount(),
                         "reference": new_reference("adyen-ref")}),
            "refund")

    def flow_void(self, s: MeasuredSession) -> None:
        psp = self._psp_reference(self._authorise(s, timed=False, capture_delay="manual"))
        s.expect_ok(
            s.call("POST /payments/{psp}/cancels", "POST",
                   self._url(f"/payments/{psp}/cancels"),
                   json={"merchantAccount": self.merchant_account,
                         "reference": new_reference("adyen-void")}),
            "cancel")

    def flow_mit(self, s: MeasuredSession) -> None:
        """Single /payments call against a stored payment method, shopperInteraction=ContAuth."""
        if not self.stored_payment_method_id:
            raise SkipFlow(
                "ADYEN_STORED_PAYMENT_METHOD_ID not set - Adyen delivers the token via the "
                "recurring.token.created webhook, so it cannot be minted inline")

        body = self._payment_body(
            new_reference("adyen-mit"),
            paymentMethod={"type": "scheme",
                           "storedPaymentMethodId": self.stored_payment_method_id},
            shopperReference=self.shopper_reference,
            shopperInteraction="ContAuth",
            recurringProcessingModel="UnscheduledCardOnFile",
        )
        response = s.call("POST /payments (ContAuth)", "POST", self._url("/payments"),
                          json=body)
        result = s.expect_ok(response, "MIT payment").get("resultCode")
        if result not in ("Authorised", "Received"):
            raise GatewayError(f"adyen: MIT charge returned resultCode={result!r}")


GATEWAY = Adyen
