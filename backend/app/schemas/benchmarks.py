"""Benchmark run, comparison test and transaction schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class StartTransactionRequest(BaseModel):
    """A single transaction started from the UI (steps 1-3 of the user journey)."""

    gateway_code: str
    integration_type: str = Field(pattern="^(hpp|direct)$")
    amount: float = Field(default=1.00, gt=0, le=100000)
    currency: str = Field(default="SAR", min_length=3, max_length=5)
    reference: Optional[str] = Field(default=None, max_length=64,
                                     description="Order/reference id. Generated when omitted.")
    description: str = Field(default="BuraPay benchmark transaction", max_length=255)
    environment: str = Field(default="sandbox", pattern="^(sandbox|production)$")
    methodology: str = Field(default="mixed", pattern="^(cold|warm|mixed)$")
    #: standard | store_card | token. Only offered for gateways that declare support;
    #: a stored-token payment charges a card the gateway already holds.
    payment_mode: str = Field(default="standard",
                              pattern="^(standard|store_card|token)$")


class StartTransactionResponse(BaseModel):
    transaction_id: str
    status: str
    #: Present for HPP: where to send the browser next.
    redirect_url: Optional[str] = None
    #: ``redirect`` for a plain hand-off, ``widget`` when the gateway needs its own
    #: browser SDK mounted instead.
    mode: Optional[str] = None
    gateway_reference: Optional[str] = None
    #: Present when a Store card payment minted one. Shown once, for copying into the
    #: gateway's settings.
    stored_token: Optional[str] = None


class BenchmarkRunCreate(BaseModel):
    name: str = Field(max_length=200)
    gateway_code: str
    integration_type: str = Field(pattern="^(hpp|direct)$")
    transaction_count: int = Field(default=10, ge=1, le=1000)
    amount: float = Field(default=1.00, gt=0, le=100000)
    currency: str = Field(default="SAR", min_length=3, max_length=5)
    #: Seconds between transactions. Raised to the configured floor if lower —
    #: automated benchmarking must not fire uncontrolled volume at a provider.
    interval_seconds: Optional[float] = Field(default=None, ge=0, le=3600)
    environment: str = Field(default="sandbox", pattern="^(sandbox|production)$")
    methodology: str = Field(default="mixed", pattern="^(cold|warm|mixed)$")
    #: standard | store_card | token. Only offered for gateways that declare support;
    #: a stored-token payment charges a card the gateway already holds.
    payment_mode: str = Field(default="standard",
                              pattern="^(standard|store_card|token)$")


class BenchmarkRunOut(ORMModel):
    id: str
    name: str
    gateway_id: Optional[str] = None
    comparison_test_id: Optional[str] = None
    integration_type: str
    payment_mode: str = "standard"
    environment: str
    currency: str
    amount: float
    transaction_count: int
    interval_seconds: float
    methodology: str
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    run_metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class BenchmarkRunDetail(BenchmarkRunOut):
    gateway_code: Optional[str] = None
    progress: Dict[str, Any] = Field(default_factory=dict)
    statistics: Dict[str, Any] = Field(default_factory=dict)


class ComparisonTestCreate(BaseModel):
    name: str = Field(max_length=200)
    gateway_codes: List[str] = Field(min_length=2)
    integration_type: str = Field(pattern="^(hpp|direct)$")
    transactions_per_gateway: int = Field(default=20, ge=1, le=1000)
    amount: float = Field(default=1.00, gt=0, le=100000)
    currency: str = Field(default="SAR", min_length=3, max_length=5)
    interval_seconds: Optional[float] = Field(default=None, ge=0, le=3600)
    environment: str = Field(default="sandbox", pattern="^(sandbox|production)$")
    methodology: str = Field(default="mixed", pattern="^(cold|warm|mixed)$")
    #: standard | store_card | token. Only offered for gateways that declare support;
    #: a stored-token payment charges a card the gateway already holds.
    payment_mode: str = Field(default="standard",
                              pattern="^(standard|store_card|token)$")


class ComparisonTestOut(ORMModel):
    id: str
    name: str
    integration_type: str
    environment: str
    currency: str
    amount: float
    transactions_per_gateway: int
    gateway_codes: List[str] = Field(default_factory=list)
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    run_metadata: Dict[str, Any] = Field(default_factory=dict)


class ApiMeasurementOut(ORMModel):
    id: str
    sequence: int
    operation_name: str
    normalized_operation: str
    endpoint: str
    http_method: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: float
    http_status: Optional[int] = None
    gateway_response_code: Optional[str] = None
    gateway_response_message: Optional[str] = None
    success: bool
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    timed_out: bool = False
    is_setup_call: bool = False
    response_size_bytes: Optional[int] = None
    response_snippet: Optional[str] = None


class TransactionEventOut(ORMModel):
    id: str
    event_type: str
    event_timestamp: datetime
    offset_ms: float
    label: Optional[str] = None
    event_metadata: Dict[str, Any] = Field(default_factory=dict)


class BrowserMeasurementOut(ORMModel):
    id: str
    metric_name: str
    value_ms: float
    origin_scope: Optional[str] = None
    created_at: datetime


class TransactionOut(ORMModel):
    id: str
    benchmark_run_id: Optional[str] = None
    gateway_code: str
    gateway_transaction_id: Optional[str] = None
    merchant_reference: str
    integration_type: str
    payment_mode: str = "standard"
    environment: str
    amount: float
    currency: str
    description: Optional[str] = None
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_duration_ms: Optional[float] = None
    gateway_api_time_ms: Optional[float] = None
    three_ds_time_ms: Optional[float] = None
    customer_interaction_time_ms: Optional[float] = None
    redirect_time_ms: Optional[float] = None
    page_load_time_ms: Optional[float] = None
    webhook_latency_ms: Optional[float] = None
    app_overhead_ms: Optional[float] = None
    api_call_count: int = 0
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    gateway_response_code: Optional[str] = None
    gateway_response_message: Optional[str] = None
    methodology: str = "mixed"
    webhook_received_at: Optional[datetime] = None


class TransactionDetail(BaseModel):
    transaction: TransactionOut
    measurements: List[ApiMeasurementOut] = Field(default_factory=list)
    events: List[TransactionEventOut] = Field(default_factory=list)
    browser_measurements: List[BrowserMeasurementOut] = Field(default_factory=list)
    gateway_api_time_ms: float = 0.0
    setup_call_time_ms: float = 0.0
    documented_calls: Optional[str] = None


class HppHandoff(BaseModel):
    """What the browser needs to mount a gateway's own checkout component.

    Only ever the values the gateway mints for client-side use — a session id, an
    Adyen ``sessionData`` blob, a public client key. No credential is returned here;
    an API key or a secret would be redacted by the sanitizer long before this point.
    """

    transaction_id: str
    gateway_code: str
    status: str
    #: ``redirect`` for a plain hand-off, ``widget`` when the gateway's SDK must be
    #: mounted in our own page instead.
    mode: str
    redirect_url: Optional[str] = None
    return_url: str
    environment: str
    amount: float
    currency: str
    #: Gateway-specific parameters for the SDK, e.g. ``session_id`` and
    #: ``session_data`` for Adyen, ``checkout_id`` for HyperPay.
    widget: Dict[str, Any] = Field(default_factory=dict)


class BrowserMetricsIn(BaseModel):
    """Browser Performance API metrics reported by the front end (section 10).

    Only what the browser could actually observe is accepted. Cross-origin
    restrictions mean a hosted page may expose nothing beyond navigation timing, and
    the platform records that absence rather than filling it in.
    """

    metrics: Dict[str, float] = Field(
        description="Metric name to milliseconds, e.g. {'redirect_start': 12.4}")
    origin_scope: str = Field(default="same-origin", pattern="^(same-origin|cross-origin)$")
    #: Reported separately because it is the one HPP figure the browser alone can see.
    page_load_time_ms: Optional[float] = None
