"""
A local stand-in for all six gateways.

This is not a simulator - it does not model authorisation logic, and its latency
figures mean nothing. Its only job is to answer each gateway module's documented
endpoints with a response of the right *shape*, so the flow code (request bodies,
response parsing, call sequencing, error handling) can be exercised end to end
without sandbox credentials.

Run standalone for a manual poke:

    python tests/mock_gateway.py --port 8099
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, urlparse

Handler = Callable[[Dict[str, Any], re.Match], Tuple[int, Dict[str, Any]]]

#: Every request the server saw, as (method, path). Tests assert against this.
REQUEST_LOG: List[Tuple[str, str]] = []

#: handler name -> (status, body). Lets a test bend one endpoint's response without
#: touching the route table. Cleared by the server on entry.
RESPONSE_OVERRIDES: Dict[str, Tuple[int, Dict[str, Any]]] = {}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# Route table - (method, path regex) -> handler
# --------------------------------------------------------------------------- #

#: (owner, method, compiled path, handler). ``owner`` disambiguates the paths that
#: two gateways genuinely share - HyperPay and Moyasar both serve POST /v1/payments,
#: so the client picks a side with a /_<owner> base-URL prefix.
ROUTES: List[Tuple[str, str, Pattern[str], Handler]] = []

OWNERS = ("stripe", "adyen", "checkout_com", "hyperpay", "moyasar", "geidea")


def route(owner: str, method: str, pattern: str) -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        ROUTES.append((owner, method, re.compile(f"^{pattern}$"), func))
        return func
    return register


# -- Stripe ----------------------------------------------------------------- #

@route("stripe", "POST", r"/v1/checkout/sessions")
def stripe_create_session(body, _m):
    return 200, {"id": _id("cs"), "object": "checkout.session",
                 "url": "https://checkout.stripe.com/pay/cs_test",
                 "status": "open", "payment_status": "unpaid"}


@route("stripe", "GET", r"/v1/checkout/sessions/(?P<id>[^/]+)")
def stripe_get_session(_body, m):
    return 200, {"id": m.group("id"), "status": "open", "payment_status": "unpaid"}


@route("stripe", "POST", r"/v1/payment_methods")
def stripe_create_pm(_body, _m):
    return 200, {"id": _id("pm"), "object": "payment_method", "type": "card"}


@route("stripe", "POST", r"/v1/payment_methods/(?P<id>[^/]+)/attach")
def stripe_attach_pm(_body, m):
    return 200, {"id": m.group("id"), "object": "payment_method"}


@route("stripe", "POST", r"/v1/customers")
def stripe_create_customer(_body, _m):
    return 200, {"id": _id("cus"), "object": "customer"}


@route("stripe", "POST", r"/v1/payment_intents")
def stripe_create_pi(body, _m):
    manual = body.get("capture_method") == "manual"
    return 200, {"id": _id("pi"), "object": "payment_intent",
                 "status": "requires_capture" if manual else "succeeded"}


@route("stripe", "POST", r"/v1/payment_intents/(?P<id>[^/]+)/capture")
def stripe_capture(_body, m):
    return 200, {"id": m.group("id"), "status": "succeeded"}


@route("stripe", "POST", r"/v1/payment_intents/(?P<id>[^/]+)/cancel")
def stripe_cancel(_body, m):
    return 200, {"id": m.group("id"), "status": "canceled"}


@route("stripe", "POST", r"/v1/refunds")
def stripe_refund(_body, _m):
    return 200, {"id": _id("re"), "status": "succeeded"}


# -- Adyen ------------------------------------------------------------------ #

@route("adyen", "POST", r"/v\d+/sessions")
def adyen_sessions(_body, _m):
    return 201, {"id": _id("CS"), "sessionData": "Ab02b4c...", "expiresAt": "2030-01-01T00:00:00Z"}


@route("adyen", "POST", r"/v\d+/paymentMethods")
def adyen_payment_methods(_body, _m):
    return 200, {"paymentMethods": [{"type": "scheme", "name": "Cards"}]}


@route("adyen", "POST", r"/v\d+/payments")
def adyen_payments(_body, _m):
    return 200, {"pspReference": _id("PSP").upper(), "resultCode": "Authorised",
                 "merchantReference": "ref"}


@route("adyen", "POST", r"/v\d+/payments/(?P<psp>[^/]+)/(?P<op>captures|refunds|cancels)")
def adyen_modification(_body, m):
    return 201, {"pspReference": _id("MOD").upper(), "status": "received",
                 "paymentPspReference": m.group("psp")}


@route("adyen", "POST", r"/v\d+/payments/details")
def adyen_details(_body, _m):
    return 200, {"pspReference": _id("PSP").upper(), "resultCode": "Authorised"}


# -- Checkout.com ----------------------------------------------------------- #

@route("checkout_com", "POST", r"/tokens")
def cko_token(_body, _m):
    return 201, {"type": "card", "token": _id("tok"), "expires_on": "2030-01-01T00:00:00Z"}


@route("checkout_com", "POST", r"/hosted-payments")
def cko_hosted(_body, _m):
    hosted_id = _id("hpp")
    return 201, {"id": hosted_id, "reference": "ref",
                 "_links": {"redirect": {"href": f"https://pay.sandbox.checkout.com/{hosted_id}"}}}


@route("checkout_com", "GET", r"/hosted-payments/(?P<id>[^/]+)")
def cko_hosted_get(_body, m):
    return 200, {"id": m.group("id"), "status": "Payment Pending"}


@route("checkout_com", "POST", r"/payments")
def cko_payment(body, _m):
    captured = body.get("capture") is not False
    return 201, {"id": _id("pay"), "status": "Captured" if captured else "Authorized",
                 "approved": True, "response_summary": "Approved",
                 "source": {"type": "card", "id": _id("src")}}


@route("checkout_com", "GET", r"/payments/(?P<id>[^/]+)")
def cko_payment_get(_body, m):
    return 200, {"id": m.group("id"), "status": "Captured", "approved": True}


@route("checkout_com", "POST", r"/payments/(?P<id>[^/]+)/(?P<op>captures|refunds|voids)")
def cko_action(_body, _m):
    return 202, {"action_id": _id("act"), "reference": "ref"}


# -- HyperPay / OPPWA -------------------------------------------------------- #

def _oppwa_ok(extra: Optional[Dict[str, Any]] = None, code: str = "000.100.110"):
    body = {"id": _id("hp").replace("_", ""),
            "result": {"code": code, "description": "Request successfully processed"},
            "timestamp": "2026-08-19 10:00:00+0000"}
    if extra:
        body.update(extra)
    return 200, body


@route("hyperpay", "POST", r"/v1/checkouts")
def hp_checkout(_body, _m):
    return _oppwa_ok(code="000.200.100")


@route("hyperpay", "GET", r"/v1/checkouts/(?P<id>[^/]+)/payment")
def hp_checkout_status(_body, _m):
    return _oppwa_ok()


@route("hyperpay", "POST", r"/v1/payments")
def hp_payment(body, _m):
    extra = {"registrationId": _id("reg").replace("_", "")} \
        if body.get("createRegistration") else None
    return _oppwa_ok(extra)


@route("hyperpay", "POST", r"/v1/payments/(?P<id>[^/]+)")
def hp_backoffice(_body, _m):
    return _oppwa_ok()


@route("hyperpay", "POST", r"/v1/registrations/(?P<id>[^/]+)/payments")
def hp_registration_payment(_body, _m):
    return _oppwa_ok()


# -- Moyasar ---------------------------------------------------------------- #

@route("moyasar", "POST", r"/v1/invoices")
def moyasar_invoice(_body, _m):
    return 201, {"id": _id("inv"), "status": "initiated",
                 "url": "https://moyasar.com/invoices/inv_test"}


@route("moyasar", "GET", r"/v1/invoices/(?P<id>[^/]+)")
def moyasar_invoice_get(_body, m):
    return 200, {"id": m.group("id"), "status": "initiated"}


@route("moyasar", "POST", r"/v1/tokens")
def moyasar_token(_body, _m):
    return 201, {"id": _id("token"), "status": "active"}


@route("moyasar", "POST", r"/v1/payments")
def moyasar_payment(body, _m):
    manual = body.get("source[manual]") == "true"
    return 201, {"id": _id("pmt"), "status": "authorized" if manual else "paid",
                 "source": {"type": "creditcard", "message": None}}


@route("moyasar", "GET", r"/v1/payments/(?P<id>[^/]+)")
def moyasar_payment_get(_body, m):
    return 200, {"id": m.group("id"), "status": "paid"}


@route("moyasar", "POST", r"/v1/payments/(?P<id>[^/]+)/(?P<op>capture|refund|void)")
def moyasar_action(_body, m):
    status = {"capture": "captured", "refund": "refunded", "void": "voided"}[m.group("op")]
    return 200, {"id": m.group("id"), "status": status}


# -- Geidea ------------------------------------------------------------------ #

def _geidea_ok(extra: Optional[Dict[str, Any]] = None):
    body: Dict[str, Any] = {"responseCode": "000", "responseMessage": "Success",
                            "detailedResponseCode": "000",
                            "detailedResponseMessage": "Success"}
    if extra:
        body.update(extra)
    return 200, body


@route("geidea", "POST", r"/payment-intent/api/v2/direct/session")
def geidea_session(_body, _m):
    return _geidea_ok({"session": {"id": _id("sess"), "status": "Initiated"}})


@route("geidea", "POST", r"/pgw/api/v6/direct/authenticate/initiate")
def geidea_initiate(_body, _m):
    return _geidea_ok({"threeDSecureId": _id("tds"), "enrollmentStatus": "Enrolled"})


@route("geidea", "POST", r"/pgw/api/v6/direct/authenticate/payer")
def geidea_payer(_body, _m):
    return _geidea_ok({"authenticationStatus": "Authenticated"})


@route("geidea", "POST", r"/pgw/api/v2/direct/pay")
def geidea_pay(_body, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Success"}})


@route("geidea", "POST", r"/pgw/api/v2/direct/pay/token")
def geidea_pay_token(_body, _m):
    return _geidea_ok({"order": {"orderId": _id("ord"), "status": "Success"}})


@route("geidea", "GET", r"/pgw/api/v1/direct/order/(?P<id>[^/]+)")
def geidea_order(_body, m):
    return _geidea_ok({"order": {"orderId": m.group("id"), "status": "Success"}})


@route("geidea", "POST", r"/pgw/api/v1/direct/capture")
def geidea_capture(_body, _m):
    return _geidea_ok({"order": {"status": "Captured"}})


@route("geidea", "POST", r"/pgw/api/v2/direct/refund")
def geidea_refund(_body, _m):
    return _geidea_ok({"order": {"status": "Refunded"}})


@route("geidea", "POST", r"/pgw/api/v3/direct/void")
def geidea_void(_body, _m):
    return _geidea_ok({"order": {"status": "Voided"}})


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this, small responses sit in the send buffer waiting on a delayed ACK,
    # which adds tens of milliseconds of pure artefact to every mocked call.
    disable_nagle_algorithm = True

    def log_message(self, *_args: Any) -> None:  # keep test output readable
        pass

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        content_type = (self.headers.get("Content-Type") or "").lower()
        if not raw:
            return {}
        if "json" in content_type:
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
            if route_method != method:
                continue
            if owner is not None and route_owner != owner:
                continue
            match = pattern.match(path)
            if match:
                override = RESPONSE_OVERRIDES.get(handler.__name__)
                if override is not None:
                    self._respond(*override)
                    return
                status, payload = handler(body, match)
                self._respond(status, payload)
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

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


class MockGatewayServer:
    """Context manager that runs the mock on an ephemeral port."""

    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def base_url_for(self, owner: str) -> str:
        """Base URL scoped to one gateway's route set."""
        if owner not in OWNERS:
            raise ValueError(f"unknown mock owner {owner!r}; known: {', '.join(OWNERS)}")
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
    import argparse

    parser = argparse.ArgumentParser(description="Run the mock gateway standalone")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    with MockGatewayServer(args.port) as server:
        print(f"mock gateway listening on {server.base_url} (ctrl-c to stop)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
