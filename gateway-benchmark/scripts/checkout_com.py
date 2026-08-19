"""
Checkout.com timing module.

Docs mapped in docs/02_api_flow_comparison.md (section "Checkout.com").

Auth: ``Authorization: Bearer <secret key>``. Bodies: JSON.
Sandbox host: https://api.sandbox.checkout.com (NAS accounts may have a
subdomain prefix - override with CHECKOUT_API_BASE if the dashboard shows one).

Card data: the standard path requires a pre-tokenized ``source``. Tokenization
happens in the browser (Frames/Flow) with the *public* key, so this module mints
the token with ``s.prep`` - untimed, and excluded from the merchant-server call
count, exactly as the comparison doc scopes it. Raw PAN straight to /payments is
gated behind SAQ D and is not measured.

Capture/refund/void all return 202 Accepted: the number measured here is
time-to-ack, not time-to-settled. Time-to-settled requires the corresponding
webhook, which the merchant does not initiate.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from harness import (Gateway, GatewayError, MeasuredSession, SkipFlow, env,
                     new_reference)

SANDBOX_BASE = "https://api.sandbox.checkout.com"


class CheckoutCom(Gateway):
    name = "checkout_com"
    label = "Checkout.com"

    documented_calls = {
        "hosted_checkout": "2 (create + confirm)",
        "direct_api": "1 frictionless (201), 2 with a challenge (202 + GET)",
        "capture": "1 (async 202)",
        "refund": "1 (async 202)",
        "void": "1 (async 202)",
        "mit": "1",
    }

    notes = [
        "Every stand-alone op returns 202 Accepted. These timings are time-to-ack; "
        "authoritative completion arrives on the payment_captured / payment_refunded / "
        "payment_voided webhook.",
        "Card tokenization runs client-side with the public key and is issued here as an "
        "untimed prep call, so the Direct API count stays comparable with the doc's "
        "merchant-server-only scope.",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.secret_key = env("CHECKOUT_SECRET_KEY")
        self.public_key = env("CHECKOUT_PUBLIC_KEY")
        self.base = (env("CHECKOUT_API_BASE", SANDBOX_BASE) or SANDBOX_BASE).rstrip("/")
        self.processing_channel_id = env("CHECKOUT_PROCESSING_CHANNEL_ID")
        self.success_url = env("CHECKOUT_SUCCESS_URL", "https://example.com/success")
        self.failure_url = env("CHECKOUT_FAILURE_URL", "https://example.com/failure")
        self.card = {
            "number": env("CHECKOUT_TEST_CARD_NUMBER", "4242424242424242"),
            "expiry_month": int(env("CHECKOUT_TEST_CARD_MONTH", "12") or 12),
            "expiry_year": int(env("CHECKOUT_TEST_CARD_YEAR", "2030") or 2030),
            "cvv": env("CHECKOUT_TEST_CARD_CVV", "100"),
        }

    # -- config ----------------------------------------------------------- #

    def configured(self) -> bool:
        return bool(self.secret_key)

    def missing_config(self) -> List[str]:
        return [] if self.secret_key else ["CHECKOUT_SECRET_KEY"]

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {self.secret_key or ''}",
            "Content-Type": "application/json",
        })
        return session

    # -- helpers ---------------------------------------------------------- #

    @property
    def minor_amount(self) -> int:
        return int(round(self.amount * 100))

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _token(self, s: MeasuredSession) -> str:
        """Client-side tokenization, replayed server-side. Untimed by design (see module docstring)."""
        if not self.public_key:
            raise SkipFlow("CHECKOUT_PUBLIC_KEY not set - needed to mint a card token")
        response = s.prep("POST /tokens (client-side)", "POST", self._url("/tokens"),
                          json={"type": "card", **self.card},
                          headers={"Authorization": self.public_key})
        return s.expect_ok(response, "create token")["token"]

    def _payment_body(self, source: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "source": source,
            "amount": self.minor_amount,
            "currency": self.currency or "GBP",
            "reference": new_reference("cko"),
            "success_url": self.success_url,
            "failure_url": self.failure_url,
        }
        if self.processing_channel_id:
            body["processing_channel_id"] = self.processing_channel_id
        body.update(overrides)
        return body

    def _pay(self, s: MeasuredSession, timed: bool, *, capture: bool = True,
             source: Dict[str, Any] | None = None, **overrides: Any) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        if source is None:
            source = {"type": "token", "token": self._token(s)}
        body = self._payment_body(source, capture=capture, **overrides)
        response = issue("POST /payments", "POST", self._url("/payments"), json=body)
        return s.expect_ok(response, "POST /payments", accept=(201, 202))

    @staticmethod
    def _payment_id(body: Dict[str, Any]) -> str:
        payment_id = body.get("id")
        if not payment_id:
            raise GatewayError("checkout_com: payment response carried no id")
        return payment_id

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        """POST /hosted-payments then the mandatory server-side confirmation GET."""
        body = {
            "amount": self.minor_amount,
            "currency": self.currency or "GBP",
            "reference": new_reference("cko-hpp"),
            "billing": {"address": {"country": env("CHECKOUT_COUNTRY", "GB")}},
            "success_url": self.success_url,
            "failure_url": self.failure_url,
            "cancel_url": env("CHECKOUT_CANCEL_URL", "https://example.com/cancel"),
        }
        if self.processing_channel_id:
            body["processing_channel_id"] = self.processing_channel_id

        created = s.expect_ok(
            s.call("POST /hosted-payments", "POST", self._url("/hosted-payments"), json=body),
            "create hosted payment", accept=(201, 202))
        hosted_id = self._payment_id(created)

        # The docs explicitly warn against trusting the front-end redirect alone, which
        # makes this second signal mandatory in substance even though it is a GET.
        s.expect_ok(
            s.call("GET /hosted-payments/{id}", "GET",
                   self._url(f"/hosted-payments/{hosted_id}")),
            "get hosted payment", accept=(200,))

    def flow_direct_api(self, s: MeasuredSession) -> None:
        """POST /payments with a tokenized source; GET only if the 202/Pending branch fires."""
        body = self._pay(s, timed=True)
        status = body.get("status")
        if status == "Pending" or body.get("_links", {}).get("redirect"):
            # 3DS branch: the outcome is only knowable from a follow-up read.
            s.expect_ok(
                s.call("GET /payments/{id}", "GET",
                       self._url(f"/payments/{self._payment_id(body)}")),
                "get payment", accept=(200,))
        elif status not in ("Authorized", "Captured", "Card Verified"):
            raise GatewayError(f"checkout_com: /payments returned status={status!r} "
                               f"response_summary={body.get('response_summary')!r}")

    def flow_capture(self, s: MeasuredSession) -> None:
        payment = self._pay(s, timed=False, capture=False)
        s.expect_ok(
            s.call("POST /payments/{id}/captures", "POST",
                   self._url(f"/payments/{self._payment_id(payment)}/captures"),
                   json={"amount": self.minor_amount, "reference": new_reference("cko-cap")}),
            "capture", accept=(202,))

    def flow_refund(self, s: MeasuredSession) -> None:
        payment = self._pay(s, timed=False, capture=True)
        s.expect_ok(
            s.call("POST /payments/{id}/refunds", "POST",
                   self._url(f"/payments/{self._payment_id(payment)}/refunds"),
                   json={"amount": self.minor_amount, "reference": new_reference("cko-ref")}),
            "refund", accept=(202,))

    def flow_void(self, s: MeasuredSession) -> None:
        payment = self._pay(s, timed=False, capture=False)
        s.expect_ok(
            s.call("POST /payments/{id}/voids", "POST",
                   self._url(f"/payments/{self._payment_id(payment)}/voids"),
                   json={"reference": new_reference("cko-void")}),
            "void", accept=(202,))

    def flow_mit(self, s: MeasuredSession) -> None:
        """Single /payments call against the stored payment id, merchant_initiated=true."""
        original = self._pay(s, timed=False, capture=True)
        source_id = (original.get("source") or {}).get("id")
        if not source_id:
            raise SkipFlow(
                "no source.id on the initial payment - the account may not have card "
                "storage enabled, so an unscheduled MIT charge cannot be timed")

        body = self._payment_body(
            {"type": "id", "id": source_id},
            payment_type="Unscheduled",
            merchant_initiated=True,
            previous_payment_id=self._payment_id(original),
        )
        response = s.call("POST /payments (MIT)", "POST", self._url("/payments"), json=body)
        result = s.expect_ok(response, "MIT payment", accept=(201, 202))
        if result.get("status") not in ("Authorized", "Captured", "Pending"):
            raise GatewayError(f"checkout_com: MIT charge status={result.get('status')!r}")


GATEWAY = CheckoutCom
