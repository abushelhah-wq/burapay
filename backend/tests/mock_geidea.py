"""
A stand-in Geidea, implementing the shapes recorded in this repository's
documentation study.

Scope, stated plainly: this proves the adapter's **sequencing, call counting,
state machine, redaction and error classification**. It does not prove Geidea
accepts these requests -- the mock was written from the same material the
adapter was, so agreement between them says nothing about the real API. See
``app/gateways/geidea/endpoints.py`` for what is and is not verified.

The mock is deliberately strict about the things the adapter is responsible
for: it rejects a request with no Authorization header, requires the fields the
adapter claims to send, and records every call so a test can assert the exact
sequence rather than only the final status.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI, Request, Response

SUCCESS = "000"
#: A non-success code used for declines. The real code table is undocumented
#: here, so this is the mock's own value and no adapter logic keys on it.
DECLINE = "999"


@dataclass
class Order:
    order_id: str
    merchant_reference: str
    amount: str
    currency: str
    status: str
    captured: float = 0.0
    refunded: float = 0.0
    operation: str = "Pay"


@dataclass
class MockState:
    """Mutable server state, reset between tests."""

    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    #: Test knobs.
    decline_pay: bool = False
    frictionless: bool = False
    delay_seconds: dict[str, float] = field(default_factory=dict)
    fail_status: dict[str, int] = field(default_factory=dict)
    garbage_body: set[str] = field(default_factory=set)
    capability_error: bool = False

    def reset(self) -> None:
        self.sessions.clear()
        self.orders.clear()
        self.calls.clear()
        self.decline_pay = False
        self.frictionless = False
        self.delay_seconds = {}
        self.fail_status = {}
        self.garbage_body = set()
        self.capability_error = False

    def steps(self) -> list[str]:
        return [call["step"] for call in self.calls]


state = MockState()


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"responseCode": SUCCESS, "responseMessage": "Success", **payload}


def _order_payload(order: Order) -> dict[str, Any]:
    return {
        "order": {
            "orderId": order.order_id,
            "merchantReferenceId": order.merchant_reference,
            "amount": order.amount,
            "currency": order.currency,
            "status": order.status,
            "transactions": [{"transactionId": f"txn_{order.order_id}"}],
            "paymentMethod": {"brand": "VISA", "maskedCardNumber": "411111******1111"},
        }
    }


def build_mock_app() -> FastAPI:
    app = FastAPI(title="Mock Geidea")

    async def record(step: str, request: Request) -> Optional[dict[str, Any]]:
        body: Optional[dict[str, Any]] = None
        raw = await request.body()
        if raw:
            import json

            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        state.calls.append(
            {
                "step": step,
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        return body

    def _auth_ok(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        return ":" in decoded and all(part for part in decoded.split(":", 1))

    def _maybe_fail(step: str) -> Optional[Response]:
        """Apply the test knobs: latency, HTTP failures, unparseable bodies."""
        delay = state.delay_seconds.get(step)
        if delay:
            time.sleep(delay)
        if step in state.garbage_body:
            return Response(content="<html>not json</html>", status_code=200,
                            media_type="text/html")
        code = state.fail_status.get(step)
        if code:
            return Response(content='{"responseCode":"500"}', status_code=code,
                            media_type="application/json")
        return None

    @app.post("/payment-intent/api/v2/direct/session")
    async def create_session(request: Request):
        body = await record("create_session", request)
        failure = _maybe_fail("create_session")
        if failure is not None:
            return failure
        if not _auth_ok(request):
            return Response(content='{"responseCode":"401"}', status_code=401,
                            media_type="application/json")

        body = body or {}
        # Assert the adapter sends what it says it sends.
        for required in ("amount", "currency", "merchantReferenceId", "timestamp", "signature"):
            if required not in body:
                return {"responseCode": DECLINE,
                        "responseMessage": f"missing {required}"}

        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        state.sessions[session_id] = dict(body)
        return _ok({
            "session": {
                "id": session_id,
                "redirectUrl": f"https://mock-geidea.test/pay/{session_id}",
            }
        })

    @app.post("/payment-intent/api/v2/direct/session/saveCard")
    async def save_card_session(request: Request):
        body = await record("save_card_session", request)
        failure = _maybe_fail("save_card_session")
        if failure is not None:
            return failure
        if not _auth_ok(request):
            return Response(content='{"responseCode":"401"}', status_code=401,
                            media_type="application/json")
        body = body or {}
        for required in ("currency", "merchantReferenceId", "signature", "timeStamp"):
            if required not in body:
                return {"responseCode": DECLINE,
                        "responseMessage": f"missing {required}"}
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        state.sessions[session_id] = dict(body)
        return _ok({
            "session": {
                "id": session_id,
                "amount": 1,
                "currency": body.get("currency"),
                "status": "Initiated",
                "paymentOperation": "SaveCard",
                "cardOnFile": True,
                "cofAgreement": body.get("cofAgreement"),
                "tokenId": None,
            }
        })

    @app.post("/pgw/api/v2/direct/pay/token")
    async def pay_with_token(request: Request):
        body = await record("pay_with_token", request) or {}
        failure = _maybe_fail("pay_with_token")
        if failure is not None:
            return failure
        # Assert the adapter sends exactly what the MIT doc specifies,
        # including the lowercase-i "sessionid" its sample uses.
        for required in ("sessionid", "initiatedBy", "agreementId",
                         "agreementType", "signature"):
            if required not in body:
                return {"responseCode": DECLINE,
                        "responseMessage": f"missing {required}"}
        if body.get("initiatedBy") != "Merchant":
            return {"responseCode": DECLINE,
                    "responseMessage": "initiatedBy must be Merchant for MIT"}

        session = state.sessions.get(str(body.get("sessionid")), {})
        order = Order(
            order_id=f"ord_{uuid.uuid4().hex[:16]}",
            merchant_reference=str(session.get("merchantReferenceId", "")),
            amount=str(session.get("amount", "0")),
            currency=str(session.get("currency", "SAR")),
            status="Paid",
            operation="Pay",
        )
        order.captured = float(order.amount or 0)
        state.orders[order.order_id] = order
        return _ok(_order_payload(order))

    @app.post("/pgw/api/v6/direct/authenticate/initiate")
    async def initiate(request: Request):
        await record("initiate_authentication", request)
        failure = _maybe_fail("initiate_authentication")
        if failure is not None:
            return failure
        return _ok({
            "threeDSecureId": f"3ds_{uuid.uuid4().hex[:8]}",
            "gatewayDecision": (
                "frictionless - not enrolled" if state.frictionless else "challenge required"
            ),
        })

    @app.post("/pgw/api/v6/direct/authenticate/payer")
    async def payer(request: Request):
        await record("authenticate_payer", request)
        failure = _maybe_fail("authenticate_payer")
        if failure is not None:
            return failure
        return _ok({"authenticationStatus": "AUTHENTICATED"})

    @app.post("/pgw/api/v2/direct/pay")
    async def pay(request: Request):
        body = await record("pay", request) or {}
        failure = _maybe_fail("pay")
        if failure is not None:
            return failure

        if state.capability_error:
            return {
                "responseCode": DECLINE,
                "responseMessage": "Operation not enabled for this merchant",
                "detailedResponseMessage": "Pre-authorization is not enabled for this merchant account.",
            }
        if state.decline_pay:
            return {"responseCode": DECLINE, "responseMessage": "Declined",
                    "detailedResponseMessage": "Insufficient funds"}

        session = state.sessions.get(str(body.get("sessionId")), {})
        operation = str(body.get("paymentOperation") or "Pay")
        order = Order(
            order_id=f"ord_{uuid.uuid4().hex[:16]}",
            merchant_reference=str(session.get("merchantReferenceId", "")),
            amount=str(session.get("amount", "0")),
            currency=str(session.get("currency", "SAR")),
            status="Authorized" if operation == "Authorize" else "Paid",
            operation=operation,
        )
        state.orders[order.order_id] = order
        return _ok(_order_payload(order))

    @app.get("/pgw/api/v1/direct/order/{order_id}")
    async def fetch(order_id: str, request: Request):
        await record("order_query", request)
        failure = _maybe_fail("order_query")
        if failure is not None:
            return failure
        order = state.orders.get(order_id)
        if order is None:
            return {"responseCode": DECLINE, "responseMessage": "Order not found"}
        return _ok(_order_payload(order))

    @app.post("/pgw/api/v1/direct/capture")
    async def capture(request: Request):
        body = await record("capture", request) or {}
        failure = _maybe_fail("capture")
        if failure is not None:
            return failure
        order = state.orders.get(str(body.get("orderId")))
        if order is None:
            return {"responseCode": DECLINE, "responseMessage": "Order not found"}
        amount = float(body.get("captureAmount") or order.amount)
        if order.captured + amount > float(order.amount) + 1e-9:
            return {"responseCode": DECLINE,
                    "responseMessage": "Capture exceeds authorized amount"}
        order.captured += amount
        order.status = "Captured" if order.captured >= float(order.amount) else "PartiallyCaptured"
        return _ok(_order_payload(order))

    @app.post("/pgw/api/v2/direct/refund")
    async def refund(request: Request):
        body = await record("refund", request) or {}
        failure = _maybe_fail("refund")
        if failure is not None:
            return failure
        order = state.orders.get(str(body.get("orderId")))
        if order is None:
            return {"responseCode": DECLINE, "responseMessage": "Order not found"}
        if "signature" not in body:
            return {"responseCode": DECLINE, "responseMessage": "missing signature"}
        amount = float(body.get("refundAmount") or 0)
        refundable = (order.captured or float(order.amount)) - order.refunded
        if amount > refundable + 1e-9:
            return {"responseCode": DECLINE, "responseMessage": "Refund exceeds balance"}
        order.refunded += amount
        order.status = "Refunded"
        return _ok(_order_payload(order))

    @app.post("/pgw/api/v3/direct/void")
    async def void(request: Request):
        body = await record("void", request) or {}
        failure = _maybe_fail("void")
        if failure is not None:
            return failure
        order = state.orders.get(str(body.get("orderId")))
        if order is None:
            return {"responseCode": DECLINE, "responseMessage": "Order not found"}
        if order.captured:
            return {"responseCode": DECLINE, "responseMessage": "Already captured"}
        order.status = "Voided"
        return _ok(_order_payload(order))

    return app
