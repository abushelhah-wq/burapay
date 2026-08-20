"""
MockPay — a local simulated gateway (specification section 49).

It exists so the platform can be exercised end to end before a single sandbox
credential arrives: the benchmark engine, the timeline, the statistics, the exports
and the comparison dashboard all work identically against it.

It is deliberately *not* an HTTP client pointed at a fake server. It sleeps for a
configured latency and returns a shaped response, which means MockPay timings measure
this application's own overhead and nothing else. That makes it useful for section 51
(how much does the platform itself add?) and useless for comparing real gateways —
which is why it is excluded from rankings and labelled a simulator everywhere it
appears.

Scenarios: success, decline, timeout, server_error, slow, three_ds.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import httpx

from app.core.errors import ErrorCategory, GatewayError
from app.gateways.base import (CredentialField, HealthProbe, HppSession, PaymentRequest,
                               PaymentGatewayAdapter, PaymentResult)
from app.gateways.http import CallMeasurement, InstrumentedClient
from app.models.enums import NormalizedOperation, TransactionStatus

SCENARIOS = ("success", "decline", "timeout", "server_error", "slow", "three_ds",
             "three_ds_challenge")


class MockPayAdapter(PaymentGatewayAdapter):
    code = "mockpay"
    display_name = "MockPay (simulator)"
    supports_hpp = True
    supports_direct = True
    supported_currencies = ("SAR", "USD", "AED", "EUR", "GBP")
    docs_url = ""

    credential_fields = (
        CredentialField("scenario", "Scenario", required=False,
                        default="success", choices=SCENARIOS,
                        help_text="Which outcome to simulate."),
        CredentialField("latency_ms", "Simulated latency per call (ms)", required=False,
                        default="250",
                        help_text="Applied to every simulated API call. 100 / 250 / 500 / 1000 are useful values."),
        CredentialField("jitter_ms", "Latency jitter (± ms)", required=False, default="40",
                        help_text="Random spread, so percentile maths has a distribution to work on."),
    )

    notes = (
        "MockPay is a simulator, not a gateway. Its timings measure this platform's "
        "own overhead, so it is excluded from gateway rankings.",
        "Use it to validate the benchmark engine, the timeline and the exports before "
        "real sandbox credentials are available.",
    )

    documented_calls = {
        "hpp": "2 (create session + confirm)",
        "direct": "2 (payment + status), 3 with a 3DS scenario",
    }

    # -- configuration ---------------------------------------------------- #

    def is_configured(self) -> bool:
        return True          # nothing to configure; it always works

    def missing_credentials(self):
        return []

    @property
    def scenario(self) -> str:
        return (self.get("scenario") or "success").lower()

    @property
    def latency(self) -> float:
        try:
            return max(0.0, float(self.get("latency_ms") or 250)) / 1000.0
        except ValueError:
            return 0.25

    @property
    def jitter(self) -> float:
        try:
            return max(0.0, float(self.get("jitter_ms") or 0)) / 1000.0
        except ValueError:
            return 0.0

    # -- simulated calls -------------------------------------------------- #

    async def _simulate(self, client: InstrumentedClient, operation: str,
                        normalized: NormalizedOperation, *, path: str,
                        is_setup: bool = False) -> Dict[str, Any]:
        """Sleep for the configured latency and record a measurement by hand.

        The record is built directly rather than through ``InstrumentedClient.call``
        because there is no socket involved; the timing still comes from
        ``perf_counter`` so it is produced exactly the way a real call's is.
        """
        client._sequence += 1                                     # noqa: SLF001
        record = CallMeasurement(
            sequence=client._sequence,                            # noqa: SLF001
            operation_name=operation,
            normalized_operation=normalized,
            endpoint=f"mockpay://{path}",
            http_method="POST",
            started_at=datetime.now(timezone.utc),
            is_setup_call=is_setup,
        )
        # No socket, so there is no real body — but the log page exists to show what
        # went out, and a simulator that shows nothing there cannot demonstrate it.
        # Labelled as simulated so it is never mistaken for a captured request.
        record.request_snippet = json.dumps({
            "simulated": True, "operation": operation, "scenario": self.scenario,
            "note": "MockPay makes no HTTP call; this describes what it stood in for.",
        })
        started = time.perf_counter()
        delay = self.latency + (random.uniform(-self.jitter, self.jitter) if self.jitter else 0.0)
        if self.scenario == "slow":
            delay *= 4
        await asyncio.sleep(max(0.0, delay))
        record.duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        record.completed_at = datetime.now(timezone.utc)

        if self.scenario == "timeout":
            record.timed_out = True
            record.error_category = ErrorCategory.TIMEOUT
            record.error_message = "simulated timeout"
            client.measurements.append(record)
            raise httpx.ReadTimeout("MockPay simulated timeout")
        if self.scenario == "server_error":
            record.http_status = 500
            record.error_category = ErrorCategory.GATEWAY_5XX
            record.error_message = "HTTP 500"
            record.gateway_response_code = "500"
            client.measurements.append(record)
            raise GatewayError("MockPay simulated 500", http_status=500,
                               gateway_code="500")

        declined = self.scenario == "decline"
        record.http_status = 200
        record.success = not declined
        record.gateway_response_code = "05" if declined else "00"
        record.gateway_response_message = "Do not honour" if declined else "Approved"
        if declined:
            record.error_category = ErrorCategory.GATEWAY_DECLINE
        record.response_snippet = json.dumps({
            "simulated": True, "responseCode": record.gateway_response_code,
            "responseMessage": record.gateway_response_message})
        client.measurements.append(record)

        return {"code": record.gateway_response_code,
                "message": record.gateway_response_message,
                "declined": declined}

    # -- flows ------------------------------------------------------------ #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        await self._simulate(client, "POST /mock/sessions",
                             NormalizedOperation.SESSION_CREATION, path="sessions")
        session_id = f"mock_sess_{request.reference}"
        return HppSession(
            redirect_url=f"/mock-checkout/{session_id}",
            gateway_reference=session_id,
            context={"session_id": session_id},
            mode="redirect")

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        body = await self._simulate(client, "GET /mock/sessions/{id}",
                                    NormalizedOperation.STATUS_CHECK, path="sessions/get")
        cancelled = str(params.get("result", "")).lower() == "cancelled"
        if cancelled:
            return PaymentResult(TransactionStatus.CANCELLED, context.get("session_id"),
                                 "cancelled", "Customer cancelled on the hosted page")
        status = TransactionStatus.DECLINED if body["declined"] else TransactionStatus.SUCCESS
        return PaymentResult(status, context.get("session_id"), body["code"], body["message"])

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        await self._simulate(client, "POST /mock/payments",
                             NormalizedOperation.PAYMENT_INITIATION, path="payments")
        challenge = self.scenario == "three_ds_challenge"
        three_ds = challenge or self.scenario == "three_ds"
        if three_ds:
            await self._simulate(client, "POST /mock/3ds/authenticate",
                                 NormalizedOperation.AUTHENTICATION, path="3ds")
        if challenge:
            # A simulated issuer that wants the cardholder. It sends them straight back
            # rather than asking anything, which is the point: what is being exercised
            # is the pause and the resume, not the OTP form. The gap between the two
            # legs is the customer's time and is measured as such.
            return PaymentResult(
                TransactionStatus.PENDING, f"mock_pay_{request.reference}",
                "3ds", "simulated 3DS challenge; awaiting the cardholder",
                three_ds_required=True, requires_customer_action=True,
                action_url=request.return_url or None,
                context={"reference": request.reference})

        body = await self._simulate(client, "POST /mock/payments/authorize",
                                    NormalizedOperation.AUTHORIZATION, path="authorize")
        status = TransactionStatus.DECLINED if body["declined"] else TransactionStatus.SUCCESS
        return PaymentResult(status, f"mock_pay_{request.reference}",
                             body["code"], body["message"], three_ds_required=three_ds)

    async def complete_direct_payment(self, client: InstrumentedClient,
                                      request: PaymentRequest,
                                      context: Mapping[str, Any],
                                      params: Mapping[str, str]) -> PaymentResult:
        """Authorize once the simulated cardholder is back. One call, and only it timed."""
        body = await self._simulate(client, "POST /mock/payments/authorize",
                                    NormalizedOperation.AUTHORIZATION, path="authorize")
        status = TransactionStatus.DECLINED if body["declined"] else TransactionStatus.SUCCESS
        reference = context.get("reference") or request.reference
        return PaymentResult(status, f"mock_pay_{reference}", body["code"],
                             body["message"], three_ds_required=True)

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        body = await self._simulate(client, "GET /mock/payments/{id}",
                                    NormalizedOperation.STATUS_CHECK, path="status")
        status = TransactionStatus.DECLINED if body["declined"] else TransactionStatus.SUCCESS
        return PaymentResult(status, payment_id, body["code"], body["message"])

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        body = await self._simulate(client, "POST /mock/refunds",
                                    NormalizedOperation.REFUND, path="refunds")
        status = TransactionStatus.FAILED if body["declined"] else TransactionStatus.SUCCESS
        return PaymentResult(status, payment_id, body["code"], body["message"])

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        return {
            "event_type": payload.get("event", "mock.payment.completed"),
            "reference": payload.get("reference"),
            "gateway_reference": payload.get("id"),
            "status": payload.get("status"),
            # No signature scheme exists, so this stays unknown rather than "verified".
            "signature_verified": None,
            "payload": payload,
        }

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        started = time.perf_counter()
        await asyncio.sleep(self.latency / 5)
        return HealthProbe(available=True,
                           response_time_ms=round((time.perf_counter() - started) * 1000, 3),
                           http_status=200, detail="simulator; always available")
