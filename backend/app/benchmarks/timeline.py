"""
The transaction timeline and the timing breakdown (specification sections 8, 9, 39).

One :class:`TransactionTimer` per transaction. It owns the monotonic clock for that
transaction: every event offset and every derived duration comes from the same
``time.perf_counter()`` origin, so the numbers are internally consistent even if the
system clock is adjusted mid-flow.

The breakdown exists because of section 39: a customer who takes fifteen seconds to
type an OTP must not make the gateway's API look like it has fifteen seconds of
latency. So four different things are computed and never conflated:

``gateway_api_time_ms``
    Sum of the timed API calls. The gateway's actual latency contribution.
``three_ds_time_ms``
    Between 3DS initiation and completion — issuer plus customer, not the gateway.
``customer_interaction_time_ms``
    Time the customer spent on the hosted page. Ours to record, nobody's to blame.
``total_duration_ms``
    Wall time from start to final state. Includes all of the above.

A duration whose bracketing events did not both occur is left ``None``. Section 9 is
explicit: do not present a metric that cannot be reliably measured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.enums import TimelineEvent


@dataclass
class TimelineEntry:
    event_type: TimelineEvent
    #: Milliseconds since the benchmark started, from the monotonic clock.
    offset_ms: float
    timestamp: datetime
    label: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class TransactionTimer:
    """Monotonic clock plus event log for one transaction."""

    def __init__(self) -> None:
        self._origin = time.perf_counter()
        self.started_at = datetime.now(timezone.utc)
        self.entries: List[TimelineEntry] = []
        #: Accumulated application overhead — our own work, excluded from gateway time.
        self.app_overhead_ms: float = 0.0

    # -- recording -------------------------------------------------------- #

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._origin) * 1000.0, 3)

    def mark(self, event: TimelineEvent, label: str = "",
             **metadata: Any) -> TimelineEntry:
        entry = TimelineEntry(event_type=event, offset_ms=self.elapsed_ms(),
                              timestamp=datetime.now(timezone.utc), label=label,
                              metadata=metadata)
        self.entries.append(entry)
        return entry

    def mark_at(self, event: TimelineEvent, offset_ms: float, *,
                timestamp: Optional[datetime] = None, label: str = "",
                **metadata: Any) -> TimelineEntry:
        """Record an event that happened at a known offset.

        Used when resuming a two-legged HPP flow: the second leg runs in a different
        process invocation, so its offsets are computed from the persisted start time
        rather than from a fresh monotonic origin.
        """
        entry = TimelineEntry(event_type=event, offset_ms=round(offset_ms, 3),
                              timestamp=timestamp or datetime.now(timezone.utc),
                              label=label, metadata=metadata)
        self.entries.append(entry)
        return entry

    def add_overhead(self, milliseconds: float) -> None:
        self.app_overhead_ms = round(self.app_overhead_ms + milliseconds, 3)

    # -- reading ---------------------------------------------------------- #

    def first(self, event: TimelineEvent) -> Optional[TimelineEntry]:
        return next((e for e in self.entries if e.event_type is event), None)

    def last(self, event: TimelineEvent) -> Optional[TimelineEntry]:
        return next((e for e in reversed(self.entries) if e.event_type is event), None)

    def span(self, start: TimelineEvent, end: TimelineEvent) -> Optional[float]:
        """Milliseconds between two events, or ``None`` if either did not occur."""
        first, last = self.first(start), self.last(end)
        if first is None or last is None:
            return None
        delta = last.offset_ms - first.offset_ms
        return round(delta, 3) if delta >= 0 else None

    # -- derived durations ------------------------------------------------ #

    def three_ds_time_ms(self) -> Optional[float]:
        return self.span(TimelineEvent.THREE_DS_INITIATED, TimelineEvent.THREE_DS_COMPLETED)

    def redirect_time_ms(self) -> Optional[float]:
        return self.span(TimelineEvent.REDIRECT_INITIATED, TimelineEvent.HOSTED_PAGE_LOADED)

    def customer_interaction_time_ms(self) -> Optional[float]:
        """From the hosted page being usable to the customer submitting it.

        Falls back to page-load → return when no submit event was reported, since the
        browser cannot always observe a submit on a cross-origin page. Returns ``None``
        when neither bracket exists rather than attributing the whole redirect to the
        customer.
        """
        submitted = self.span(TimelineEvent.HOSTED_PAGE_LOADED,
                              TimelineEvent.CUSTOMER_SUBMITTED)
        if submitted is not None:
            return submitted
        return self.span(TimelineEvent.HOSTED_PAGE_LOADED,
                         TimelineEvent.RETURN_URL_RECEIVED)

    def summary(self, *, gateway_api_time_ms: float,
                total_duration_ms: Optional[float] = None) -> Dict[str, Optional[float]]:
        return {
            "gateway_api_time_ms": round(gateway_api_time_ms, 3),
            "three_ds_time_ms": self.three_ds_time_ms(),
            "customer_interaction_time_ms": self.customer_interaction_time_ms(),
            "redirect_time_ms": self.redirect_time_ms(),
            "total_duration_ms": (round(total_duration_ms, 3)
                                  if total_duration_ms is not None else self.elapsed_ms()),
            "app_overhead_ms": self.app_overhead_ms,
        }


#: Human labels for the timeline, matching the T0..T10 sequence in section 8.
EVENT_LABELS: Dict[TimelineEvent, str] = {
    TimelineEvent.BENCHMARK_STARTED: "Benchmark started",
    TimelineEvent.SESSION_REQUEST_SENT: "Create session request sent",
    TimelineEvent.SESSION_RESPONSE_RECEIVED: "Create session response received",
    TimelineEvent.HPP_URL_GENERATED: "Hosted payment page URL generated",
    TimelineEvent.REDIRECT_INITIATED: "Redirect initiated",
    TimelineEvent.HOSTED_PAGE_LOADED: "Hosted payment page loaded",
    TimelineEvent.CUSTOMER_SUBMITTED: "Customer submitted payment",
    TimelineEvent.PAYMENT_REQUEST_SENT: "Payment API request sent",
    TimelineEvent.PAYMENT_RESPONSE_RECEIVED: "Payment API response received",
    TimelineEvent.THREE_DS_INITIATED: "3DS initiated",
    TimelineEvent.THREE_DS_COMPLETED: "3DS completed",
    TimelineEvent.AUTHORIZATION_REQUESTED: "Authorization request sent",
    TimelineEvent.AUTHORIZATION_RESPONSE: "Authorization response received",
    TimelineEvent.RETURN_URL_RECEIVED: "Customer returned from gateway",
    TimelineEvent.WEBHOOK_RECEIVED: "Gateway webhook received",
    TimelineEvent.FINAL_STATUS_CONFIRMED: "Final transaction status confirmed",
    TimelineEvent.ERROR: "Error",
}
