"""
HyperPay adapter (OPPWA engine).

Auth: Bearer access token plus an ``entityId`` on every request. Bodies: form-encoded.
Test host: https://eu-test.oppwa.com — HyperPay issues a branded equivalent at
onboarding, so HYPERPAY_API_BASE overrides it.

UNVERIFIED — the back-office URL shape for capture (CP), refund (RF) and reversal
(RV) could not be confirmed on a HyperPay-branded page; both dedicated tutorial
pages 404'd. The default posts to /v1/payments/{id}; Peach Payments, running the
same engine, documents /v1/payments/{id}/payments. Switch with
HYPERPAY_BACKOFFICE_PATH=nested if the sandbox rejects the default.

The Copy&Pay status read is throttled to 2 calls/minute per checkout and the
checkout id becomes single-use once it returns a final result.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from app.adapters.base import Adapter, Card, Charge, Outcome, Started
from app.config import env
from app.timing import GatewayError, MeasuredSession, money


class HyperPayAdapter(Adapter):
    name = "hyperpay"
    label = "HyperPay"

    documented_calls = {
        "hosted_checkout": "2 (prepare + mandatory status read)",
        "direct_api": "1 frictionless, 2 with a challenge (unverified)",
        "capture": "1 (URL shape unverified)", "refund": "1 (URL shape unverified)",
        "void": "1 (URL shape unverified)", "mit": "1",
    }

    notes = [
        "Back-office URL shape for capture/refund/void is inferred from Peach Payments "
        "on the same OPPWA engine — HyperPay's own tutorial pages 404'd. Set "
        "HYPERPAY_BACKOFFICE_PATH=nested to try the alternate shape.",
        "The Copy&Pay status read is throttled to 2 calls/minute per checkout and the "
        "checkout id is single-use once final.",
        "The 1–2 call Direct claim rests on an overview page; the dedicated 3DS "
        "reference page was unreachable during research.",
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        self.access_token = env("HYPERPAY_ACCESS_TOKEN")
        self.entity_id = env("HYPERPAY_ENTITY_ID")
        self.base = (env("HYPERPAY_API_BASE", "https://eu-test.oppwa.com")
                     or "https://eu-test.oppwa.com").rstrip("/")
        self.backoffice_path = (env("HYPERPAY_BACKOFFICE_PATH", "flat") or "flat").lower()
        self.registration_id = env("HYPERPAY_REGISTRATION_ID")

    def missing_config(self) -> List[str]:
        missing = []
        if not self.access_token:
            missing.append("HYPERPAY_ACCESS_TOKEN")
        if not self.entity_id:
            missing.append("HYPERPAY_ENTITY_ID")
        return missing

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {self.access_token or ''}"
        return session

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _base_data(self, charge: Charge, **extra: Any) -> Dict[str, str]:
        data = {"entityId": self.entity_id or "", "amount": money(charge.amount),
                "currency": charge.currency}
        data.update({k: str(v) for k, v in extra.items()})
        return data

    def _card(self, card: Card) -> Dict[str, str]:
        return {"paymentBrand": env("HYPERPAY_TEST_CARD_BRAND", "VISA") or "VISA",
                "card.number": card.number, "card.holder": card.holder,
                "card.expiryMonth": card.month, "card.expiryYear": card.year,
                "card.cvv": card.cvc}

    def _backoffice_url(self, payment_id: str) -> str:
        if self.backoffice_path == "nested":
            return self._url(f"/v1/payments/{payment_id}/payments")
        return self._url(f"/v1/payments/{payment_id}")

    @staticmethod
    def _code(body: Dict[str, Any]) -> str:
        return ((body.get("result") or {}).get("code")) or ""

    @classmethod
    def _outcome(cls, body: Dict[str, Any]) -> Outcome:
        """OPPWA carries the real outcome in result.code, not the HTTP status.

        000.000.* and 000.100.1* are successes; 000.200.* means pending, awaiting the
        shopper — a legitimate state for a freshly prepared checkout.
        """
        code = cls._code(body)
        description = (body.get("result") or {}).get("description")
        if code.startswith("000.000.") or code.startswith("000.100.1"):
            status = "succeeded"
        elif code.startswith("000.200."):
            status = "pending"
        else:
            status = "failed"
        return Outcome(status, f"result.code={code!r} ({description!r})",
                       body.get("id"), body)

    def _require_ok(self, body: Dict[str, Any], what: str) -> Dict[str, Any]:
        outcome = self._outcome(body)
        if outcome.status == "failed":
            raise GatewayError(f"hyperpay: {what} {outcome.detail}")
        return body

    def _pay(self, s: MeasuredSession, timed: bool, charge: Charge, card: Card,
             payment_type: str, **extra: Any) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        data = self._base_data(charge, paymentType=payment_type,
                               merchantTransactionId=charge.reference, **extra)
        data.update(self._card(card))
        body = s.expect_ok(issue(f"POST /v1/payments ({payment_type})", "POST",
                                 self._url("/v1/payments"), data=data),
                           f"payment {payment_type}", accept=(200, 201))
        return self._require_ok(body, f"payment {payment_type}")

    @staticmethod
    def _id(body: Dict[str, Any]) -> str:
        value = body.get("id")
        if not value:
            raise GatewayError("hyperpay: response carried no id")
        return value

    def _backoffice(self, s: MeasuredSession, charge: Charge, payment_id: str,
                    payment_type: str, label: str, with_amount: bool = True) -> Outcome:
        data: Dict[str, str] = {"entityId": self.entity_id or "", "paymentType": payment_type}
        if with_amount:
            data["amount"] = money(charge.amount)
            data["currency"] = charge.currency
        body = s.expect_ok(
            s.call(f"{label} (paymentType={payment_type})", "POST",
                   self._backoffice_url(payment_id), data=data),
            label, accept=(200, 201))
        return self._outcome(self._require_ok(body, label))

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        body = s.expect_ok(
            s.call("POST /v1/checkouts", "POST", self._url("/v1/checkouts"),
                   data=self._base_data(charge, paymentType="DB",
                                        merchantTransactionId=charge.reference,
                                        shopperResultUrl=charge.return_url)),
            "prepare checkout", accept=(200, 201))
        self._require_ok(body, "prepare checkout")
        checkout_id = self._id(body)
        # Copy&Pay is a widget mounted in the merchant's own page rather than a hosted
        # URL to 303 to, so the handoff route below renders that widget.
        return Started(redirect_url=f"/handoff/hyperpay/{checkout_id}",
                       gateway_ref=checkout_id, context={"checkout_id": checkout_id})

    def confirm_hosted(self, s: MeasuredSession, charge: Charge, context: Dict[str, Any],
                       params: Dict[str, str]) -> Outcome:
        checkout_id = context.get("checkout_id")
        resource_path = params.get("resourcePath") or (
            f"/v1/checkouts/{checkout_id}/payment" if checkout_id else None)
        if not resource_path:
            raise GatewayError("hyperpay: no resourcePath or checkout id to confirm against")
        body = s.expect_ok(
            s.call("GET {resourcePath}", "GET", self._url(resource_path),
                   params={"entityId": self.entity_id}),
            "checkout status", accept=(200,))
        return self._outcome(body)

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        body = self._pay(s, True, charge, card, "DB")
        if body.get("redirect") or self._code(body).startswith("000.200."):
            resource_path = body.get("resourcePath") or f"/v1/payments/{self._id(body)}"
            body = s.expect_ok(
                s.call("GET {resourcePath}", "GET", self._url(resource_path),
                       params={"entityId": self.entity_id}),
                "payment status", accept=(200,))
        return self._outcome(body)

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        authorised = self._pay(s, False, charge, card, "PA")
        return self._backoffice(s, charge, self._id(authorised), "CP", "POST capture")

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        debited = self._pay(s, False, charge, card, "DB")
        return self._backoffice(s, charge, self._id(debited), "RF", "POST refund")

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        authorised = self._pay(s, False, charge, card, "PA")
        return self._backoffice(s, charge, self._id(authorised), "RV", "POST reversal",
                                with_amount=False)

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        registration_id = self.registration_id
        if not registration_id:
            body = self._pay(s, False, charge, card, "DB", createRegistration="true")
            registration_id = (body.get("registrationId")
                               or (body.get("registration") or {}).get("id"))
            if not registration_id:
                raise GatewayError(
                    "hyperpay: no registrationId returned. Set HYPERPAY_REGISTRATION_ID "
                    "to a stored card to measure the MIT charge.")
        data = self._base_data(
            charge, paymentType="DB", merchantTransactionId=charge.reference,
            **{"standingInstruction.mode": "REPEATED",
               "standingInstruction.type": "UNSCHEDULED",
               "standingInstruction.source": "MIT", "recurringType": "REPEATED"})
        body = s.expect_ok(
            s.call("POST /v1/registrations/{id}/payments", "POST",
                   self._url(f"/v1/registrations/{registration_id}/payments"), data=data),
            "MIT payment", accept=(200, 201))
        return self._outcome(self._require_ok(body, "MIT payment"))
