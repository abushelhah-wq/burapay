"""
The instrumented HTTP client — the measurement layer everything else depends on.

Every outbound gateway call in the platform goes through :class:`InstrumentedClient`.
Nothing else is allowed to talk to a gateway, because a call that bypasses this class
is a call nobody timed.

What is measured
----------------
``duration_ms`` is a ``time.perf_counter()`` delta taken immediately before the
request is issued and immediately after the response body has been fully read. The
monotonic clock is used because it cannot jump backwards mid-measurement the way a
wall clock can (specification section 2); the wall-clock timestamps recorded beside
it exist for ordering and display, never for arithmetic.

Reading the body is inside the measurement on purpose. A gateway that returns headers
quickly and then dribbles the body out is slower than one that does not, and stopping
at the headers would hide exactly that.

What is *not* measured
----------------------
Serializing the request, persisting the result, and everything else this application
does sits outside the timer (section 51). ``app_overhead_ms`` on the transaction
records that separately, so gateway latency is never inflated by our own work.

Timed vs setup calls
--------------------
:meth:`call` is a measured call. :meth:`setup_call` is preparation a real merchant
would already have done — minting a token, creating a payment so there is something
to refund. Setup calls are recorded in full, with ``is_setup_call`` set, and excluded
from the reported API time. Without that split, a gateway that needs a tokenization
round trip to run a refund test looks slower at refunding than it is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.core.errors import ErrorCategory, category_for_http
from app.core.logging import get_logger
from app.core.sanitizer import sanitize_snippet, scrub_text
from app.models.enums import NormalizedOperation

logger = get_logger(__name__)


@dataclass
class CallMeasurement:
    """One outbound HTTP call, from just before the request to body-read complete."""

    sequence: int
    operation_name: str
    normalized_operation: NormalizedOperation
    endpoint: str
    http_method: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    #: Milliseconds, from the monotonic clock, kept at microsecond resolution.
    duration_ms: float = 0.0
    http_status: Optional[int] = None
    gateway_response_code: Optional[str] = None
    gateway_response_message: Optional[str] = None
    success: bool = False
    error_category: Optional[ErrorCategory] = None
    error_message: Optional[str] = None
    timed_out: bool = False
    is_setup_call: bool = False
    request_size_bytes: Optional[int] = None
    response_size_bytes: Optional[int] = None
    response_snippet: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "operation_name": self.operation_name,
            "normalized_operation": self.normalized_operation.value,
            "endpoint": self.endpoint,
            "http_method": self.http_method,
            "duration_ms": self.duration_ms,
            "http_status": self.http_status,
            "gateway_response_code": self.gateway_response_code,
            "gateway_response_message": self.gateway_response_message,
            "success": self.success,
            "error_category": self.error_category.value if self.error_category else None,
            "is_setup_call": self.is_setup_call,
        }


@dataclass
class InstrumentedClient:
    """An ``httpx.AsyncClient`` that records every call it makes.

    One instance per transaction. It owns the call log, so ``client.measurements`` is
    the complete record of what the gateway was asked and how long it took to answer.
    """

    gateway_code: str
    client: httpx.AsyncClient
    #: Secret literals to strip from any message before it is stored, in case a
    #: gateway echoes a key back inside an error string.
    known_secrets: List[str] = field(default_factory=list)
    measurements: List[CallMeasurement] = field(default_factory=list)
    _sequence: int = 0

    async def call(self, operation_name: str, method: str, url: str, *,
                   normalized: NormalizedOperation = NormalizedOperation.OTHER,
                   **kwargs: Any) -> httpx.Response:
        """A measured call. Counts toward the transaction's gateway API time."""
        return await self._request(operation_name, method, url, normalized=normalized,
                                   is_setup=False, **kwargs)

    async def setup_call(self, operation_name: str, method: str, url: str, *,
                         normalized: NormalizedOperation = NormalizedOperation.OTHER,
                         **kwargs: Any) -> httpx.Response:
        """A preparation call. Recorded in full, excluded from the reported API time."""
        return await self._request(operation_name, method, url, normalized=normalized,
                                   is_setup=True, **kwargs)

    @property
    def timed_measurements(self) -> List[CallMeasurement]:
        return [m for m in self.measurements if not m.is_setup_call]

    @property
    def gateway_api_time_ms(self) -> float:
        """Sum of the timed calls — the fair gateway-latency figure (section 39)."""
        return round(sum(m.duration_ms for m in self.timed_measurements), 3)

    def measurements_for(self, normalized: NormalizedOperation) -> List[CallMeasurement]:
        return [m for m in self.timed_measurements if m.normalized_operation is normalized]

    async def _request(self, operation_name: str, method: str, url: str, *,
                       normalized: NormalizedOperation, is_setup: bool,
                       **kwargs: Any) -> httpx.Response:
        self._sequence += 1
        record = CallMeasurement(
            sequence=self._sequence,
            operation_name=operation_name,
            normalized_operation=normalized,
            endpoint=url,
            http_method=method.upper(),
            started_at=datetime.now(timezone.utc),
            is_setup_call=is_setup,
        )

        started = time.perf_counter()
        try:
            response = await self.client.request(method.upper(), url, **kwargs)
            body = response.content          # inside the timer, on purpose
            elapsed_ms = (time.perf_counter() - started) * 1000.0
        except httpx.TimeoutException as exc:
            record.duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            record.completed_at = datetime.now(timezone.utc)
            record.timed_out = True
            record.error_category = ErrorCategory.TIMEOUT
            record.error_message = self._clean(f"timeout after {record.duration_ms:.0f} ms: {exc}")
            self.measurements.append(record)
            self._log(record)
            raise
        except httpx.HTTPError as exc:
            record.duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            record.completed_at = datetime.now(timezone.utc)
            record.error_category = ErrorCategory.NETWORK_ERROR
            record.error_message = self._clean(f"{type(exc).__name__}: {exc}")
            self.measurements.append(record)
            self._log(record)
            raise

        record.duration_ms = round(elapsed_ms, 3)
        record.completed_at = datetime.now(timezone.utc)
        record.http_status = response.status_code
        record.response_size_bytes = len(body)
        request_body = getattr(response.request, "content", b"") or b""
        record.request_size_bytes = len(request_body)
        record.success = response.is_success
        if not response.is_success:
            record.error_category = category_for_http(response.status_code)
            record.error_message = f"HTTP {response.status_code}"
        # The snippet is sanitized twice over: PAN/secret scrubbing, then truncation.
        # Request bodies are never stored, so a card sent to a gateway never lands here.
        record.response_snippet = self._clean(sanitize_snippet(response.text, 1500))

        self.measurements.append(record)
        self._log(record)
        return response

    def annotate_last(self, *, code: Optional[str] = None, message: Optional[str] = None,
                      success: Optional[bool] = None,
                      category: Optional[ErrorCategory] = None) -> None:
        """Attach the gateway's own outcome to the call that carried it.

        Needed because several gateways answer HTTP 200 with a decline in the body —
        Geidea's ``responseCode``, Adyen's ``resultCode``, OPPWA's ``result.code``.
        Only the adapter can read those, so it hands them back here.
        """
        if not self.measurements:
            return
        record = self.measurements[-1]
        if code is not None:
            record.gateway_response_code = str(code)
        if message is not None:
            record.gateway_response_message = self._clean(str(message))[:1000]
        if success is not None:
            record.success = success
        if category is not None:
            record.error_category = category

    def _clean(self, text: str) -> str:
        from app.core.sanitizer import redact_secret_values
        return redact_secret_values(scrub_text(text), self.known_secrets)

    def _log(self, record: CallMeasurement) -> None:
        logger.info(
            "gateway call",
            extra={"gateway": self.gateway_code, "operation": record.operation_name,
                   "normalized_operation": record.normalized_operation.value,
                   "duration_ms": record.duration_ms, "http_status": record.http_status,
                   "status": "ok" if record.success else "error",
                   "setup_call": record.is_setup_call})
