"""
Call timing — the part of the app that actually measures something.

Every outbound HTTP call an adapter makes goes through :class:`MeasuredSession`,
which records wall-clock duration from just before the request is issued to the
moment the response body has been fully read. That boundary matters: stopping at
the response headers would systematically under-report gateways that stream a
large body, and would make a fast-headers/slow-body gateway look better than it is.

Calls are either **timed** (part of the operation being measured) or **prep**
(setup a real merchant would already have done — creating a payment so there is
something to refund, minting a token so there is something to charge). Only timed
calls count toward the reported call count and latency.
"""

from __future__ import annotations

import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests


class GatewayError(RuntimeError):
    """A gateway returned something the flow cannot continue from."""


class NotConfigured(RuntimeError):
    """The gateway is missing credentials this operation needs."""


class NotSupported(RuntimeError):
    """This gateway genuinely does not offer this flow."""


@dataclass
class CallRecord:
    """One HTTP round trip."""

    step: int
    name: str
    method: str
    url: str
    timed: bool
    elapsed_ms: float
    status_code: Optional[int] = None
    ok: bool = False
    error: Optional[str] = None
    #: Short response excerpt, kept for debugging failures. Request bodies are never
    #: stored, so card data submitted upstream never reaches the database or the UI.
    snippet: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_reference(prefix: str = "bench") -> str:
    """Unique merchant reference. Gateways reject repeats, so every attempt needs one."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def money(amount: float) -> str:
    """Two-decimal string, for gateways that sign or transmit decimal amounts."""
    return f"{amount:.2f}"


def minor_units(amount: float) -> int:
    """Smallest currency unit, for gateways that take integer amounts."""
    return int(round(amount * 100))


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile — never interpolated, so every figure is one that occurred."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct / 100 * len(ordered) // 1))))
    return round(ordered[rank - 1], 3)


def summarize(values: Sequence[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "min": None, "mean": None, "median": None,
                "p95": None, "max": None, "stdev": None}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": percentile(values, 95),
        "max": round(max(values), 3),
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }


class MeasuredSession:
    """A ``requests.Session`` wrapper that times every call an adapter makes."""

    def __init__(self, gateway: str, flow: str, timeout: float = 30.0,
                 session: Optional[requests.Session] = None) -> None:
        self.gateway = gateway
        self.flow = flow
        self.timeout = timeout
        self.session = session or requests.Session()
        self.calls: List[CallRecord] = []
        self._step = 0
        self._owns_session = session is None

    # -- issuing calls ---------------------------------------------------- #

    def call(self, name: str, method: str, url: str, **kwargs: Any) -> requests.Response:
        """A measured call: counts toward the call count and the latency total."""
        return self._request(name, method, url, timed=True, **kwargs)

    def prep(self, name: str, method: str, url: str, **kwargs: Any) -> requests.Response:
        """An untimed setup call: excluded from the count and the latency total."""
        return self._request(name, method, url, timed=False, **kwargs)

    # -- reading responses ------------------------------------------------ #

    def json_of(self, response: requests.Response) -> Dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise GatewayError(
                f"{self.gateway}: expected JSON from {response.request.method} "
                f"{response.url}, got {response.status_code} {response.text[:200]!r}")
        if not isinstance(body, dict):
            raise GatewayError(
                f"{self.gateway}: expected a JSON object from {response.url}, "
                f"got {type(body).__name__}")
        return body

    def expect_ok(self, response: requests.Response, what: str,
                  accept: Iterable[int] = (200, 201, 202)) -> Dict[str, Any]:
        if response.status_code not in tuple(accept):
            raise GatewayError(f"{self.gateway}: {what} returned HTTP "
                               f"{response.status_code}: {response.text[:300]}")
        return self.json_of(response)

    # -- accounting ------------------------------------------------------- #

    @property
    def timed_calls(self) -> List[CallRecord]:
        return [c for c in self.calls if c.timed]

    @property
    def call_count(self) -> int:
        return len(self.timed_calls)

    @property
    def total_ms(self) -> float:
        return round(sum(c.elapsed_ms for c in self.timed_calls), 3)

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> "MeasuredSession":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- internals -------------------------------------------------------- #

    def _request(self, name: str, method: str, url: str, timed: bool,
                 **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        self._step += 1
        record = CallRecord(step=self._step, name=name, method=method.upper(), url=url,
                            timed=timed, elapsed_ms=0.0)
        started = time.perf_counter()
        try:
            response = self.session.request(method.upper(), url, **kwargs)
            _ = response.content     # include reading the body in the measurement
            record.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record.status_code = response.status_code
            record.ok = response.ok
            if not response.ok:
                record.error = f"HTTP {response.status_code}"
                record.snippet = response.text[:300]
        except requests.RequestException as exc:
            record.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record.error = f"{type(exc).__name__}: {exc}"
            self.calls.append(record)
            raise GatewayError(f"{self.gateway}: {method.upper()} {url} failed: {exc}") from exc

        self.calls.append(record)
        return response
