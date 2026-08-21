"""
Normalized results returned by every adapter method (§0.3, §3).

These types are the boundary between "how a particular gateway talks" and "how
this platform reasons". Nothing above the adapter layer ever sees a gateway's
own field names -- if a caller needs the raw payload it reads
``api_call_logs.response_body_json``, which is redacted and stored for exactly
that purpose.

All amounts are integer minor units (§0.4). All timestamps are UTC (§0.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.models import TransactionStatus


@dataclass(slots=True)
class CardDetails:
    """
    The only card attributes this platform is permitted to retain (§0.8).

    There is deliberately no ``number`` or ``cvv`` field. A raw card is carried
    by :class:`RawCard`, which is never stored and never serialised into a log.
    """

    brand: Optional[str] = None
    last4: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None


@dataclass(slots=True)
class RawCard:
    """
    A raw test card, in memory only, for the duration of one request.

    Never persisted, never logged, never placed on a result object. The
    redactor strips these values from request bodies before they reach
    ``api_call_logs``; this class existing separately from :class:`CardDetails`
    is what makes "did we accidentally store a PAN" answerable by reading the
    type signatures.
    """

    number: str
    exp_month: int
    exp_year: int
    cvv: str
    holder_name: Optional[str] = None

    def safe_details(self, brand: Optional[str] = None) -> CardDetails:
        digits = "".join(c for c in self.number if c.isdigit())
        return CardDetails(
            brand=brand or detect_brand(digits),
            last4=digits[-4:] if len(digits) >= 4 else None,
            exp_month=self.exp_month,
            exp_year=self.exp_year,
        )

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Overridden so a stray repr() in a traceback or a debug log cannot
        # print a PAN. This is the class most likely to end up in an exception
        # message by accident.
        digits = "".join(c for c in self.number if c.isdigit())
        tail = digits[-4:] if len(digits) >= 4 else "????"
        return f"<RawCard last4={tail} exp={self.exp_month:02d}/{self.exp_year}>"


def detect_brand(digits: str) -> Optional[str]:
    """
    Best-effort brand from the IIN range.

    Used only for display and for the ``card_brand`` column. The gateway's own
    reported brand always wins when it returns one -- this is the fallback for
    a response that does not.
    """
    if not digits:
        return None
    if digits.startswith("4"):
        return "VISA"
    if digits[:2] in {"51", "52", "53", "54", "55"} or 222100 <= int(digits[:6] or 0) <= 272099:
        return "MASTERCARD"
    if digits[:2] in {"34", "37"}:
        return "AMEX"
    if digits.startswith("62"):
        return "UNIONPAY"
    if digits[:4] == "6011" or digits[:2] == "65":
        return "DISCOVER"
    if digits[:2] in {"50", "58", "60", "63", "67"}:
        return "MADA"
    return None


@dataclass(slots=True)
class GatewayCall:
    """A single logged HTTP call, as returned to the adapter that made it."""

    log_id: Optional[int]
    sequence_number: int
    step_name: str
    method: str
    url: str
    status_code: Optional[int]
    response_json: Any
    response_text: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    timeout_seconds: float
    error_text: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


@dataclass(slots=True)
class BaseResult:
    """Fields common to every adapter result."""

    #: Every call the operation made, in order. ``len(calls)`` is the value
    #: written to ``transactions.request_count`` -- the benchmark's headline
    #: metric (§3), derived rather than counted by hand so it cannot drift.
    calls: list[GatewayCall] = field(default_factory=list)
    raw: Any = None

    @property
    def request_count(self) -> int:
        return len(self.calls)


@dataclass(slots=True)
class PaymentResult(BaseResult):
    status: TransactionStatus = TransactionStatus.PENDING
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    gateway_order_id: Optional[str] = None
    gateway_transaction_id: Optional[str] = None
    token_reference: Optional[str] = None
    card: Optional[CardDetails] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    #: Set when the gateway requires the payer to complete a 3DS challenge in a
    #: browser before the payment can proceed.
    challenge_url: Optional[str] = None


@dataclass(slots=True)
class HPPSessionResult(BaseResult):
    session_id: Optional[str] = None
    #: Where the browser is sent. The adapter reports which response field this
    #: came from rather than assuming a spelling.
    redirect_url: Optional[str] = None
    gateway_order_id: Optional[str] = None
    #: The field name the redirect URL was actually found under, recorded so a
    #: gateway changing its response shape is visible in the audit log.
    redirect_field: Optional[str] = None


@dataclass(slots=True)
class TokenResult(BaseResult):
    status: TransactionStatus = TransactionStatus.PENDING
    token_reference: Optional[str] = None
    #: Separate consent/agreement identifier where the gateway issues one.
    agreement_reference: Optional[str] = None
    card: Optional[CardDetails] = None
    gateway_transaction_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(slots=True)
class OrderStatusResult(BaseResult):
    status: TransactionStatus = TransactionStatus.PENDING
    gateway_order_id: Optional[str] = None
    gateway_transaction_id: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    captured_amount_minor: Optional[int] = None
    refunded_amount_minor: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    #: The gateway's own status string, kept verbatim for the audit trail. The
    #: normalised ``status`` above is what the platform reasons about.
    gateway_status: Optional[str] = None


@dataclass(slots=True)
class OrderContext:
    """
    What an adapter needs to start a payment.

    Built by the service layer so an adapter never reads settings or the
    database directly -- it receives everything, including the callback URLs
    already resolved from ``PUBLIC_BASE_URL``.
    """

    merchant_reference: str
    amount_minor: int
    currency: str
    return_url: Optional[str] = None
    callback_url: Optional[str] = None
    customer_email: Optional[str] = None
    #: Free-form, gateway-specific, non-secret. Populated from the request.
    metadata: dict[str, Any] = field(default_factory=dict)
