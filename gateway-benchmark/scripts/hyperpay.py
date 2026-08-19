"""
HyperPay timing module.

Docs mapped in docs/02_api_flow_comparison.md (section "HyperPay"). HyperPay is a
white-label deployment of the OPPWA engine, so the API shape matches OPPWA/Peach.

Auth: ``Authorization: Bearer <access token>`` plus an ``entityId`` on every request.
Bodies: form-encoded. Test host: https://eu-test.oppwa.com (HyperPay issues a
branded equivalent at onboarding - override with HYPERPAY_API_BASE).

UNVERIFIED SHAPES - read before trusting these numbers
------------------------------------------------------
The research pass could not confirm two things on a HyperPay-branded page (both
dedicated tutorial pages 404'd), so they are cross-referenced from Peach Payments,
which runs the same engine:

  * Back-office ops (capture CP / refund RF / reversal RV): this module posts to
    ``/v1/payments/{id}`` with ``paymentType``. Peach's docs show
    ``/v1/payments/{id}/payments`` for the pre-auth+capture scenario. Switch with
    HYPERPAY_BACKOFFICE_PATH=nested if your sandbox rejects the default.
  * The "1 call frictionless / 2 with a challenge" Direct API claim rests on an
    overview page only.

Treat both as sandbox-confirmable, not confirmed. The flags are surfaced in the
report so a reader does not mistake an inferred URL for a documented one.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from harness import (Gateway, GatewayError, MeasuredSession, SkipFlow, env,
                     money, new_reference)

TEST_BASE = "https://eu-test.oppwa.com"


class HyperPay(Gateway):
    name = "hyperpay"
    label = "HyperPay"

    documented_calls = {
        "hosted_checkout": "2 (prepare + mandatory status GET)",
        "direct_api": "1 frictionless, 2 with a challenge (unverified)",
        "capture": "1 (URL shape unverified)",
        "refund": "1 (URL shape unverified)",
        "void": "1 (URL shape unverified)",
        "mit": "1",
    }

    notes = [
        "Back-office URL shape (capture/refund/void) is inferred from Peach Payments on "
        "the same OPPWA engine - HyperPay's own tutorial pages 404'd. Set "
        "HYPERPAY_BACKOFFICE_PATH=nested to try the alternate shape.",
        "The Copy&Pay status GET is throttled to 2 calls/minute per checkout and the "
        "checkout id is single-use after a successful read, so hosted-checkout runs need "
        "DELAY_BETWEEN_RUNS_MS or they will hit the throttle.",
        "The Direct API 1-2 call claim rests on an overview page; the dedicated 3DS "
        "reference page was unreachable during research.",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.access_token = env("HYPERPAY_ACCESS_TOKEN")
        self.entity_id = env("HYPERPAY_ENTITY_ID")
        self.base = (env("HYPERPAY_API_BASE", TEST_BASE) or TEST_BASE).rstrip("/")
        self.backoffice_path = (env("HYPERPAY_BACKOFFICE_PATH", "flat") or "flat").lower()
        self.registration_id = env("HYPERPAY_REGISTRATION_ID")
        self.card = {
            "paymentBrand": env("HYPERPAY_TEST_CARD_BRAND", "VISA"),
            "card.number": env("HYPERPAY_TEST_CARD_NUMBER", "4200000000000000"),
            "card.holder": env("HYPERPAY_TEST_CARD_HOLDER", "Benchmark Runner"),
            "card.expiryMonth": env("HYPERPAY_TEST_CARD_MONTH", "05"),
            "card.expiryYear": env("HYPERPAY_TEST_CARD_YEAR", "2034"),
            "card.cvv": env("HYPERPAY_TEST_CARD_CVV", "123"),
        }

    # -- config ----------------------------------------------------------- #

    def configured(self) -> bool:
        return bool(self.access_token and self.entity_id)

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

    # -- helpers ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _base_data(self, **extra: Any) -> Dict[str, str]:
        data = {
            "entityId": self.entity_id or "",
            "amount": money(self.amount),
            "currency": self.currency or "EUR",
        }
        data.update({k: str(v) for k, v in extra.items()})
        return data

    def _backoffice_url(self, payment_id: str) -> str:
        """Flat vs nested back-office shape - see the module docstring."""
        if self.backoffice_path == "nested":
            return self._url(f"/v1/payments/{payment_id}/payments")
        return self._url(f"/v1/payments/{payment_id}")

    @staticmethod
    def _result_code(body: Dict[str, Any]) -> str:
        return ((body.get("result") or {}).get("code")) or ""

    def _require_success(self, body: Dict[str, Any], what: str) -> None:
        """OPPWA signals outcome in result.code, not the HTTP status.

        000.000.* / 000.100.1* are successes; 000.200.* means "pending, awaiting the
        shopper" which is a legitimate intermediate state for a fresh checkout.
        """
        code = self._result_code(body)
        if not (code.startswith("000.000.") or code.startswith("000.100.1")
                or code.startswith("000.200.")):
            description = (body.get("result") or {}).get("description")
            raise GatewayError(f"hyperpay: {what} result.code={code!r} ({description!r})")

    def _pay(self, s: MeasuredSession, timed: bool, payment_type: str) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        data = self._base_data(paymentType=payment_type,
                               merchantTransactionId=new_reference("hp"))
        data.update(self.card)
        response = issue(f"POST /v1/payments ({payment_type})", "POST",
                         self._url("/v1/payments"), data=data)
        body = s.expect_ok(response, f"payment {payment_type}", accept=(200, 201))
        self._require_success(body, f"payment {payment_type}")
        return body

    @staticmethod
    def _payment_id(body: Dict[str, Any]) -> str:
        payment_id = body.get("id")
        if not payment_id:
            raise GatewayError("hyperpay: response carried no id")
        return payment_id

    def _backoffice(self, s: MeasuredSession, payment_id: str, payment_type: str,
                    label: str, *, with_amount: bool = True) -> None:
        data: Dict[str, str] = {"entityId": self.entity_id or "", "paymentType": payment_type}
        if with_amount:
            data["amount"] = money(self.amount)
            data["currency"] = self.currency or "EUR"
        response = s.call(f"{label} (paymentType={payment_type})", "POST",
                          self._backoffice_url(payment_id), data=data)
        body = s.expect_ok(response, label, accept=(200, 201))
        self._require_success(body, label)

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        """Copy&Pay: POST /v1/checkouts, then the mandatory status read."""
        created = s.expect_ok(
            s.call("POST /v1/checkouts", "POST", self._url("/v1/checkouts"),
                   data=self._base_data(paymentType="DB",
                                        merchantTransactionId=new_reference("hp-cp"))),
            "prepare checkout", accept=(200, 201))
        self._require_success(created, "prepare checkout")

        checkout_id = created.get("id")
        if not checkout_id:
            raise GatewayError("hyperpay: checkout response carried no id")

        # Mandatory per the widget docs. Throttled to 2/min per checkout id and single-use
        # once it returns a final result - hence the DELAY_BETWEEN_RUNS_MS advice.
        s.call("GET /v1/checkouts/{id}/payment", "GET",
               self._url(f"/v1/checkouts/{checkout_id}/payment"),
               params={"entityId": self.entity_id})

    def flow_direct_api(self, s: MeasuredSession) -> None:
        """Server-to-server debit. A second read only if a challenge redirect came back."""
        body = self._pay(s, timed=True, payment_type="DB")

        redirect = body.get("redirect")
        if redirect or self._result_code(body).startswith("000.200."):
            # Challenge branch: the outcome is only knowable from the resourcePath read.
            resource_path = (body.get("resourcePath")
                             or f"/v1/payments/{self._payment_id(body)}")
            s.call("GET {resourcePath}", "GET", self._url(resource_path),
                   params={"entityId": self.entity_id})

    def flow_capture(self, s: MeasuredSession) -> None:
        authorised = self._pay(s, timed=False, payment_type="PA")
        self._backoffice(s, self._payment_id(authorised), "CP", "POST capture")

    def flow_refund(self, s: MeasuredSession) -> None:
        debited = self._pay(s, timed=False, payment_type="DB")
        self._backoffice(s, self._payment_id(debited), "RF", "POST refund")

    def flow_void(self, s: MeasuredSession) -> None:
        authorised = self._pay(s, timed=False, payment_type="PA")
        self._backoffice(s, self._payment_id(authorised), "RV", "POST reversal",
                         with_amount=False)

    def flow_mit(self, s: MeasuredSession) -> None:
        """Single charge against a stored registration, standingInstruction REPEATED/MIT."""
        registration_id = self.registration_id
        if not registration_id:
            # Mint one so the flow is runnable end to end; untimed, since a real merchant
            # already holds the registration before the recurring charge.
            data = self._base_data(paymentType="DB",
                                   merchantTransactionId=new_reference("hp-reg"))
            data.update(self.card)
            data["createRegistration"] = "true"
            body = s.expect_ok(
                s.prep("POST /v1/payments (createRegistration)", "POST",
                       self._url("/v1/payments"), data=data),
                "create registration", accept=(200, 201))
            self._require_success(body, "create registration")
            registration_id = (body.get("registrationId")
                               or (body.get("registration") or {}).get("id"))
            if not registration_id:
                raise SkipFlow(
                    "no registrationId returned - set HYPERPAY_REGISTRATION_ID to a "
                    "pre-existing stored card to measure the MIT charge")

        data = self._base_data(
            paymentType="DB",
            merchantTransactionId=new_reference("hp-mit"),
            **{
                "standingInstruction.mode": "REPEATED",
                "standingInstruction.type": "UNSCHEDULED",
                "standingInstruction.source": "MIT",
                "recurringType": "REPEATED",
            })
        body = s.expect_ok(
            s.call("POST /v1/registrations/{id}/payments", "POST",
                   self._url(f"/v1/registrations/{registration_id}/payments"), data=data),
            "MIT payment", accept=(200, 201))
        self._require_success(body, "MIT payment")


GATEWAY = HyperPay
