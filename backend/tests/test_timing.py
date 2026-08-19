"""
Tests for the timing layer.

The instrumented client is what every number in this platform ultimately comes from,
so these tests pin down the properties that matter: that a call is timed at all, that
the body read is inside the measurement, that setup calls are excluded from the
reported latency, and that a failure is still measured rather than lost.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.benchmarks.timeline import TransactionTimer
from app.core.errors import ErrorCategory
from app.gateways.http import InstrumentedClient
from app.models.enums import NormalizedOperation, TimelineEvent


def make_client(handler) -> InstrumentedClient:
    transport = httpx.MockTransport(handler)
    return InstrumentedClient(gateway_code="test",
                              client=httpx.AsyncClient(transport=transport))


class TestInstrumentedClient:
    async def test_measures_a_successful_call(self):
        client = make_client(lambda request: httpx.Response(200, json={"ok": True}))
        response = await client.call("op", "GET", "https://gateway.test/x",
                                     normalized=NormalizedOperation.STATUS_CHECK)
        assert response.status_code == 200

        record = client.measurements[0]
        assert record.sequence == 1
        assert record.duration_ms >= 0
        assert record.success is True
        assert record.normalized_operation is NormalizedOperation.STATUS_CHECK
        assert record.response_size_bytes and record.response_size_bytes > 0
        await client.client.aclose()

    async def test_body_read_is_inside_the_measurement(self):
        """A gateway that is slow to deliver the body must measure as slow."""
        async def slow_body(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"ok": True})

        client = make_client(slow_body)
        await client.call("slow", "GET", "https://gateway.test/slow")
        assert client.measurements[0].duration_ms >= 45
        await client.client.aclose()

    async def test_setup_calls_are_excluded_from_reported_latency(self):
        client = make_client(lambda request: httpx.Response(200, json={}))
        await client.setup_call("mint token", "POST", "https://gateway.test/tokens")
        await client.call("charge", "POST", "https://gateway.test/pay")

        assert len(client.measurements) == 2
        assert len(client.timed_measurements) == 1
        # The setup call is recorded in full but contributes nothing to the number
        # a gateway is judged on.
        assert client.gateway_api_time_ms == client.timed_measurements[0].duration_ms
        await client.client.aclose()

    async def test_error_status_is_recorded_and_categorised(self):
        client = make_client(lambda request: httpx.Response(500, text="boom"))
        await client.call("fail", "GET", "https://gateway.test/x")
        record = client.measurements[0]
        assert record.success is False
        assert record.error_category is ErrorCategory.GATEWAY_5XX
        assert record.duration_ms > 0          # a failure is still a measurement
        await client.client.aclose()

    async def test_timeout_is_measured_then_raised(self):
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = make_client(timeout)
        with pytest.raises(httpx.TimeoutException):
            await client.call("slow", "GET", "https://gateway.test/x")
        record = client.measurements[0]
        assert record.timed_out is True
        assert record.error_category is ErrorCategory.TIMEOUT
        await client.client.aclose()

    async def test_secrets_are_stripped_from_stored_snippets(self):
        client = InstrumentedClient(
            gateway_code="test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, text="bad key sk_live_supersecret123"))),
            known_secrets=["sk_live_supersecret123"])
        await client.call("op", "GET", "https://gateway.test/x")
        assert "supersecret" not in (client.measurements[0].response_snippet or "")
        await client.client.aclose()

    async def test_pan_in_a_response_is_masked_before_storage(self):
        client = make_client(
            lambda request: httpx.Response(200, text='{"card":"4111111111111111"}'))
        await client.call("op", "GET", "https://gateway.test/x")
        assert "4111111111111111" not in (client.measurements[0].response_snippet or "")
        await client.client.aclose()

    async def test_annotate_last_attaches_the_gateway_outcome(self):
        """A 200 carrying a decline must be recorded as a failure."""
        client = make_client(lambda request: httpx.Response(200, json={"responseCode": "123"}))
        await client.call("pay", "POST", "https://gateway.test/pay")
        client.annotate_last(code="123", message="Declined", success=False,
                             category=ErrorCategory.GATEWAY_DECLINE)
        record = client.measurements[0]
        assert record.gateway_response_code == "123"
        assert record.success is False
        assert record.error_category is ErrorCategory.GATEWAY_DECLINE
        await client.client.aclose()

    async def test_sequence_numbers_are_ordered(self):
        client = make_client(lambda request: httpx.Response(200, json={}))
        for _ in range(3):
            await client.call("op", "GET", "https://gateway.test/x")
        assert [m.sequence for m in client.measurements] == [1, 2, 3]
        await client.client.aclose()


class TestTransactionTimer:
    async def test_spans_measure_between_events(self):
        timer = TransactionTimer()
        timer.mark(TimelineEvent.THREE_DS_INITIATED)
        await asyncio.sleep(0.02)
        timer.mark(TimelineEvent.THREE_DS_COMPLETED)
        assert timer.three_ds_time_ms() >= 15

    def test_missing_events_produce_none_not_zero(self):
        """Section 9: a metric that could not be measured must not be presented."""
        timer = TransactionTimer()
        timer.mark(TimelineEvent.BENCHMARK_STARTED)
        assert timer.three_ds_time_ms() is None
        assert timer.redirect_time_ms() is None
        assert timer.customer_interaction_time_ms() is None

    def test_offsets_increase_monotonically(self):
        timer = TransactionTimer()
        for event in (TimelineEvent.BENCHMARK_STARTED, TimelineEvent.SESSION_REQUEST_SENT,
                      TimelineEvent.SESSION_RESPONSE_RECEIVED):
            timer.mark(event)
        offsets = [entry.offset_ms for entry in timer.entries]
        assert offsets == sorted(offsets)

    def test_summary_keeps_the_categories_separate(self):
        timer = TransactionTimer()
        timer.mark(TimelineEvent.BENCHMARK_STARTED)
        summary = timer.summary(gateway_api_time_ms=250.0, total_duration_ms=8000.0)
        # The whole point of section 39: a slow customer must not read as a slow API.
        assert summary["gateway_api_time_ms"] == 250.0
        assert summary["total_duration_ms"] == 8000.0
