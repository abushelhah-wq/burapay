"""
The platform's shared vocabulary.

These are stored as strings rather than native database enums: adding a value later
must not require a migration that locks the transactions table, and a value written
by an older application version must still read back cleanly.
"""

from __future__ import annotations

from enum import Enum


class IntegrationType(str, Enum):
    """The two integration models being compared (specification section 1)."""

    HPP = "hpp"
    DIRECT = "direct"


class GatewayEnvironment(str, Enum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class TransactionStatus(str, Enum):
    """Final states from section 9, plus the two in-flight states."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"

    @property
    def is_final(self) -> bool:
        return self not in (TransactionStatus.PENDING, TransactionStatus.IN_PROGRESS)


#: The one status that counts as a success for rate calculations. Everything else,
#: including DECLINED, is a failure: a declined transaction did not complete, even
#: though the gateway behaved correctly. The reports separate the two so a gateway is
#: never penalised for a test card the issuer refuses.
SUCCESS_STATUSES = frozenset({TransactionStatus.SUCCESS})


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NormalizedOperation(str, Enum):
    """Normalized categories every gateway operation maps to (section 38).

    The gateway's real operation name is stored next to this, never replaced by it:
    Stripe's PaymentIntent and Adyen's /payments are different things that both map
    to PAYMENT_INITIATION, and flattening that away would lose the distinction the
    comparison exists to expose.
    """

    SESSION_CREATION = "SESSION_CREATION"
    PAYMENT_INITIATION = "PAYMENT_INITIATION"
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    CAPTURE = "CAPTURE"
    STATUS_CHECK = "STATUS_CHECK"
    REFUND = "REFUND"
    VOID = "VOID"
    TOKENIZATION = "TOKENIZATION"
    HEALTH_CHECK = "HEALTH_CHECK"
    OTHER = "OTHER"


class TimelineEvent(str, Enum):
    """Normalized timeline milestones (section 8).

    Gateway flows differ, so not every transaction produces every event. A missing
    event is left missing rather than interpolated — section 9 forbids presenting a
    metric that could not be measured.
    """

    BENCHMARK_STARTED = "BENCHMARK_STARTED"
    SESSION_REQUEST_SENT = "SESSION_REQUEST_SENT"
    SESSION_RESPONSE_RECEIVED = "SESSION_RESPONSE_RECEIVED"
    HPP_URL_GENERATED = "HPP_URL_GENERATED"
    REDIRECT_INITIATED = "REDIRECT_INITIATED"
    HOSTED_PAGE_LOADED = "HOSTED_PAGE_LOADED"
    CUSTOMER_SUBMITTED = "CUSTOMER_SUBMITTED"
    PAYMENT_REQUEST_SENT = "PAYMENT_REQUEST_SENT"
    PAYMENT_RESPONSE_RECEIVED = "PAYMENT_RESPONSE_RECEIVED"
    THREE_DS_INITIATED = "THREE_DS_INITIATED"
    THREE_DS_COMPLETED = "THREE_DS_COMPLETED"
    AUTHORIZATION_REQUESTED = "AUTHORIZATION_REQUESTED"
    AUTHORIZATION_RESPONSE = "AUTHORIZATION_RESPONSE"
    RETURN_URL_RECEIVED = "RETURN_URL_RECEIVED"
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    FINAL_STATUS_CONFIRMED = "FINAL_STATUS_CONFIRMED"
    ERROR = "ERROR"


class TestMethodology(str, Enum):
    """Our own test methodology label (section 41).

    Explicitly *not* a claim about the gateway's internal warm-up state, which is not
    observable from outside.
    """

    COLD = "cold"
    WARM = "warm"
    MIXED = "mixed"


class UserRole(str, Enum):
    ADMIN = "admin"
    VIEWER = "viewer"
