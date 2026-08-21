"""
Database model (§2).

Design notes worth knowing before changing anything here:

* **Money is ``BigInteger`` minor units.** There is no ``Numeric`` and no
  ``Float`` column in this file. See :mod:`app.money`.
* **Timestamps are ``TIMESTAMP WITH TIME ZONE``**, written as UTC with
  millisecond precision by :func:`app.timeutil.utcnow`.
* **Enums are ``String`` + ``CheckConstraint``, not native Postgres enums.**
  Adding a new gateway must never require touching shared code (§1), and a new
  gateway routinely brings a new operation with it. Extending a
  ``CheckConstraint`` is an ordinary migration; ``ALTER TYPE ... ADD VALUE`` is
  not transactional in older Postgres and cannot be reversed in a downgrade.
* **Raw gateway payloads are JSONB** so the audit log is queryable, which is
  the whole point of §2's closing requirement: every transaction must be
  explainable from ``api_call_logs`` alone.
"""

from __future__ import annotations

import enum
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.timeutil import utcnow

#: JSONB on Postgres, plain JSON elsewhere so the mock-gateway test suite can
#: run on SQLite without a database server.
JSONType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


def _ts(**kwargs: Any) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), **kwargs)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Operation(str, enum.Enum):
    """The operations a transaction row can represent (§2)."""

    AUTH = "AUTH"
    SALE = "SALE"
    CAPTURE = "CAPTURE"
    PARTIAL_CAPTURE = "PARTIAL_CAPTURE"
    REFUND = "REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    VOID = "VOID"
    TOKENIZE = "TOKENIZE"
    CIT_CHARGE = "CIT_CHARGE"
    MIT_CHARGE = "MIT_CHARGE"
    ORDER_QUERY = "ORDER_QUERY"


class TransactionStatus(str, enum.Enum):
    """
    Lifecycle of a transaction (§2).

    ``FAILED`` and ``ERROR`` are deliberately distinct and mean different
    things to the benchmark: ``FAILED`` is the gateway answering "no" (a
    decline), which is a successful round trip; ``ERROR`` is the call itself
    not completing (network failure, non-2xx, unparseable body). A gateway that
    declines quickly is not the same as one that errors, and averaging them
    together would hide exactly the difference this product measures.
    """

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


#: Terminal states. A transaction in one of these is never re-processed by a
#: replayed webhook (§4k).
TERMINAL_STATUSES = frozenset({
    TransactionStatus.SUCCESS, TransactionStatus.FAILED,
    TransactionStatus.ERROR, TransactionStatus.TIMEOUT,
})


class IntegrationTypeCode(str, enum.Enum):
    """
    Integration styles. §2 gives these as examples ("e.g."), so ``TOKENIZE`` is
    added here for the standalone save-card flow of §4g, which is neither a
    hosted page nor a charge.
    """

    HPP = "HPP"
    DIRECT_API = "DIRECT_API"
    TOKEN_CIT = "TOKEN_CIT"
    TOKEN_MIT = "TOKEN_MIT"
    TOKENIZE = "TOKENIZE"


