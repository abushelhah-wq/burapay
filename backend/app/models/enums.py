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


class PaymentMode(str, Enum):
    """How the card is presented for a Direct API payment.

    A stored-credential payment is a different flow with a different call count from a
    one-off card payment, so it is recorded and compared separately rather than
    averaged in with it.
    """

    #: A one-off payment with card details. The default.
    STANDARD = "standard"
    #: A one-off payment that also asks the gateway to store the card and return a
    #: token, so a later merchant-initiated charge has something to charge.
    STORE_CARD = "store_card"
    #: A merchant-initiated payment against a stored card. No card details are sent —
    #: only the token and the agreement it was stored under.
    TOKEN = "token"


PAYMENT_MODE_LABELS = {
    PaymentMode.STANDARD.value: "Standard card payment",
    PaymentMode.STORE_CARD.value: "Store card (tokenize)",
    PaymentMode.TOKEN.value: "Stored token (merchant-initiated)",
}


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
    """Who may do what (specification section 10).

    ``USER`` replaces the older ``viewer``: a viewer could only read, while a normal
    BuraPay user runs payment tests and performs the transaction operations the study
    needs. The stored values are upper case to match the specification's vocabulary;
    :func:`normalize_role` maps the historical lower-case values so a token or a row
    written by an earlier version still reads back correctly.
    """

    ADMIN = "ADMIN"
    USER = "USER"


#: Values written by versions before roles were renamed, and the case-insensitive
#: spellings a hand-written API call is likely to use.
_ROLE_ALIASES = {
    "admin": UserRole.ADMIN,
    "administrator": UserRole.ADMIN,
    "viewer": UserRole.USER,
    "user": UserRole.USER,
}


def normalize_role(value: str) -> UserRole:
    """Map any accepted spelling of a role onto the canonical enum.

    Raises ``ValueError`` for anything unrecognised, which the API turns into a 422
    rather than silently granting the safer-looking role — a typo that quietly became
    ``USER`` would be a bug an administrator could not see.
    """
    try:
        return _ROLE_ALIASES[str(value).strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"unsupported role {value!r}; expected one of "
            f"{', '.join(role.value for role in UserRole)}") from exc


class UserStatus(str, Enum):
    """Account state (specification section 10).

    A boolean cannot express the difference between an account an administrator
    switched off and one the platform locked after repeated failed logins, and the
    two need different remedies — so this is a status, not an ``is_active`` flag.
    """

    ACTIVE = "ACTIVE"
    #: Disabled by an administrator. The preferred alternative to deletion, because it
    #: keeps audit history and transaction ownership intact.
    INACTIVE = "INACTIVE"
    #: Locked by brute-force protection. Clears itself once the lockout window passes,
    #: or immediately when an administrator re-enables the account.
    LOCKED = "LOCKED"

    @property
    def can_sign_in(self) -> bool:
        return self is UserStatus.ACTIVE


_STATUS_ALIASES = {status.value.lower(): status for status in UserStatus}


def normalize_status(value: str) -> UserStatus:
    try:
        return _STATUS_ALIASES[str(value).strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"unsupported status {value!r}; expected one of "
            f"{', '.join(status.value for status in UserStatus)}") from exc


class AuditEvent(str, Enum):
    """Auditable events (specification section 10).

    An audit row records who did what to whom, never what the secret was: a password,
    a hash and a token are all absent from every payload written here.
    """

    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_DISABLED = "USER_DISABLED"
    USER_ENABLED = "USER_ENABLED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_PASSWORD_RESET = "USER_PASSWORD_RESET"
    USER_PASSWORD_CHANGED = "USER_PASSWORD_CHANGED"
    USER_LOCKED = "USER_LOCKED"

    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
