"""Transaction and audit-log responses (§5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from app.schemas.common import ApiModel, Money


class ApiCallLogOut(ApiModel):
    """
    One logged HTTP call. This is the audit trail §2 requires: a transaction
    must be explainable from these rows alone, in ``sequence_number`` order.

    Bodies are stored already redacted, so what is served here is what was
    stored -- there is no second redaction pass that could differ from the one
    applied at write time.
    """

    id: int
    sequence_number: int
    step_name: str
    http_method: str
    url: str
    request_headers_json: dict[str, Any] = Field(default_factory=dict)
    request_body_json: Optional[Any] = None
    response_status_code: Optional[int] = None
    response_headers_json: Optional[dict[str, Any]] = None
    response_body_json: Optional[Any] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    #: The timeout in force for this call, so timeout behaviour is comparable
    #: between gateways rather than anecdotal (§6).
    timeout_seconds: Optional[float] = None
    error_text: Optional[str] = None


class TransactionActions(ApiModel):
    """
    Which follow-up operations are legal right now (§5).

    Computed server-side from the transaction's state and the adapter's
    capabilities. The UI renders buttons from this rather than deciding for
    itself, so the button and the endpoint can never disagree.
    """

    can_capture: bool = False
    can_partial_capture: bool = False
    can_refund: bool = False
    can_partial_refund: bool = False
    can_void: bool = False
    can_query: bool = False
    remaining_capturable: Optional[Money] = None
    remaining_refundable: Optional[Money] = None
    blocked_reason: Optional[str] = None


class TransactionOut(ApiModel):
    id: int
    gateway_code: str
    gateway_name: str
    integration_type: Optional[str] = None
    operation: str
    status: str
    parent_transaction_id: Optional[int] = None

    amount: Optional[Money] = None
    captured: Optional[Money] = None
    refunded: Optional[Money] = None

    card_brand: Optional[str] = None
    card_last4: Optional[str] = None
    card_exp_month: Optional[int] = None
    card_exp_year: Optional[int] = None
    token_reference: Optional[str] = None

    gateway_order_id: Optional[str] = None
    gateway_transaction_id: Optional[str] = None
    merchant_reference: str
    idempotency_key: str

    error_code: Optional[str] = None
    error_message: Optional[str] = None

    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    #: Total HTTP round trips this operation needed -- the benchmark's headline
    #: metric (§3).
    request_count: int = 0
    created_at: datetime


class TransactionDetail(TransactionOut):
    api_calls: list[ApiCallLogOut] = Field(default_factory=list)
    actions: TransactionActions = Field(default_factory=TransactionActions)
    children: list[TransactionOut] = Field(default_factory=list)


class HPPSessionOut(ApiModel):
    """
    A hosted session the customer must complete.

    Returned by hosted checkout, save-card and customer-initiated token charges
    -- all three create a session rather than completing server-side.

    ``redirect_url`` may be absent. Geidea's hosted page is opened by its
    JavaScript SDK from a ``session_id`` rather than by redirecting to a URL,
    so ``session_id`` is the field that always matters and ``redirect_url`` is
    populated only when the gateway returns one.
    """

    transaction: TransactionOut
    redirect_url: Optional[str] = None
    session_id: Optional[str] = None
    #: Which response field the redirect URL was found under. Recorded because
    #: the canonical field name is not confirmed for every gateway.
    redirect_field: Optional[str] = None
    #: Explains what the operator has to do next when there is no redirect URL
    #: to follow. Server-side rather than hardcoded in the UI, because the
    #: answer is gateway-specific.
    next_step: Optional[str] = None


class WebhookOut(ApiModel):
    id: int
    gateway_code: str
    #: ``None`` means "could not be verified on this build" -- distinct from
    #: both a valid and a forged signature (§4k).
    signature_valid: Optional[bool] = None
    received_at: datetime
    matched_transaction_id: Optional[int] = None
    duplicate_of_id: Optional[int] = None
    event_reference: Optional[str] = None
    headers_json: dict[str, Any] = Field(default_factory=dict)
    body_json: Optional[Any] = None


class TokenOut(ApiModel):
    id: int
    gateway_code: str
    token_reference: str
    card_brand: Optional[str] = None
    card_last4: Optional[str] = None
    card_exp_month: Optional[int] = None
    card_exp_year: Optional[int] = None
    is_active: bool
    created_at: datetime
