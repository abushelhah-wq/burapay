"""
Error normalization.

Six gateways report failure six different ways: an HTTP 402, a 200 carrying
``responseCode: "123"``, a ``resultCode: "Refused"``, an OPPWA ``result.code`` of
``800.100.153``. Comparing them requires one vocabulary, so every failure is mapped
to an :class:`ErrorCategory` while the gateway's own code and message are preserved
verbatim alongside it (specification sections 37 and 38).

The mapping is deliberately conservative: anything unrecognised becomes
``unknown_error`` rather than being guessed into a category, because a wrong
category is worse than an honest "not classified" when the whole point is objective
comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCategory(str, Enum):
    SUCCESS = "success"
    GATEWAY_DECLINE = "gateway_decline"
    AUTHENTICATION_ERROR = "authentication_error"
    CONFIGURATION_ERROR = "configuration_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    GATEWAY_5XX = "gateway_5xx"
    INVALID_REQUEST = "invalid_request"
    THREE_DS_FAILURE = "3ds_failure"
    CUSTOMER_CANCELLED = "customer_cancelled"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class NormalizedError:
    """One failure, in the platform's vocabulary and the gateway's own words."""

    gateway: str
    operation: str
    category: ErrorCategory
    message: str
    http_status: Optional[int] = None
    gateway_code: Optional[str] = None
    duration_ms: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gateway": self.gateway,
            "operation": self.operation,
            "category": self.category.value,
            "http_status": self.http_status,
            "gateway_code": self.gateway_code,
            "message": self.message,
            "duration_ms": self.duration_ms,
        }


class BenchmarkError(RuntimeError):
    """Base class for the failures the benchmark engine knows how to record."""

    category = ErrorCategory.UNKNOWN_ERROR

    def __init__(self, message: str, *, gateway_code: Optional[str] = None,
                 http_status: Optional[int] = None,
                 raw: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.gateway_code = gateway_code
        self.http_status = http_status
        self.raw = raw or {}


class GatewayError(BenchmarkError):
    """The gateway answered, but with something the flow cannot continue from."""


class GatewayDeclined(BenchmarkError):
    category = ErrorCategory.GATEWAY_DECLINE


class NotConfigured(BenchmarkError):
    """Credentials this operation needs are missing."""

    category = ErrorCategory.CONFIGURATION_ERROR


class NotSupported(BenchmarkError):
    """This gateway genuinely does not offer this integration or operation."""

    category = ErrorCategory.INVALID_REQUEST


class NetworkError(BenchmarkError):
    category = ErrorCategory.NETWORK_ERROR


class GatewayTimeout(BenchmarkError):
    category = ErrorCategory.TIMEOUT


def category_for_http(status: Optional[int]) -> ErrorCategory:
    """Map an HTTP status to a category.

    Transport-level only — a 200 that carries a decline is classified by the adapter,
    which is the only layer that can read the gateway's own response code.
    """
    if status is None:
        return ErrorCategory.NETWORK_ERROR
    if 200 <= status < 300:
        return ErrorCategory.SUCCESS
    if status in (401, 403):
        return ErrorCategory.AUTHENTICATION_ERROR
    if status == 402:
        return ErrorCategory.GATEWAY_DECLINE
    if status in (408, 504):
        return ErrorCategory.TIMEOUT
    if 400 <= status < 500:
        return ErrorCategory.INVALID_REQUEST
    if status >= 500:
        return ErrorCategory.GATEWAY_5XX
    return ErrorCategory.UNKNOWN_ERROR


def normalize(exc: BaseException, *, gateway: str, operation: str,
              duration_ms: Optional[float] = None) -> NormalizedError:
    """Turn any exception raised during a flow into a recordable failure."""
    import httpx  # local import so this module stays importable without the client

    if isinstance(exc, BenchmarkError):
        category = exc.category
        if category is ErrorCategory.UNKNOWN_ERROR and exc.http_status is not None:
            category = category_for_http(exc.http_status)
        return NormalizedError(gateway=gateway, operation=operation, category=category,
                               message=str(exc), http_status=exc.http_status,
                               gateway_code=exc.gateway_code, duration_ms=duration_ms,
                               raw=exc.raw)
    if isinstance(exc, httpx.TimeoutException):
        return NormalizedError(gateway=gateway, operation=operation,
                               category=ErrorCategory.TIMEOUT,
                               message=f"request timed out: {exc}", duration_ms=duration_ms)
    if isinstance(exc, httpx.HTTPError):
        return NormalizedError(gateway=gateway, operation=operation,
                               category=ErrorCategory.NETWORK_ERROR,
                               message=f"{type(exc).__name__}: {exc}",
                               duration_ms=duration_ms)
    return NormalizedError(gateway=gateway, operation=operation,
                           category=ErrorCategory.UNKNOWN_ERROR,
                           message=f"{type(exc).__name__}: {exc}", duration_ms=duration_ms)
