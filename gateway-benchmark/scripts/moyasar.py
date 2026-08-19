"""
Moyasar timing module.

Docs mapped in docs/02_api_flow_comparison.md (section "Moyasar").

Auth: HTTP Basic - secret key as the username, empty password.
Bodies: form-encoded (Moyasar also accepts JSON; form matches the docs' examples).
Currency: SAR by default; Moyasar amounts are in halalas (minor units).

Direct API note: Moyasar's own docs disagree with themselves about the minimum -
the API reference schema allows raw card fields straight to /v1/payments (2 calls
with the confirming fetch), while the recommended Custom UI guide prescribes
tokenize-first (3 calls). Both are implemented; MOYASAR_DIRECT_MODE selects which
one is measured, defaulting to the tokenize-first path Moyasar recommends.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from harness import (Gateway, GatewayError, MeasuredSession, SkipFlow, env,
                     new_reference)

API_BASE = "https://api.moyasar.com"


class Moyasar(Gateway):
    name = "moyasar"
    label = "Moyasar"

    documented_calls = {
        "hosted_checkout": "2 (create + confirm)",
        "direct_api": "2 raw-card / 3 tokenize-first",
        "capture": "1",
        "refund": "1",
        "void": "1",
        "mit": "2 (token minted first, then charge)",
    }

    notes = [
        "The API reference schema and the recommended Custom UI guide disagree on whether "
        "raw card fields or tokenize-first is the production path; MOYASAR_DIRECT_MODE "
        "picks which one is measured so the two can be compared directly.",
        "Void has roughly a 2-hour post-capture window; after that only refund is possible.",
        "Capture must happen within 14 days on Mada.",
        "MIT measures 1 timed call, not the 2 the docs describe: the token is minted "
        "once and reused, so only the charge is a per-recurring-charge cost. Contrast "
        "Geidea, whose MIT needs a fresh session on every single charge - both of its "
        "calls are timed because both recur.",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.secret_key = env("MOYASAR_SECRET_KEY")
        self.publishable_key = env("MOYASAR_PUBLISHABLE_KEY")
        self.base = (env("MOYASAR_API_BASE", API_BASE) or API_BASE).rstrip("/")
        self.direct_mode = (env("MOYASAR_DIRECT_MODE", "token") or "token").lower()
        self.callback_url = env("MOYASAR_CALLBACK_URL", "https://example.com/moyasar/callback")
        self.card = {
            "name": env("MOYASAR_TEST_CARD_HOLDER", "Benchmark Runner"),
            "number": env("MOYASAR_TEST_CARD_NUMBER", "4111111111111111"),
            "month": env("MOYASAR_TEST_CARD_MONTH", "12"),
            "year": env("MOYASAR_TEST_CARD_YEAR", "30"),
            "cvc": env("MOYASAR_TEST_CARD_CVC", "123"),
        }

    # -- config ----------------------------------------------------------- #

    def configured(self) -> bool:
        return bool(self.secret_key)

    def missing_config(self) -> List[str]:
        return [] if self.secret_key else ["MOYASAR_SECRET_KEY"]

    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (self.secret_key or "", "")
        return session

    # -- helpers ---------------------------------------------------------- #

    @property
    def minor_amount(self) -> int:
        """Moyasar amounts are in the minor unit (halalas for SAR)."""
        return int(round(self.amount * 100))

    def _currency(self) -> str:
        # Moyasar sandboxes are SAR-first; TEST_CURRENCY is shared across gateways, so a
        # dedicated override keeps a USD-wide run from failing only here.
        return env("MOYASAR_CURRENCY", "SAR") or "SAR"

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _card_source(self) -> Dict[str, str]:
        return {
            "source[type]": "creditcard",
            "source[name]": self.card["name"],
            "source[number]": self.card["number"],
            "source[month]": self.card["month"],
            "source[year]": self.card["year"],
            "source[cvc]": self.card["cvc"],
        }

    def _token(self, s: MeasuredSession, timed: bool) -> str:
        if not self.publishable_key:
            raise SkipFlow("MOYASAR_PUBLISHABLE_KEY not set - needed to mint a card token")
        issue = s.call if timed else s.prep
        response = issue("POST /v1/tokens", "POST", self._url("/v1/tokens"),
                         data={
                             "publishable_api_key": self.publishable_key,
                             "name": self.card["name"],
                             "number": self.card["number"],
                             "month": self.card["month"],
                             "year": self.card["year"],
                             "cvc": self.card["cvc"],
                             "save_only": "true",
                         })
        return s.expect_ok(response, "create token")["id"]

    def _pay(self, s: MeasuredSession, timed: bool, *, source: Dict[str, str] | None = None,
             manual: bool = False) -> Dict[str, Any]:
        issue = s.call if timed else s.prep
        data: Dict[str, str] = {
            "amount": str(self.minor_amount),
            "currency": self._currency(),
            "description": new_reference("moyasar"),
            "callback_url": self.callback_url,
        }
        if manual:
            data["source[manual]"] = "true"
        data.update(source or self._card_source())
        response = issue("POST /v1/payments", "POST", self._url("/v1/payments"), data=data)
        return s.expect_ok(response, "create payment", accept=(200, 201))

    @staticmethod
    def _payment_id(body: Dict[str, Any]) -> str:
        payment_id = body.get("id")
        if not payment_id:
            raise GatewayError("moyasar: payment response carried no id")
        return payment_id

    def _require_status(self, body: Dict[str, Any], allowed: tuple, what: str) -> None:
        status = body.get("status")
        if status not in allowed:
            source = body.get("source") or {}
            raise GatewayError(f"moyasar: {what} status={status!r} "
                               f"message={source.get('message')!r}")

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        """POST /v1/invoices then GET /v1/invoices/{id} for the server-side confirmation."""
        created = s.expect_ok(
            s.call("POST /v1/invoices", "POST", self._url("/v1/invoices"),
                   data={
                       "amount": str(self.minor_amount),
                       "currency": self._currency(),
                       "description": new_reference("moyasar-inv"),
                       "callback_url": self.callback_url,
                       "success_url": env("MOYASAR_SUCCESS_URL", "https://example.com/success"),
                       "back_url": env("MOYASAR_BACK_URL", "https://example.com/back"),
                   }),
            "create invoice", accept=(200, 201))

        invoice_id = created.get("id")
        if not invoice_id:
            raise GatewayError("moyasar: invoice response carried no id")
        s.expect_ok(
            s.call("GET /v1/invoices/{id}", "GET", self._url(f"/v1/invoices/{invoice_id}")),
            "fetch invoice", accept=(200,))

    def flow_direct_api(self, s: MeasuredSession) -> None:
        """Raw-card (2 calls) or tokenize-first (3 calls), per MOYASAR_DIRECT_MODE."""
        if self.direct_mode == "token":
            token = self._token(s, timed=True)
            body = self._pay(s, timed=True,
                             source={"source[type]": "token", "source[token]": token})
        elif self.direct_mode in ("card", "creditcard", "raw"):
            body = self._pay(s, timed=True)
        else:
            raise SkipFlow(f"MOYASAR_DIRECT_MODE={self.direct_mode!r} is not 'token' or 'card'")

        # 3DS is not a separate call here - it is a transaction_url field on this response,
        # and the fetch below is needed regardless of whether a challenge happened.
        s.expect_ok(
            s.call("GET /v1/payments/{id}", "GET",
                   self._url(f"/v1/payments/{self._payment_id(body)}")),
            "fetch payment", accept=(200,))

    def flow_capture(self, s: MeasuredSession) -> None:
        payment = self._pay(s, timed=False, manual=True)
        self._require_status(payment, ("authorized", "initiated", "paid"), "pre-capture payment")
        s.expect_ok(
            s.call("POST /v1/payments/{id}/capture", "POST",
                   self._url(f"/v1/payments/{self._payment_id(payment)}/capture"),
                   data={"amount": str(self.minor_amount)}),
            "capture", accept=(200, 201))

    def flow_refund(self, s: MeasuredSession) -> None:
        payment = self._pay(s, timed=False)
        self._require_status(payment, ("paid", "initiated"), "pre-refund payment")
        s.expect_ok(
            s.call("POST /v1/payments/{id}/refund", "POST",
                   self._url(f"/v1/payments/{self._payment_id(payment)}/refund"),
                   data={"amount": str(self.minor_amount)}),
            "refund", accept=(200, 201))

    def flow_void(self, s: MeasuredSession) -> None:
        payment = self._pay(s, timed=False, manual=True)
        s.expect_ok(
            s.call("POST /v1/payments/{id}/void", "POST",
                   self._url(f"/v1/payments/{self._payment_id(payment)}/void")),
            "void", accept=(200, 201))

    def flow_mit(self, s: MeasuredSession) -> None:
        """Token minted out of band, then a single token-sourced charge - the 2nd of the 2 calls."""
        token = self._token(s, timed=False)
        body = self._pay(s, timed=True,
                         source={"source[type]": "token", "source[token]": token})
        self._require_status(body, ("paid", "initiated", "authorized"), "MIT charge")


GATEWAY = Moyasar