def _check(column: str, values: type[enum.Enum], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class User(Base):
    """
    An operator of this platform. Not a payer -- there are no customer accounts.

    Created either by the bootstrap path (first admin, only when this table is
    empty -- see :mod:`app.services.bootstrap`) or by an existing admin.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[Any] = _ts(nullable=False, default=utcnow)
    last_login_at: Mapped[Optional[Any]] = _ts(nullable=True)


# ---------------------------------------------------------------------------
# Gateway registry
# ---------------------------------------------------------------------------

class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Non-secret adapter configuration only (region, API base override, flags).
    #: Credentials never live here -- they live in ``gateway_credentials``,
    #: encrypted. A plaintext secret in this column would be a §7b violation.
    config_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[Any] = _ts(nullable=False, default=utcnow)

    credentials: Mapped[List["GatewayCredential"]] = relationship(
        back_populates="gateway", cascade="all, delete-orphan"
    )


class IntegrationType(Base):
    __tablename__ = "integration_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        _check("code", IntegrationTypeCode, "ck_integration_types_code"),
    )


class GatewayCredential(Base):
    """
    One credential value for one gateway, encrypted at rest (§7b).

    ``encrypted_value`` is Fernet ciphertext produced with ``ENCRYPTION_KEY``.
    Nothing writes plaintext to this table -- not a seed script, not a
    migration, not a test fixture. :mod:`app.security.credentials` is the only
    module that encrypts or decrypts it.
    """

    __tablename__ = "gateway_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[int] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False
    )
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    #: Gates ALLOW_PRODUCTION_GATEWAYS. A credential flagged live is refused at
    #: the adapter layer unless that flag is explicitly on (§7b).
    is_production: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[Any] = _ts(nullable=False, default=utcnow, onupdate=utcnow)
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    gateway: Mapped["Gateway"] = relationship(back_populates="credentials")

    __table_args__ = (
        UniqueConstraint("gateway_id", "key_name", name="uq_gateway_credential"),
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    integration_type_id: Mapped[int] = mapped_column(
        ForeignKey("integration_types.id"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)

    #: capture -> auth, refund -> capture/sale, CIT/MIT -> the tokenize that
    #: produced the token. Lets the UI render a flow as a tree.
    parent_transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TransactionStatus.PENDING.value
    )

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    #: Running totals maintained on the *parent* so §4d's "captured to date /
    #: remaining capturable" and §4e's refund ceiling are answerable without
    #: re-summing children on every request.
    captured_amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    refunded_amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )

    # §0.8 permits exactly these four card attributes to be stored.
    card_brand: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    card_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    card_exp_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    card_exp_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    token_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    gateway_order_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gateway_transaction_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    #: Our own reference, always set before the first outbound call, so a
    #: transaction is identifiable in the gateway's dashboard even if the
    #: response never arrives.
    merchant_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[Any] = _ts(nullable=False, default=utcnow)
    completed_at: Mapped[Optional[Any]] = _ts(nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: Total HTTP calls this operation took. A first-class product metric, not
    #: an implementation detail (§3) -- it is what the benchmark compares.
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[Any] = _ts(nullable=False, default=utcnow)

    api_calls: Mapped[List["ApiCallLog"]] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="ApiCallLog.sequence_number",
    )
    # Self-referential: capture -> auth, refund -> capture/sale. remote_side
    # belongs on the many-to-one side only; putting it on both makes SQLAlchemy
    # read the pair as two many-to-one relationships pointing at each other.
    parent: Mapped[Optional["Transaction"]] = relationship(
        back_populates="children",
        remote_side=lambda: [Transaction.id],
        foreign_keys=lambda: [Transaction.parent_transaction_id],
    )
    children: Mapped[List["Transaction"]] = relationship(
        back_populates="parent",
        foreign_keys=lambda: [Transaction.parent_transaction_id],
        cascade="save-update",
    )

    __table_args__ = (
        # §0.6: the backend must never create two transactions for one key,
        # even under retry or a double-click. This is the enforcement -- the
        # service layer's check-then-insert is only an optimisation on top of
        # it, since a check alone races.
        UniqueConstraint(
            "gateway_id", "idempotency_key", name="uq_transaction_idempotency"
        ),
        _check("operation", Operation, "ck_transactions_operation"),
        _check("status", TransactionStatus, "ck_transactions_status"),
        CheckConstraint("amount_minor >= 0", name="ck_transactions_amount_nonneg"),
        CheckConstraint(
            "captured_amount_minor >= 0 AND refunded_amount_minor >= 0",
            name="ck_transactions_totals_nonneg",
        ),
        Index("ix_transactions_gateway_created", "gateway_id", "created_at"),
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_merchant_reference", "merchant_reference"),
        Index("ix_transactions_gateway_order_id", "gateway_order_id"),
    )

    # -- derived helpers used by the API and the UI gating rules -----------

    @property
    def remaining_capturable_minor(self) -> int:
        return max(0, self.amount_minor - self.captured_amount_minor)

    @property
    def remaining_refundable_minor(self) -> int:
        """
        Refundable ceiling (§4e).

        For an authorisation, only what has actually been captured can be
        refunded; for a sale, the full amount can. Refunding an uncaptured
        auth is blocked outright rather than sent to the gateway to reject.
        """
        if self.operation == Operation.AUTH.value:
            base = self.captured_amount_minor
        else:
            base = self.amount_minor
        return max(0, base - self.refunded_amount_minor)


class ApiCallLog(Base):
    """
    One outbound HTTP call to a gateway (§2).

    Written by :mod:`app.gateways.http_client` and by nothing else. One row is
    created *before* the request goes out and completed *after* the response
    (or the timeout, or the parse failure) -- so a call that never returns
    still leaves a row explaining why, and no transaction can sit in
    ``PENDING`` with no log behind it (§6).
    """

    __tablename__ = "api_call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Nullable: some calls precede the transaction they belong to, e.g. an
    #: HPP session created before an order ID exists (§2).
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=True
    )
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(64), nullable=False)

    http_method: Mapped[str] = mapped_column(String(8), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)

    request_headers_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    request_body_json: Mapped[Optional[Any]] = mapped_column(JSONType, nullable=True)

    response_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_headers_json: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    response_body_json: Mapped[Optional[Any]] = mapped_column(JSONType, nullable=True)

    started_at: Mapped[Any] = _ts(nullable=False, default=utcnow)
    completed_at: Mapped[Optional[Any]] = _ts(nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: The timeout that applied to this call, logged so "gateway X times out
    #: more than gateway Y" is measurable rather than anecdotal (§6).
    timeout_seconds: Mapped[Optional[float]] = mapped_column(nullable=True)

    #: Set on network error, timeout, non-2xx handling, or a body that would
    #: not parse. Distinguishes "no response" from "a response we disliked".
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[Any] = _ts(nullable=False, default=utcnow)

    transaction: Mapped[Optional["Transaction"]] = relationship(back_populates="api_calls")

    __table_args__ = (
        UniqueConstraint(
            "transaction_id", "sequence_number", name="uq_api_call_sequence"
        ),
        Index("ix_api_call_logs_transaction", "transaction_id", "sequence_number"),
        Index("ix_api_call_logs_gateway_started", "gateway_id", "started_at"),
    )


class Token(Base):
    """A stored card token (§2, §4g). Holds a reference, never a PAN."""

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    token_reference: Mapped[str] = mapped_column(String(255), nullable=False)

    card_brand: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    card_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    card_exp_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    card_exp_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_from_transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    #: Some gateways issue a separate agreement/consent identifier alongside the
    #: token and require it on merchant-initiated charges. Nullable because not
    #: every gateway has the concept.
    agreement_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[Any] = _ts(nullable=False, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("gateway_id", "token_reference", name="uq_token_reference"),
    )


class WebhookReceived(Base):
    """
    A raw inbound callback (§2, §4k).

    Stored before any interpretation, so a callback that fails signature
    verification is still evidence rather than a discarded 401.
    """

    __tablename__ = "webhooks_received"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    headers_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    body_json: Mapped[Optional[Any]] = mapped_column(JSONType, nullable=True)
    signature_valid: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    received_at: Mapped[Any] = _ts(nullable=False, default=utcnow)
    matched_transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    #: Set when a replay is detected, so §4k's "must not double-process" is
    #: observable in the audit log rather than being a silent no-op.
    duplicate_of_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("webhooks_received.id", ondelete="SET NULL"), nullable=True
    )
    #: Gateway-supplied event identifier when one exists; the dedupe key.
    event_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_webhooks_gateway_received", "gateway_id", "received_at"),
        Index("ix_webhooks_event_reference", "gateway_id", "event_reference"),
    )


class BenchmarkRun(Base):
    """
    A batch of transactions executed together for measurement.

    Exists so ``BENCHMARK_MIN_INTERVAL_SECONDS`` and
    ``BENCHMARK_MAX_TRANSACTIONS_PER_RUN`` can be enforced by the executor
    rather than being advisory (§7b), and so a run that stops because it hit
    the cap can say so in ``stop_reason`` instead of silently truncating.
    """

    __tablename__ = "benchmark_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    integration_type_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("integration_types.id"), nullable=True
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The amount each transaction in the run charges. Stored on the run so a
    #: re-queued job charges the same amount it was created for, rather than
    #: falling back to a default that would quietly change the measurement.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="SAR")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    #: Populated when a run stops early -- cap reached, interval violated,
    #: gateway unavailable. Never left blank on a run that did not finish.
    stop_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Any] = _ts(nullable=False, default=utcnow)
    completed_at: Mapped[Optional[Any]] = _ts(nullable=True)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        _check("operation", Operation, "ck_benchmark_runs_operation"),
        Index("ix_benchmark_runs_gateway_started", "gateway_id", "started_at"),
    )
