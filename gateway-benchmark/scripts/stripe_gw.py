"""
Stripe timing module.

Docs mapped in docs/02_api_flow_comparison.md (section "Stripe") and
docs/03_stripe_flow_mapping.md.

Auth: HTTP Basic, secret key as the username, empty password.
Bodies: form-encoded (Stripe does not accept JSON request bodies).

Card data: this module charges Stripe's test *tokens* (``tok_visa``) rather than
raw PANs. Stripe blocks raw card numbers on /v1/payment_methods for accounts
without PCI enablement, so a raw-PAN variant would fail on most sandbox accounts
and would not be comparable anyway. The token exchange happens in the browser in
a real integration, so it is not a merchant-server call - matching how the
comparison doc counts Checkout.com and Adyen.
"""

from __future__ import annotations

from typing import Any, Dict

import requests

from harness import (Gateway, GatewayError, MeasuredSession, SkipFlow, env,
                     new_reference)

API_BASE = "https://api.stripe.com"


class Stripe(Gateway):
    name = "stripe"
    label = "Stripe"

    documented_calls = {
        "hosted_checkout": "1 out + 1 webhook (+1 optional GET)",
        "direct_api": "1-2 (create+confirm; +1 if a PaymentMethod must be minted)",
        "capture": "1",
        "refund": "1",
        "void": "1",
        "mit": "1",
    }

    notes = [
        "Stripe's own quickstart steers integrators away from raw PaymentIntents "
        "toward Checkout Sessions/Elements, so the 1-call Direct API minimum is a real "
        "capability but not Stripe's recommended default path.",
        "Hosted Checkout fulfillment is webhook-mandatory per Stripe's docs; the GET "
        "measured here is the defensive re-check Stripe's own sample code performs, "
        "not a substitute for the webhook.",
        "MIT off-session failures land in requires_payment_method (not requires_action), "
        "which needs a full on-session re-auth to recover.",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.secret_key = env("STRIPE_SECRET_KEY")
        self.test_token = env("STRIPE_TEST_TOKEN", "tok_visa")
        self.base = env("STRIPE_API_BASE", API_BASE)
        self.api_version = env("STRIPE_API_VERSION")

    # -- config ----------------------------------------------------------- #

    def configured(self) -> bool:
        return bool(self.secret_key)

    def missing_config(self):
        return [] if self.secret_key else ["STRIPE_SECRET_KEY"]

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (self.secret_key or "", "")
        if self.api_version:
            session.headers["Stripe-Version"] = self.api_version
        return session

    # -- helpers ---------------------------------------------------------- #

    @property
    def minor_amount(self) -> int:
        """Stripe takes amounts in the currency's smallest unit."""
        return int(round(self.amount * 100))

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _payment_method(self, s: MeasuredSession, timed: bool) -> str:
        """Mint a PaymentMethod from a test token. Timed only inside the Direct API flow."""
        issue = s.call if timed else s.prep
        response = issue("create PaymentMethod", "POST", self._url("/v1/payment_methods"),
                         data={"type": "card", "card[token]": self.test_token})
        return s.expect_ok(response, "create PaymentMethod")["id"]

    def _pay(self, s: MeasuredSession, timed: bool, *, payment_method: str,
             capture_method: str = "automatic", extra: Dict[str, Any] | None = None
             ) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        data = {
            "amount": str(self.minor_amount),
            "currency": (self.currency or "usd").lower(),
            "payment_method": payment_method,
            "confirm": "true",
            "capture_method": capture_method,
            # Card-only, no redirect-based methods: keeps the sandbox flow frictionless
            # and comparable to the other gateways' non-3DS paths.
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
            "description": new_reference("stripe"),
        }
        if extra:
            data.update(extra)
        response = issue("create+confirm PaymentIntent", "POST",
                         self._url("/v1/payment_intents"), data=data)
        return s.expect_ok(response, "create+confirm PaymentIntent")

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        """POST /v1/checkout/sessions, then the defensive GET from Stripe's sample code."""
        reference = new_reference("stripe-co")
        created = s.expect_ok(
            s.call("create Checkout Session", "POST",
                   self._url("/v1/checkout/sessions"),
                   data={
                       "mode": "payment",
                       "success_url": env("STRIPE_SUCCESS_URL",
                                          "https://example.com/success"
                                          "?session_id={CHECKOUT_SESSION_ID}"),
                       "cancel_url": env("STRIPE_CANCEL_URL", "https://example.com/cancel"),
                       "client_reference_id": reference,
                       "line_items[0][price_data][currency]": (self.currency or "usd").lower(),
                       "line_items[0][price_data][product_data][name]": "Benchmark item",
                       "line_items[0][price_data][unit_amount]": str(self.minor_amount),
                       "line_items[0][quantity]": "1",
                   }),
            "create Checkout Session")

        session_id = created.get("id")
        if not session_id:
            raise GatewayError("stripe: Checkout Session response had no id")

        # The authoritative confirmation is the checkout.session.completed webhook, which
        # a merchant does not initiate and therefore cannot time as a round trip. This GET
        # is what Stripe's reference fulfillment handler calls to re-verify.
        s.expect_ok(s.call("retrieve Checkout Session", "GET",
                           self._url(f"/v1/checkout/sessions/{session_id}")),
                    "retrieve Checkout Session")

    def flow_direct_api(self, s: MeasuredSession) -> None:
        """PaymentMethod (conditional) + create/confirm PaymentIntent."""
        payment_method = self._payment_method(s, timed=True)
        intent = self._pay(s, timed=True, payment_method=payment_method)

        # requires_action would mean a 3DS challenge; with allow_redirects=never and a
        # frictionless test token it should not happen, but record it if it does.
        if intent.get("status") == "requires_action":
            raise GatewayError(
                "stripe: PaymentIntent returned requires_action - a 3DS challenge fired, "
                "so this run is not comparable to the frictionless baseline")

    def flow_capture(self, s: MeasuredSession) -> None:
        """Prep an uncaptured auth, then time the single capture call."""
        payment_method = self._payment_method(s, timed=False)
        intent = self._pay(s, timed=False, payment_method=payment_method,
                           capture_method="manual")
        if intent.get("status") != "requires_capture":
            raise GatewayError(
                f"stripe: expected requires_capture before capture, got {intent.get('status')}")
        s.expect_ok(s.call("capture PaymentIntent", "POST",
                           self._url(f"/v1/payment_intents/{intent['id']}/capture")),
                    "capture PaymentIntent")

    def flow_refund(self, s: MeasuredSession) -> None:
        """Prep a captured payment, then time the single refund call."""
        payment_method = self._payment_method(s, timed=False)
        intent = self._pay(s, timed=False, payment_method=payment_method)
        if intent.get("status") != "succeeded":
            raise GatewayError(
                f"stripe: expected succeeded before refund, got {intent.get('status')}")
        s.expect_ok(s.call("create Refund", "POST", self._url("/v1/refunds"),
                           data={"payment_intent": intent["id"]}),
                    "create Refund")

    def flow_void(self, s: MeasuredSession) -> None:
        """Prep an uncaptured auth, then time the single cancel call."""
        payment_method = self._payment_method(s, timed=False)
        intent = self._pay(s, timed=False, payment_method=payment_method,
                           capture_method="manual")
        s.expect_ok(s.call("cancel PaymentIntent", "POST",
                           self._url(f"/v1/payment_intents/{intent['id']}/cancel")),
                    "cancel PaymentIntent")

    def flow_mit(self, s: MeasuredSession) -> None:
        """Prep a customer with a saved card, then time the single off-session charge."""
        customer = s.expect_ok(
            s.prep("create Customer", "POST", self._url("/v1/customers"),
                   data={"description": new_reference("stripe-mit")}),
            "create Customer")
        payment_method = self._payment_method(s, timed=False)
        s.expect_ok(
            s.prep("attach PaymentMethod", "POST",
                   self._url(f"/v1/payment_methods/{payment_method}/attach"),
                   data={"customer": customer["id"]}),
            "attach PaymentMethod")

        # The on-session first charge that establishes the mandate. Real merchants do this
        # once; only the later recurring charge is the MIT operation being measured.
        self._pay(s, timed=False, payment_method=payment_method,
                  extra={"customer": customer["id"], "setup_future_usage": "off_session"})

        intent = self._pay(s, timed=True, payment_method=payment_method,
                           extra={"customer": customer["id"], "off_session": "true"})
        if intent.get("status") not in ("succeeded", "processing", "requires_capture"):
            raise GatewayError(
                f"stripe: MIT charge ended in {intent.get('status')} - off-session "
                "SCA exemption was refused; recovery needs an on-session re-auth")


GATEWAY = Stripe
