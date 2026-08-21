"""
Checkout.com adapter.

Auth: Bearer secret key. Bodies: JSON. Sandbox: https://api.sandbox.checkout.com
(NAS accounts may carry a subdomain prefix — set CHECKOUT_API_BASE from the dashboard).

Card tokenization runs client-side with the public key in a real integration, so it
is issued here as an untimed prep call and stays out of the merchant-server count.

Capture/refund/void all return 202 Accepted: what is measured is time-to-ack, not
time-to-settled. The authoritative outcome arrives on a webhook.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from app.adapters.base import Adapter, Card, Charge, Outcome, Started
from app.config import env
from app.timing import GatewayError, MeasuredSession, NotConfigured, minor_units


class CheckoutAdapter(Adapter):
    name = "checkout_com"
    label = "Checkout.com"

    documented_calls = {
        "hosted_checkout": "2 (create + confirm)",
        "direct_api": "1 frictionless (201), 2 with a challenge (202 + GET)",
        "capture": "1 (async 202)", "refund": "1 (async 202)", "void": "1 (async 202)",
        "mit": "1",
    }

    notes = [
        "Capture, refund and void return 202 Accepted. These timings are time-to-ack; "
        "completion arrives on the payment_captured / payment_refunded / payment_voided "
        "webhook.",
        "Card tokenization is client-side and issued as untimed prep, keeping the "
        "merchant-server call count comparable with the other gateways.",
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        self.secret_key = env("CHECKOUT_SECRET_KEY")
        self.public_key = env("CHECKOUT_PUBLIC_KEY")
        self.base = (env("CHECKOUT_API_BASE", "https://api.sandbox.checkout.com")
                     or "https://api.sandbox.checkout.com").rstrip("/")
        self.processing_channel_id = env("CHECKOUT_PROCESSING_CHANNEL_ID")
        self.country = env("CHECKOUT_COUNTRY", "GB")

    def missing_config(self) -> List[str]:
        return [] if self.secret_key else ["CHECKOUT_SECRET_KEY"]

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {self.secret_key or ''}",
                                "Content-Type": "application/json"})
        return session

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _token(self, s: MeasuredSession, card: Card) -> str:
        if not self.public_key:
            raise NotConfigured("CHECKOUT_PUBLIC_KEY is needed to mint a card token.")
        response = s.prep("POST /tokens (client-side)", "POST", self._url("/tokens"),
                          json={"type": "card", "number": card.number,
                                "expiry_month": int(card.month),
                                "expiry_year": int(card.year), "cvv": card.cvc},
                          headers={"Authorization": self.public_key})
        return s.expect_ok(response, "create token")["token"]

    def _body(self, charge: Charge, source: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "source": source,
            "amount": minor_units(charge.amount),
            "currency": charge.currency,
            "reference": charge.reference,
            "success_url": charge.return_url,
            "failure_url": charge.return_url,
        }
        if self.processing_channel_id:
            body["processing_channel_id"] = self.processing_channel_id
        body.update(extra)
        return body

    def _pay(self, s: MeasuredSession, timed: bool, charge: Charge, card: Card,
             capture: bool = True, source: Dict[str, Any] | None = None,
             **extra: Any) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        if source is None:
            source = {"type": "token", "token": self._token(s, card)}
        return s.expect_ok(
            issue("POST /payments", "POST", self._url("/payments"),
                  json=self._body(charge, source, capture=capture, **extra)),
            "create payment", accept=(201, 202))

    @staticmethod
    def _id(body: Dict[str, Any]) -> str:
        value = body.get("id")
        if not value:
            raise GatewayError("checkout_com: response carried no id")
        return value

    @staticmethod
    def _outcome(body: Dict[str, Any]) -> Outcome:
        status = str(body.get("status") or "")
        mapping = {"Authorized": "succeeded", "Captured": "succeeded",
                   "Card Verified": "succeeded", "Paid": "succeeded",
                   "Pending": "pending"}
        return Outcome(mapping.get(status, "succeeded" if body.get("action_id") else "failed"),
                       f"status={status!r} summary={body.get('response_summary')!r}",
                       body.get("id") or body.get("action_id"), body)

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        body: Dict[str, Any] = {
            "amount": minor_units(charge.amount), "currency": charge.currency,
            "reference": charge.reference,
            "billing": {"address": {"country": self.country}},
            "success_url": charge.return_url, "failure_url": charge.return_url,
            "cancel_url": charge.return_url,
        }
        if self.processing_channel_id:
            body["processing_channel_id"] = self.processing_channel_id
        created = s.expect_ok(
            s.call("POST /hosted-payments", "POST", self._url("/hosted-payments"), json=body),
            "create hosted payment", accept=(201, 202))
        link = ((created.get("_links") or {}).get("redirect") or {}).get("href")
        if not link:
            raise GatewayError("checkout_com: hosted payment response carried no redirect link")
        return Started(redirect_url=link, gateway_ref=self._id(created),
                       context={"hosted_id": self._id(created)})

    def confirm_hosted(self, s: MeasuredSession, charge: Charge, context: Dict[str, Any],
                       params: Dict[str, str]) -> Outcome:
        hosted_id = params.get("cko-payment-id") or context.get("hosted_id")
        if not hosted_id:
            raise GatewayError("checkout_com: no hosted payment id to confirm against")
        body = s.expect_ok(
            s.call("GET /hosted-payments/{id}", "GET",
                   self._url(f"/hosted-payments/{hosted_id}")),
            "get hosted payment", accept=(200,))
        return self._outcome(body)

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        body = self._pay(s, True, charge, card)
        if body.get("status") == "Pending" or (body.get("_links") or {}).get("redirect"):
            body = s.expect_ok(
                s.call("GET /payments/{id}", "GET", self._url(f"/payments/{self._id(body)}")),
                "get payment", accept=(200,))
        return self._outcome(body)

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment = self._pay(s, False, charge, card, capture=False)
        body = s.expect_ok(
            s.call("POST /payments/{id}/captures", "POST",
                   self._url(f"/payments/{self._id(payment)}/captures"),
                   json={"amount": minor_units(charge.amount),
                         "reference": charge.reference}),
            "capture", accept=(202,))
        return Outcome("succeeded", "202 Accepted — completion confirmed by webhook",
                       body.get("action_id"), body)

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment = self._pay(s, False, charge, card, capture=True)
        body = s.expect_ok(
            s.call("POST /payments/{id}/refunds", "POST",
                   self._url(f"/payments/{self._id(payment)}/refunds"),
                   json={"amount": minor_units(charge.amount),
                         "reference": charge.reference}),
            "refund", accept=(202,))
        return Outcome("succeeded", "202 Accepted — completion confirmed by webhook",
                       body.get("action_id"), body)

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment = self._pay(s, False, charge, card, capture=False)
        body = s.expect_ok(
            s.call("POST /payments/{id}/voids", "POST",
                   self._url(f"/payments/{self._id(payment)}/voids"),
                   json={"reference": charge.reference}),
            "void", accept=(202,))
        return Outcome("succeeded", "202 Accepted — completion confirmed by webhook",
                       body.get("action_id"), body)

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        original = self._pay(s, False, charge, card, capture=True)
        source_id = (original.get("source") or {}).get("id")
        if not source_id:
            raise NotConfigured(
                "The initial payment returned no source.id, so there is no stored card "
                "to charge. The account may not have card storage enabled.")
        body = s.expect_ok(
            s.call("POST /payments (MIT)", "POST", self._url("/payments"),
                   json=self._body(charge, {"type": "id", "id": source_id},
                                   payment_type="Unscheduled", merchant_initiated=True,
                                   previous_payment_id=self._id(original))),
            "MIT payment", accept=(201, 202))
        return self._outcome(body)
