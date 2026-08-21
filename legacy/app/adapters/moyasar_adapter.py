"""
Moyasar adapter.

Auth: HTTP Basic — secret key as username, empty password. Bodies: form-encoded.
Amounts are in halalas (minor units); sandboxes are SAR-first.

Hosted checkout uses Invoices (create → hosted url → fetch to confirm).
Direct has two documented minimums that contradict each other, selected by
MOYASAR_DIRECT_MODE: ``token`` (3 calls, the recommended Custom UI guide) or
``card`` (2 calls, what the API reference schema allows).
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from app.adapters.base import Adapter, Card, Charge, Outcome, Started
from app.config import env
from app.timing import GatewayError, MeasuredSession, NotConfigured, minor_units


class MoyasarAdapter(Adapter):
    name = "moyasar"
    label = "Moyasar"

    documented_calls = {
        "hosted_checkout": "2 (create invoice + fetch)",
        "direct_api": "2 raw-card / 3 tokenize-first",
        "capture": "1", "refund": "1", "void": "1",
        "mit": "2 documented (token minted once, then charge)",
    }

    notes = [
        "Moyasar's API reference and its recommended Custom UI guide disagree on "
        "whether raw card fields or tokenize-first is the production path. "
        "MOYASAR_DIRECT_MODE selects which one is measured.",
        "Void has roughly a 2-hour post-capture window; after that only refund works.",
        "MIT measures 1 timed call, not the documented 2: the token is minted once "
        "and reused, so only the charge is a per-recurring-charge cost.",
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        self.secret_key = env("MOYASAR_SECRET_KEY")
        self.publishable_key = env("MOYASAR_PUBLISHABLE_KEY")
        self.base = (env("MOYASAR_API_BASE", "https://api.moyasar.com")
                     or "https://api.moyasar.com").rstrip("/")
        self.direct_mode = (env("MOYASAR_DIRECT_MODE", "token") or "token").lower()

    def missing_config(self) -> List[str]:
        return [] if self.secret_key else ["MOYASAR_SECRET_KEY"]

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (self.secret_key or "", "")
        return session

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _card_source(self, card: Card) -> Dict[str, str]:
        return {"source[type]": "creditcard", "source[name]": card.holder,
                "source[number]": card.number, "source[month]": card.month,
                "source[year]": card.year, "source[cvc]": card.cvc}

    def _token(self, s: MeasuredSession, timed: bool, card: Card) -> str:
        if not self.publishable_key:
            raise NotConfigured("MOYASAR_PUBLISHABLE_KEY is needed to mint a card token.")
        issue = s.call if timed else s.prep
        response = issue("POST /v1/tokens", "POST", self._url("/v1/tokens"),
                         data={"publishable_api_key": self.publishable_key,
                               "name": card.holder, "number": card.number,
                               "month": card.month, "year": card.year,
                               "cvc": card.cvc, "save_only": "true"})
        return s.expect_ok(response, "create token")["id"]

    def _pay(self, s: MeasuredSession, timed: bool, charge: Charge,
             source: Dict[str, str], manual: bool = False) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        data: Dict[str, str] = {
            "amount": str(minor_units(charge.amount)),
            "currency": charge.currency,
            "description": charge.reference,
            "callback_url": charge.webhook_url or charge.return_url,
        }
        if manual:
            data["source[manual]"] = "true"
        data.update(source)
        return s.expect_ok(issue("POST /v1/payments", "POST", self._url("/v1/payments"),
                                 data=data), "create payment", accept=(200, 201))

    @staticmethod
    def _outcome(body: Dict[str, Any]) -> Outcome:
        status = str(body.get("status") or "").lower()
        mapping = {"paid": "succeeded", "captured": "succeeded", "authorized": "succeeded",
                   "refunded": "succeeded", "voided": "succeeded",
                   "initiated": "pending", "pending": "pending"}
        message = (body.get("source") or {}).get("message")
        return Outcome(mapping.get(status, "failed"),
                       f"status={status!r}" + (f" message={message!r}" if message else ""),
                       body.get("id"), body)

    @staticmethod
    def _id(body: Dict[str, Any]) -> str:
        value = body.get("id")
        if not value:
            raise GatewayError("moyasar: response carried no id")
        return value

    # -- hosted ----------------------------------------------------------- #

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        body = s.expect_ok(
            s.call("POST /v1/invoices", "POST", self._url("/v1/invoices"),
                   data={"amount": str(minor_units(charge.amount)),
                         "currency": charge.currency,
                         "description": charge.reference,
                         "callback_url": charge.webhook_url or charge.return_url,
                         "success_url": charge.return_url,
                         "back_url": charge.return_url}),
            "create invoice", accept=(200, 201))
        url = body.get("url")
        if not url:
            raise GatewayError("moyasar: invoice response carried no hosted url")
        return Started(redirect_url=url, gateway_ref=self._id(body),
                       context={"invoice_id": self._id(body)})

    def confirm_hosted(self, s: MeasuredSession, charge: Charge, context: Dict[str, Any],
                       params: Dict[str, str]) -> Outcome:
        invoice_id = params.get("invoice_id") or context.get("invoice_id")
        if not invoice_id:
            raise GatewayError("moyasar: no invoice id to confirm against")
        body = s.expect_ok(
            s.call("GET /v1/invoices/{id}", "GET", self._url(f"/v1/invoices/{invoice_id}")),
            "fetch invoice", accept=(200,))
        return self._outcome(body)

    # -- server-side ------------------------------------------------------ #

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        if self.direct_mode == "token":
            token = self._token(s, True, card)
            body = self._pay(s, True, charge,
                             {"source[type]": "token", "source[token]": token})
        else:
            body = self._pay(s, True, charge, self._card_source(card))
        # 3DS is a transaction_url field on the response, not a separate call — this
        # fetch is needed either way, which is why it is always timed.
        fetched = s.expect_ok(
            s.call("GET /v1/payments/{id}", "GET",
                   self._url(f"/v1/payments/{self._id(body)}")),
            "fetch payment", accept=(200,))
        return self._outcome(fetched)

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment = self._pay(s, False, charge, self._card_source(card), manual=True)
        body = s.expect_ok(
            s.call("POST /v1/payments/{id}/capture", "POST",
                   self._url(f"/v1/payments/{self._id(payment)}/capture"),
                   data={"amount": str(minor_units(charge.amount))}),
            "capture", accept=(200, 201))
        return self._outcome(body)

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment = self._pay(s, False, charge, self._card_source(card))
        body = s.expect_ok(
            s.call("POST /v1/payments/{id}/refund", "POST",
                   self._url(f"/v1/payments/{self._id(payment)}/refund"),
                   data={"amount": str(minor_units(charge.amount))}),
            "refund", accept=(200, 201))
        return self._outcome(body)

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        payment = self._pay(s, False, charge, self._card_source(card), manual=True)
        body = s.expect_ok(
            s.call("POST /v1/payments/{id}/void", "POST",
                   self._url(f"/v1/payments/{self._id(payment)}/void")),
            "void", accept=(200, 201))
        return self._outcome(body)

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        token = self._token(s, False, card)
        body = self._pay(s, True, charge,
                         {"source[type]": "token", "source[token]": token})
        return self._outcome(body)
