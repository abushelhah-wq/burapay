"""Benchmark run, comparison test and transaction schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


class CardIn(BaseModel):
    """A sandbox test card typed on the payment form.

    Held in memory for the duration of one request and never written anywhere: not to
    the database, not to a log, not into a stored response snippet (specification
    section 25). The sanitizer truncates any PAN that reaches a log regardless, and
    the CVV is dropped outright. Accepting this at all requires
    ``ALLOW_DIRECT_CARD_ENTRY`` — typing a card number into a web application puts it
    in PCI scope, so it is off unless somebody deliberately turns it on.
    """

    number: str = Field(min_length=12, max_length=25,
                        description="Sandbox test card number. Spaces are stripped.")
    month: str = Field(min_length=1, max_length=2)
    year: str = Field(min_length=2, max_length=4)
    cvc: str = Field(min_length=3, max_length=4)
    holder: str = Field(default="BuraPay Benchmark", max_length=100)

    @field_validator("number")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        if not 12 <= len(digits) <= 19:
            raise ValueError("A card number is 12 to 19 digits.")
        return digits

    @field_validator("month", "year", "cvc")
    @classmethod
    def _numeric(cls, value: str) -> str:
        value = value.strip()
        if not value.isdigit():
            raise ValueError("Expected digits only.")
        return value


class BrowserContext(BaseModel):
    """What the browser that started a payment could see about itself.

    3-D Secure risk engines score a payment partly on this, and an issuer given none of
    it reaches for a challenge more often. A server-to-server call cannot know any of
    it, so the front end sends what it has. Nothing here is more identifying than what
    an ordinary web request already carries, and anything the browser could not see is
    left out rather than invented.
    """

    user_agent: Optional[str] = Field(default=None, max_length=500)
    language: Optional[str] = Field(default=None, max_length=35)
    time_zone_offset_minutes: Optional[int] = Field(default=None, ge=-1440, le=1440)
    #: A per-browser identifier the front end generates. Not a fingerprint and not tied
    #: to a person — just something stable enough for a gateway to correlate on.
    device_id: Optional[str] = Field(default=None, max_length=100)


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
    #: The card the operator typed on the payment form. Rejected unless
    #: ``ALLOW_DIRECT_CARD_ENTRY`` is on, and ignored for hosted checkout, where the
    #: card is entered on the provider's own page and never reaches this application.
    card: Optional[CardIn] = None
    #: A card token to authenticate and charge instead of a card number. Always
    #: accepted — a token is not card data — and the only way to run a Direct payment
    #: on an account that is not cleared to receive raw card numbers.
    token_id: Optional[str] = Field(default=None, max_length=200)
    #: The agreement a stored card is charged under, or stored against. On Geidea the
    #: merchant chooses this value rather than the gateway issuing one; a Store card
    #: payment mints one when it is left blank.
    agreement_id: Optional[str] = Field(default=None, max_length=200)
    #: Merchant for an unattended charge (MIT), Customer when the cardholder is present
    #: and picking a saved card (CIT). The card networks treat the two differently, so
    #: it belongs to the payment rather than to the account.
    initiated_by: Optional[str] = Field(default=None, pattern="^(Merchant|Customer)$")
    #: What the browser can see about itself. 3DS risk engines ask for it and a
    #: server-to-server call cannot know it, so the front end forwards it.
    browser: Optional[BrowserContext] = None


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
    #: True when a Direct payment stopped for a 3DS challenge. ``redirect_url`` then
    #: says where to send the cardholder, exactly as it does for hosted checkout.
    requires_customer_action: bool = False
    #: The agreement a minted token was stored under. Useless without the token and
    #: vice versa, so the two are returned together.
    agreement_id: Optional[str] = None


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
    #: A card token this payment minted. Shown once, on the transaction that created
    #: it, for copying into the gateway's settings — never stored there automatically,
    #: because a card token belongs to a cardholder rather than to the merchant.
    stored_token: Optional[str] = None
    stored_token_hint: Optional[str] = None
    #: The agreement the token was stored under, where the gateway needs one. A later
    #: charge needs both halves, so both are shown.
    agreement_id: Optional[str] = None
    agreement_type: Optional[str] = None
    #: True while the payment is parked on a 3DS challenge, so the page can offer a way
    #: back into it instead of showing a PENDING row with no explanation.
    awaiting_customer_action: bool = False


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


class ThreeDsChallenge(BaseModel):
    """What the browser needs to put the cardholder in front of their issuer.

    Either a URL to navigate to, or an auto-submitting form the issuer wants posted.
    The form is rendered in a sandboxed frame rather than injected into the page: it
    is markup this application received from somewhere else, and it belongs in its own
    origin, not in ours.
    """

    transaction_id: str
    gateway_code: str
    status: str
    #: ``redirect`` when the issuer gave a URL, ``form`` when it gave markup to post.
    mode: str = Field(pattern="^(redirect|form)$")
    challenge_url: Optional[str] = None
    challenge_html: Optional[str] = None
    #: Where the issuer sends the cardholder afterwards, and where the payment resumes.
    return_url: str
    amount: float
    currency: str
    environment: str


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
