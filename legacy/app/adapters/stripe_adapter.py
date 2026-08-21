"""
Stripe adapter — the reference implementation other adapters follow.

Auth: HTTP Basic, secret key as username, empty password.
Bodies: form-encoded. Stripe does not accept JSON request bodies.

Card handling: this adapter charges Stripe's test *tokens* rather than raw PANs.
Stripe blocks raw card numbers on /v1/payment_methods for accounts without PCI
enablement, so a raw-PAN path would fail on most sandboxes. Tokenization is a
browser-side step in a real integration, so it is issued as an untimed prep call.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from app.adapters.base import Adapter, Card, Charge, Outcome, Started
from app.config import env
from app.timing import GatewayError, MeasuredSession, minor_units, new_reference


class StripeAdapter(Adapter):
    name = "stripe"
    label = "Stripe"

    documented_calls = {
        "hosted_checkout": "1 outbound + 1 webhook (+1 optional GET)",
        "direct_api": "1–2 (create+confirm; +1 to mint a PaymentMethod)",
        "capture": "1", "refund": "1", "void": "1", "mit": "1",
    }

    notes = [
        "Stripe's own quickstart steers integrators toward Checkout Sessions rather "
        "than raw PaymentIntents, so the 1-call Direct minimum is a real capability "
        "but not the recommended path.",
        "Fulfillment is webhook-mandatory per Stripe's docs. The GET measured on "
        "confirmation is the defensive re-check Stripe's own sample code performs.",
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        self.secret_key = env("STRIPE_SECRET_KEY")
        self.base = (env("STRIPE_API_BASE", "https://api.stripe.com")
                     or "https://api.stripe.com").rstrip("/")
        self.test_token = env("STRIPE_TEST_TOKEN", "tok_visa")
        self.api_version = env("STRIPE_API_VERSION")

    # -- configuration ---------------------------------------------------- #

    def missing_config(self) -> List[str]:
        return [] if self.secret_key else ["STRIPE_SECRET_KEY"]

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (self.secret_key or "", "")
        if self.api_version:
            session.headers["Stripe-Version"] = self.api_version
        return session

    # -- helpers ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _currency(self, charge: Charge) -> str:
        return (charge.currency or "usd").lower()

    def _payment_method(self, s: MeasuredSession, timed: bool) -> str:
        issue = s.call if timed else s.prep
        response = issue("create PaymentMethod", "POST", self._url("/v1/payment_methods"),
                         data={"type": "card", "card[token]": self.test_token})
        return s.expect_ok(response, "create PaymentMethod")["id"]

    def _pay(self, s: MeasuredSession, timed: bool, charge: Charge, payment_method: str,
             capture_method: str = "automatic", extra: Dict[str, Any] | None = None
             ) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        data = {
            "amount": str(minor_units(charge.amount)),
            "currency": self._currency(charge),
            "payment_method": payment_method,
            "confirm": "true",
            "capture_method": capture_method,
            # Card-only, no redirect methods: keeps the sandbox path frictionless and
            # comparable with the other gateways' non-3DS flows.
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
            "description": charge.reference,
        }
        if extra:
            data.update(extra)
        response = issue("create+confirm PaymentIntent", "POST",
                         self._url("/v1/payment_intents"), data=data)
        return s.expect_ok(response, "create+confirm PaymentIntent")

    # -- hosted checkout -------------------------------------------------- #

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        created = s.expect_ok(
            s.call("POST /v1/checkout/sessions", "POST",
                   self._url("/v1/checkout/sessions"),
                   data={
                       "mode": "payment",
                       # Stripe substitutes the real id into this placeholder on redirect.
                       "success_url": f"{charge.return_url}&session_id={{CHECKOUT_SESSION_ID}}"
                                      if "?" in charge.return_url
                                      else f"{charge.return_url}?session_id="
                                           f"{{CHECKOUT_SESSION_ID}}",
                       "cancel_url": charge.return_url,
                       "client_reference_id": charge.reference,
                       "line_items[0][price_data][currency]": self._currency(charge),
                       "line_items[0][price_data][product_data][name]": "Benchmark item",
                       "line_items[0][price_data][unit_amount]": str(minor_units(charge.amount)),
                       "line_items[0][quantity]": "1",
                   }),
            "create Checkout Session")

        session_id, url = created.get("id"), created.get("url")
        if not session_id or not url:
            raise GatewayError("stripe: Checkout Session response had no id/url")
        return Started(redirect_url=url, gateway_ref=session_id,
                       context={"session_id": session_id})

    def confirm_hosted(self, s: MeasuredSession, charge: Charge, context: Dict[str, Any],
                       params: Dict[str, str]) -> Outcome:
        session_id = params.get("session_id") or context.get("session_id")
        if not session_id:
            raise GatewayError("stripe: no session_id to confirm against")

        body = s.expect_ok(
            s.call("GET /v1/checkout/sessions/{id}", "GET",
                   self._url(f"/v1/checkout/sessions/{session_id}")),
            "retrieve Checkout Session", accept=(200,))

        payment_status = body.get("payment_status")
        status = {"paid": "succeeded", "no_payment_required": "succeeded"}.get(
            payment_status, "pending" if body.get("status") == "open" else "failed")
        return Outcome(status=status,
                       detail=f"payment_status={payment_status}, status={body.get('status')}",
                       gateway_ref=session_id, raw=body)

    # -- server-side flows ------------------------------------------------ #

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment_method = self._payment_method(s, timed=True)
        intent = self._pay(s, True, charge, payment_method)
        status = intent.get("status")
        if status == "requires_action":
            return Outcome("pending", "3DS challenge required (requires_action)",
                           intent.get("id"), intent)
        return Outcome("succeeded" if status == "succeeded" else "failed",
                       f"status={status}", intent.get("id"), intent)

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment_method = self._payment_method(s, timed=False)
        intent = self._pay(s, False, charge, payment_method, capture_method="manual")
        if intent.get("status") != "requires_capture":
            raise GatewayError(f"stripe: expected requires_capture, got {intent.get('status')}")
        body = s.expect_ok(
            s.call("POST /v1/payment_intents/{id}/capture", "POST",
                   self._url(f"/v1/payment_intents/{intent['id']}/capture")),
            "capture")
        return Outcome("succeeded" if body.get("status") == "succeeded" else "failed",
                       f"status={body.get('status')}", body.get("id"), body)

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment_method = self._payment_method(s, timed=False)
        intent = self._pay(s, False, charge, payment_method)
        if intent.get("status") != "succeeded":
            raise GatewayError(f"stripe: expected succeeded before refund, "
                               f"got {intent.get('status')}")
        body = s.expect_ok(
            s.call("POST /v1/refunds", "POST", self._url("/v1/refunds"),
                   data={"payment_intent": intent["id"]}),
            "create Refund")
        return Outcome("succeeded" if body.get("status") in ("succeeded", "pending")
                       else "failed", f"status={body.get('status')}", body.get("id"), body)

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment_method = self._payment_method(s, timed=False)
        intent = self._pay(s, False, charge, payment_method, capture_method="manual")
        body = s.expect_ok(
            s.call("POST /v1/payment_intents/{id}/cancel", "POST",
                   self._url(f"/v1/payment_intents/{intent['id']}/cancel")),
            "cancel PaymentIntent")
        return Outcome("succeeded" if body.get("status") == "canceled" else "failed",
                       f"status={body.get('status')}", body.get("id"), body)

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        customer = s.expect_ok(
            s.prep("create Customer", "POST", self._url("/v1/customers"),
                   data={"description": charge.reference}), "create Customer")
        payment_method = self._payment_method(s, timed=False)
        s.expect_ok(
            s.prep("attach PaymentMethod", "POST",
                   self._url(f"/v1/payment_methods/{payment_method}/attach"),
                   data={"customer": customer["id"]}), "attach PaymentMethod")
        # The on-session charge that establishes the mandate. Done once by a real
        # merchant, so it is prep — only the later recurring charge is the MIT operation.
        self._pay(s, False, charge, payment_method,
                  extra={"customer": customer["id"], "setup_future_usage": "off_session"})

        intent = self._pay(s, True, charge, payment_method,
                           extra={"customer": customer["id"], "off_session": "true"})
        status = intent.get("status")
        if status not in ("succeeded", "processing", "requires_capture"):
            return Outcome("failed",
                           f"status={status} — off-session SCA exemption refused; "
                           f"recovery needs an on-session re-auth",
                           intent.get("id"), intent)
        return Outcome("succeeded", f"status={status}", intent.get("id"), intent)
