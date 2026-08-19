"""
The persistence model.

Shape follows specification section 19, with three additions the spec implies but
does not list: ``users`` (section 22), ``app_settings`` (sections 18 and 26, so
scoring weights and rate limits are configurable without a redeploy), and
``gateway_health_checks`` (section 36).

Timing columns are ``Float`` milliseconds. Durations are computed from
``time.perf_counter()`` deltas, so the wall-clock timestamps beside them are for
ordering and display only — never for arithmetic (section 2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, JSON, String,
                        Text, UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPrimaryKey, utcnow


class User(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Gateway(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "gateways"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_hpp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_direct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: ISO-4217 codes this gateway is configured to accept. Empty means "unknown",
    #: which the UI shows as such rather than assuming universal support (section 3).
    supported_currencies: Mapped[List[str]] = mapped_column(JSON, default=list)
    logo_slug: Mapped[Optional[str]] = mapped_column(String(50))
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    docs_url: Mapped[Optional[str]] = mapped_column(String(500))
    notes: Mapped[List[str]] = mapped_column(JSON, default=list)

    credentials: Mapped[List["GatewayCredential"]] = relationship(
        back_populates="gateway", cascade="all, delete-orphan")


class GatewayCredential(UUIDPrimaryKey, Timestamped, Base):
    """One encrypted credential set per gateway per environment.

    ``encrypted_credentials`` is a Fernet blob over a JSON map. It is never returned
    by the API in any form other than masked (section 5).
    """

    __tablename__ = "gateway_credentials"
    __table_args__ = (UniqueConstraint("gateway_id", "environment",
                                       name="uq_gateway_credentials_gateway_id"),)

    gateway_id: Mapped[str] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="sandbox")
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which keys the blob holds. Stored in the clear so the UI can show what is
    #: configured without decrypting anything.
    field_names: Mapped[List[str]] = mapped_column(JSON, default=list)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))

    gateway: Mapped[Gateway] = relationship(back_populates="credentials")


class BenchmarkRun(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "benchmark_runs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    gateway_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("gateways.id", ondelete="SET NULL"), index=True)
    #: Set when this run is one leg of a multi-gateway comparison test (section 17).
    comparison_test_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("comparison_tests.id", ondelete="CASCADE"), index=True)
    integration_type: Mapped[str] = mapped_column(String(20), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="sandbox")
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    interval_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    methodology: Mapped[str] = mapped_column(String(20), nullable=False, default="mixed")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    #: Section 42: app version, hostname, region, python version, run configuration.
    #: Deliberately excludes anything that identifies the host beyond its name.
    run_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[Optional[str]] = mapped_column(String(255))

    gateway: Mapped[Optional[Gateway]] = relationship()
    transactions: Mapped[List["Transaction"]] = relationship(
        back_populates="benchmark_run", cascade="all, delete-orphan")


class ComparisonTest(UUIDPrimaryKey, Timestamped, Base):
    """A multi-gateway comparison (section 17): the same test against N gateways."""

    __tablename__ = "comparison_tests"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    integration_type: Mapped[str] = mapped_column(String(20), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="sandbox")
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    transactions_per_gateway: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    gateway_codes: Mapped[List[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[Optional[str]] = mapped_column(String(255))
    run_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)


class Transaction(UUIDPrimaryKey, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_gateway_integration", "gateway_id", "integration_type"),
        Index("ix_transactions_started_at", "started_at"),
    )

    benchmark_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True)
    gateway_id: Mapped[str] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False, index=True)
    gateway_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    gateway_transaction_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    merchant_reference: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    integration_type: Mapped[str] = mapped_column(String(20), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="sandbox")
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING",
                                        index=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # -- the measurements, all milliseconds ------------------------------- #
    #: Wall time from benchmark start to final state. Includes customer thinking time
    #: for HPP, which is why it is never used on its own to compare gateways.
    total_duration_ms: Mapped[Optional[float]] = mapped_column(Float)
    #: Sum of the timed gateway API calls only. This is the fair-comparison number
    #: (section 39): no customer interaction, no redirect, no browser.
    gateway_api_time_ms: Mapped[Optional[float]] = mapped_column(Float)
    three_ds_time_ms: Mapped[Optional[float]] = mapped_column(Float)
    customer_interaction_time_ms: Mapped[Optional[float]] = mapped_column(Float)
    redirect_time_ms: Mapped[Optional[float]] = mapped_column(Float)
    page_load_time_ms: Mapped[Optional[float]] = mapped_column(Float)
    #: Time from the customer's return to the gateway's webhook landing, when both
    #: happened. Null when the gateway sent no webhook.
    webhook_latency_ms: Mapped[Optional[float]] = mapped_column(Float)
    #: Application overhead measured separately so it is never charged to a gateway
    #: (section 51).
    app_overhead_ms: Mapped[Optional[float]] = mapped_column(Float)

    api_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    gateway_response_code: Mapped[Optional[str]] = mapped_column(String(50))
    gateway_response_message: Mapped[Optional[str]] = mapped_column(Text)
    methodology: Mapped[str] = mapped_column(String(20), nullable=False, default="mixed")
    webhook_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: Opaque per-adapter state needed to finish a two-legged HPP flow. Sanitized
    #: before it is written; never contains a credential.
    context: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    benchmark_run: Mapped[Optional[BenchmarkRun]] = relationship(back_populates="transactions")
    gateway: Mapped[Gateway] = relationship()
    measurements: Mapped[List["ApiMeasurement"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="ApiMeasurement.sequence")
    browser_measurements: Mapped[List["BrowserMeasurement"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan")
    events: Mapped[List["TransactionEvent"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="TransactionEvent.event_timestamp")


class ApiMeasurement(UUIDPrimaryKey, Base):
    """One outbound HTTP call to a gateway, timed end to end.

    ``duration_ms`` is a ``perf_counter`` delta covering the request through the full
    read of the response body: stopping at the headers would flatter a gateway that
    streams a large body slowly.
    """

    __tablename__ = "api_measurements"
    __table_args__ = (Index("ix_api_measurements_gw_op", "gateway_id", "normalized_operation"),)

    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
    gateway_id: Mapped[str] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The gateway's own operation name, kept raw (section 38).
    operation_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The comparable category it maps to.
    normalized_operation: Mapped[str] = mapped_column(String(40), nullable=False,
                                                      default="OTHER")
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    http_method: Mapped[str] = mapped_column(String(10), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    gateway_response_code: Mapped[Optional[str]] = mapped_column(String(50))
    gateway_response_message: Mapped[Optional[str]] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_category: Mapped[Optional[str]] = mapped_column(String(50))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Excluded from the reported API time: setup a real merchant would already have
    #: done (minting a token, creating something to refund).
    is_setup_call: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    response_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    #: Sanitized excerpt for debugging (section 43). Never a request body, never a
    #: header, never a secret.
    response_snippet: Mapped[Optional[str]] = mapped_column(Text)

    transaction: Mapped[Transaction] = relationship(back_populates="measurements")


class BrowserMeasurement(UUIDPrimaryKey, Base):
    """A Performance-API metric reported by the browser (section 10).

    Kept in its own table, never mixed into ``api_measurements``: browser numbers come
    from a different clock on a different machine, and cross-origin restrictions mean
    some of them are simply unavailable for a hosted page.
    """

    __tablename__ = "browser_measurements"

    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(80), nullable=False)
    value_ms: Mapped[float] = mapped_column(Float, nullable=False)
    #: "same-origin" metrics are complete; "cross-origin" ones are limited by browser
    #: security to what the gateway's Timing-Allow-Origin header permits.
    origin_scope: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)

    transaction: Mapped[Transaction] = relationship(back_populates="browser_measurements")


class TransactionEvent(UUIDPrimaryKey, Base):
    """A timeline milestone (section 8)."""

    __tablename__ = "transaction_events"

    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Milliseconds since the benchmark started, from the monotonic clock. This, not
    #: the timestamp difference, is what the timeline renders.
    offset_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    label: Mapped[Optional[str]] = mapped_column(String(255))
    event_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    transaction: Mapped[Transaction] = relationship(back_populates="events")


class WebhookEvent(UUIDPrimaryKey, Base):
    """A raw inbound webhook (section 23).

    Recorded whether or not it could be matched to a transaction, because an
    unmatched webhook is itself a finding about the gateway's callback behaviour.
    """

    __tablename__ = "webhook_events"

    gateway_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    transaction_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
    event_type: Mapped[Optional[str]] = mapped_column(String(120))
    signature_verified: Mapped[Optional[bool]] = mapped_column(Boolean)
    merchant_reference: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    #: Sanitized payload. Signature headers and secrets are stripped first.
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)


class GatewayHealthCheck(UUIDPrimaryKey, Base):
    """Result of a read-only reachability probe (section 36).

    Never a payment: the probe hits a safe endpoint, and a gateway with no safe
    endpoint is reported as "no safe probe available" rather than charged a card.
    """

    __tablename__ = "gateway_health_checks"

    gateway_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    response_time_ms: Mapped[Optional[float]] = mapped_column(Float)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    detail: Mapped[Optional[str]] = mapped_column(Text)


class AppSetting(UUIDPrimaryKey, Timestamped, Base):
    """Runtime-configurable application settings.

    Scoring weights (section 18) and benchmark rate limits (section 16) live here so
    they can be changed by an admin without a redeploy. Gateway credentials never do —
    those are encrypted in ``gateway_credentials``.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))
