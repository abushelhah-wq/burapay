"""
A local stand-in for all six gateways.

Not a simulator: it does not model authorisation, and its latencies mean nothing.
Its job is to answer each adapter's documented endpoints with correctly *shaped*
responses, so request construction, response parsing, call sequencing and the
timed/prep split can be exercised end to end without sandbox credentials.

HyperPay and Moyasar both serve POST /v1/payments, so every gateway gets its own
``/_<name>`` base-URL prefix and routes resolve inside that namespace.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, urlparse

Handler = Callable[[Dict[str, Any], "re.Match"], Tuple[int, Dict[str, Any]]]

OWNERS = ("stripe", "adyen", "checkout_com", "hyperpay", "moyasar", "geidea")

REQUEST_LOG: List[Tuple[str, str]] = []
#: handler name -> (status, body). Lets a test bend one endpoint without touching routes.
RESPONSE_OVERRIDES: Dict[str, Tuple[int, Dict[str, Any]]] = {}

ROUTES: List[Tuple[str, str, Pattern[str], Handler]] = []


def route(owner: str, method: str, pattern: str) -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        ROUTES.append((owner, method, re.compile(f"^{pattern}$"), func))
        return func
    return register


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# -- Geidea ------------------------------------------------------------------ #

def _geidea_ok(extra: Optional[Dict[str, Any]] = None):
    body: Dict[str, Any] = {"responseCode": "000", "responseMessage": "Success",
                            "detailedResponseCode": "000",
                            "detailedResponseMessage": "Success"}
    if extra:
        body.update(extra)
    return 200, body


@route("geidea", "POST", r"/payment-intent/api/v2/direct/session")
def geidea_session(_b, _m):
    session_id = _id("sess")
    return _geidea_ok({"session": {
        "id": session_id, "status": "Initiated",
        "redirectUrl": f"https://gateway.example/hosted/{session_id}"}})


@route("geidea", "POST", r"/pgw/api/v6/direct/authenticate/initiate")
def geidea_initiate(_b, _m):
    return _geidea_ok({"threeDSecureId": _id("tds"), "enrollmentStatus": "Enrolled"})


@route("geidea", "POST", r"/pgw/api/v6/direct/authenticate/payer")
def geidea_payer(_b, _m):
    return _geidea_ok({"authenticationStatus": "Authenticated"})


@route("geidea", "POST", r"/pgw/api/v2/direct/pay")
def geidea_pay(_b, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Success"}})


@route("geidea", "POST", r"/pgw/api/v2/direct/pay/token")
def geidea_pay_token(_b, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Success"}})


@route("geidea", "GET", r"/pgw/api/v1/direct/order/(?P<id>[^/]+)")
def geidea_order(_b, m):
    return _geidea_ok({"order": {"orderId": m.group("id"), "status": "Success"}})


@route("geidea", "POST", r"/pgw/api/v1/direct/capture")
def geidea_capture(_b, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Captured"}})


@route("geidea", "POST", r"/pgw/api/v2/direct/refund")
def geidea_refund(_b, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Refunded"}})


@route("geidea", "POST", r"/pgw/api/v3/direct/void")
def geidea_void(_b, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Voided"}})


# -- Stripe ------------------------------------------------------------------ #

@route("stripe", "POST", r"/v1/checkout/sessions")
def stripe_create_session(_b, _m):
    cs = _id("cs")
    return 200, {"id": cs, "url": f"https://checkout.stripe.com/pay/{cs}",
                 "status": "open", "payment_status": "unpaid"}


@route("stripe", "GET", r"/v1/checkout/sessions/(?P<id>[^/]+)")
def stripe_get_session(_b, m):
    return 200, {"id": m.group("id"), "status": "complete", "payment_status": "paid"}


@route("stripe", "POST", r"/v1/payment_methods")
def stripe_create_pm(_b, _m):
    return 200, {"id": _id("pm"), "type": "card"}


@route("stripe", "POST", r"/v1/payment_methods/(?P<id>[^/]+)/attach")
def stripe_attach(_b, m):
    return 200, {"id": m.group("id")}


@route("stripe", "POST", r"/v1/customers")
def stripe_customer(_b, _m):
    return 200, {"id": _id("cus")}


@route("stripe", "POST", r"/v1/payment_intents")
def stripe_pi(body, _m):
    manual = body.get("capture_method") == "manual"
    return 200, {"id": _id("pi"), "status": "requires_capture" if manual else "succeeded"}


@route("stripe", "POST", r"/v1/payment_intents/(?P<id>[^/]+)/capture")
def stripe_capture(_b, m):
    return 200, {"id": m.group("id"), "status": "succeeded"}


@route("stripe", "POST", r"/v1/payment_intents/(?P<id>[^/]+)/cancel")
def stripe_cancel(_b, m):
    return 200, {"id": m.group("id"), "status": "canceled"}


@route("stripe", "POST", r"/v1/refunds")
def stripe_refund(_b, _m):
    return 200, {"id": _id("re"), "status": "succeeded"}


# -- Adyen -------------------------------------------------------------------- #

@route("adyen", "POST", r"/v\d+/sessions")
def adyen_sessions(_b, _m):
    return 201, {"id": _id("CS"), "sessionData": "Ab02b4c"}


@route("adyen", "POST", r"/v\d+/paymentMethods")
def adyen_pm(_b, _m):
    return 200, {"paymentMethods": [{"type": "scheme"}]}


@route("adyen", "POST", r"/v\d+/payments")
def adyen_payments(_b, _m):
    return 200, {"pspReference": _id("PSP").upper(), "resultCode": "Authorised"}


@route("adyen", "POST", r"/v\d+/payments/(?P<psp>[^/]+)/(?P<op>captures|refunds|cancels)")
def adyen_mod(_b, _m):
    return 201, {"pspReference": _id("MOD").upper(), "status": "received"}


@route("adyen", "POST", r"/v\d+/payments/details")
def adyen_details(_b, _m):
    return 200, {"pspReference": _id("PSP").upper(), "resultCode": "Authorised"}


# -- Checkout.com -------------------------------------------------------------- #

@route("checkout_com", "POST", r"/tokens")
def cko_token(_b, _m):
    return 201, {"type": "card", "token": _id("tok")}


@route("checkout_com", "POST", r"/hosted-payments")
def cko_hosted(_b, _m):
    hosted = _id("hpp")
    return 201, {"id": hosted,
                 "_links": {"redirect": {"href": f"https://pay.example/{hosted}"}}}


@route("checkout_com", "GET", r"/hosted-payments/(?P<id>[^/]+)")
def cko_hosted_get(_b, m):
    return 200, {"id": m.group("id"), "status": "Captured"}


@route("checkout_com", "POST", r"/payments")
def cko_payment(body, _m):
    captured = body.get("capture") is not False
    return 201, {"id": _id("pay"), "status": "Captured" if captured else "Authorized",
                 "approved": True, "response_summary": "Approved",
                 "source": {"type": "card", "id": _id("src")}}


@route("checkout_com", "GET", r"/payments/(?P<id>[^/]+)")
def cko_payment_get(_b, m):
    return 200, {"id": m.group("id"), "status": "Captured"}


@route("checkout_com", "POST", r"/payments/(?P<id>[^/]+)/(?P<op>captures|refunds|voids)")
def cko_action(_b, _m):
    return 202, {"action_id": _id("act")}


# -- HyperPay ------------------------------------------------------------------ #

def _oppwa_ok(extra: Optional[Dict[str, Any]] = None, code: str = "000.100.110"):
    body: Dict[str, Any] = {"id": uuid.uuid4().hex,
                            "result": {"code": code, "description": "Request successfully processed"}}
    if extra:
        body.update(extra)
    return 200, body


@route("hyperpay", "POST", r"/v1/checkouts")
def hp_checkout(_b, _m):
    return _oppwa_ok(code="000.200.100")


@route("hyperpay", "GET", r"/v1/checkouts/(?P<id>[^/]+)/payment")
def hp_checkout_status(_b, _m):
    return _oppwa_ok()


@route("hyperpay", "POST", r"/v1/payments")
def hp_payment(body, _m):
    extra = {"registrationId": uuid.uuid4().hex} if body.get("createRegistration") else None
    return _oppwa_ok(extra)


@route("hyperpay", "POST", r"/v1/payments/(?P<id>[^/]+)")
def hp_backoffice(_b, _m):
    return _oppwa_ok()


@route("hyperpay", "POST", r"/v1/registrations/(?P<id>[^/]+)/payments")
def hp_registration(_b, _m):
    return _oppwa_ok()


# -- Moyasar -------------------------------------------------------------------- #

@route("moyasar", "POST", r"/v1/invoices")
def moyasar_invoice(_b, _m):
    inv = _id("inv")
    return 201, {"id": inv, "status": "initiated", "url": f"https://moyasar.example/{inv}"}


@route("moyasar", "GET", r"/v1/invoices/(?P<id>[^/]+)")
def moyasar_invoice_get(_b, m):
    return 200, {"id": m.group("id"), "status": "paid"}


@route("moyasar", "POST", r"/v1/tokens")
def moyasar_token(_b, _m):
    return 201, {"id": _id("token"), "status": "active"}


@route("moyasar", "POST", r"/v1/payments")
def moyasar_payment(body, _m):
    manual = body.get("source[manual]") == "true"
    return 201, {"id": _id("pmt"), "status": "authorized" if manual else "paid",
                 "source": {"type": "creditcard", "message": None}}


@route("moyasar", "GET", r"/v1/payments/(?P<id>[^/]+)")
def moyasar_payment_get(_b, m):
    return 200, {"id": m.group("id"), "status": "paid"}


@route("moyasar", "POST", r"/v1/payments/(?P<id>[^/]+)/(?P<op>capture|refund|void)")
def moyasar_action(_b, m):
    status = {"capture": "captured", "refund": "refunded", "void": "voided"}[m.group("op")]
    return 200, {"id": m.group("id"), "status": status}


# -- Server --------------------------------------------------------------------- #

class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this, small responses wait on a delayed ACK and add tens of milliseconds
    # of pure artefact to every mocked call.
    disable_nagle_algorithm = True

    def log_message(self, *_args: Any) -> None:
        pass

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if not raw:
            return {}
        if "json" in (self.headers.get("Content-Type") or "").lower():
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {"_raw": raw}
            return parsed if isinstance(parsed, dict) else {"_raw": parsed}
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        REQUEST_LOG.append((method, path))
        body = self._read_body()

        owner: Optional[str] = None
        prefix = re.match(r"/_(?P<owner>\w+)(?P<rest>/.*)?$", path)
        if prefix and prefix.group("owner") in OWNERS:
            owner = prefix.group("owner")
            path = prefix.group("rest") or "/"

        for route_owner, route_method, pattern, handler in ROUTES:
            if route_method != method or (owner is not None and route_owner != owner):
                continue
            match = pattern.match(path)
            if match:
                override = RESPONSE_OVERRIDES.get(handler.__name__)
                self._respond(*(override if override else handler(body, match)))
                return
        self._respond(404, {"error": "no mock route", "method": method, "path": path,
                            "owner": owner})

    def _respond(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:   # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


class MockGatewayServer:
    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def base_url_for(self, owner: str) -> str:
        if owner not in OWNERS:
            raise ValueError(f"unknown mock owner {owner!r}")
        return f"{self.base_url}/_{owner}"

    def __enter__(self) -> "MockGatewayServer":
        REQUEST_LOG.clear()
        RESPONSE_OVERRIDES.clear()
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    with MockGatewayServer(8099) as server:
        print(f"mock gateway on {server.base_url}")
        threading.Event().wait()
