"""
The gateway adapter contract (§3).

Adding a gateway means writing one module under ``app/gateways/<name>/`` and
adding one line to :mod:`app.gateways.registry`. Nothing else in the codebase
changes -- that is the requirement in §1, and it is the reason every
gateway-specific concern (auth, signing, field names, error mapping) is behind
this interface.

Not every gateway supports every operation. An adapter declares what it can do
via :attr:`GatewayAdapter.capabilities`; the unimplemented methods raise
:class:`~app.gateways.errors.UnsupportedOperationError`, and the API publishes
the capability set so the frontend hides the control instead of offering a
button that cannot work.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.gateways.errors import (
    CredentialsMissing, ProductionGatewayBlocked, UnsupportedOperationError,
)
from app.gateways.http_client import GatewayAuth, GatewayHTTPClient
from app.gateways.results import (
    HPPSessionResult, OrderContext, OrderStatusResult, PaymentResult, RawCard,
    TokenResult,
)
from app.models import IntegrationTypeCode


class Capability(str, enum.Enum):
    """One entry per method on the adapter interface."""

    CREATE_HPP_SESSION = "create_hpp_session"
    DIRECT_API_SALE = "direct_api_sale"
    DIRECT_API_AUTH = "direct_api_auth"
    CAPTURE = "capture"
    PARTIAL_CAPTURE = "partial_capture"
    REFUND = "refund"
    PARTIAL_REFUND = "partial_refund"
    VOID = "void"
    TOKENIZE = "tokenize"
    CHARGE_WITH_TOKEN_CIT = "charge_with_token_cit"
    CHARGE_WITH_TOKEN_MIT = "charge_with_token_mit"
    QUERY_ORDER = "query_order"
    VERIFY_WEBHOOK = "verify_webhook"
    #: Multiple partial captures against one authorisation. Declared separately
    #: because §4d makes it a per-gateway question, not an assumption.
    MULTIPLE_PARTIAL_CAPTURES = "multiple_partial_captures"


@dataclass(slots=True)
class GatewayContext:
    """
    Everything an adapter is given for one operation.

    Credentials arrive here already decrypted by
    :class:`~app.security.credentials.CredentialStore`, resolved at call time
    and discarded with this object when the request ends (§7b). An adapter
    never reads ``os.environ`` and never holds a plaintext copy beyond the
    request that needed it.
    """

    gateway_id: int
    gateway_code: str
    display_name: str
    credentials: Mapping[str, str]
    config: Mapping[str, Any]
    timeout_seconds: float
    #: True when any credential in use is flagged as production. Checked
    #: against ALLOW_PRODUCTION_GATEWAYS before any outbound call.
    is_production: bool = False
    allow_production: bool = False
    transaction_id: Optional[int] = None
    #: Absolute URLs built from PUBLIC_BASE_URL by the service layer. An
    #: adapter must never construct one itself -- that is how a hardcoded
    #: domain gets in.
    return_url: Optional[str] = None
    callback_url: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class GatewayAdapter(abc.ABC):
    """
    Base class for every gateway integration.

    Subclasses override only the operations they support and declare them in
    :attr:`capabilities`. The default implementations raise
    ``UnsupportedOperationError`` so a missing method is a typed, catchable,
    displayable condition rather than an ``AttributeError`` at runtime.
    """

    #: Stable identifier, matching ``gateways.code``.
    code: str = ""
    display_name: str = ""

    #: Credential key names this adapter cannot operate without. The Settings
    #: page renders a field per entry, and the adapter refuses to run when one
    #: is absent rather than sending an unauthenticated request.
    required_credentials: tuple[str, ...] = ()
    #: Credentials that unlock extra flows but are not needed for a basic sale.
    optional_credentials: tuple[str, ...] = ()

    capabilities: frozenset[Capability] = frozenset()
    supported_integration_types: tuple[IntegrationTypeCode, ...] = ()

    #: Per-method documentation URLs, rendered in the UI next to each
    #: operation. §8 requires the doc page used to be linked from the code;
    #: keeping them in a mapping makes that machine-checkable.
    doc_urls: Mapping[str, str] = {}

    def __init__(self, context: GatewayContext) -> None:
        self.context = context

    # -- guards -----------------------------------------------------------

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise UnsupportedOperationError(self.display_name or self.code, capability.value)

    def require_credentials(self, *keys: str) -> dict[str, str]:
        """
        Fetch required credentials or raise listing every one that is missing.

        Reporting all of them at once matters: a Settings page that says
        "missing API password", is fixed, then says "missing merchant ID" wastes
        a round trip against a sandbox.
        """
        wanted = keys or self.required_credentials
        missing = [k for k in wanted if not self.context.credentials.get(k)]
        if missing:
            raise CredentialsMissing(self.display_name or self.code, missing)
        return {k: str(self.context.credentials[k]) for k in wanted}

    def guard_production(self) -> None:
        """
        Server-side production gate (§7b).

        Called by every adapter method that transacts, before the first
        outbound call. It lives here rather than in a route handler so calling
        the API directly cannot bypass it.
        """
        if self.context.is_production and not self.context.allow_production:
            raise ProductionGatewayBlocked(self.display_name or self.code)

    # -- HTTP -------------------------------------------------------------

    def auth(self) -> Optional[GatewayAuth]:
        """The signing/authentication strategy for this gateway. Override."""
        return None

    def client(self, *, timeout_seconds: Optional[float] = None) -> GatewayHTTPClient:
        """
        Build the shared logging client for this operation.

        This is the only way an adapter is allowed to reach the network (§0.3).
        """
        return GatewayHTTPClient(
            gateway_id=self.context.gateway_id,
            gateway_code=self.code,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else self.context.timeout_seconds
            ),
            auth=self.auth(),
            transaction_id=self.context.transaction_id,
        )

    # -- the interface (§3) ------------------------------------------------

    async def create_hpp_session(self, order: OrderContext) -> HPPSessionResult:
        raise UnsupportedOperationError(self.display_name or self.code, "create_hpp_session")

    async def direct_api_sale(self, order: OrderContext, card: RawCard) -> PaymentResult:
        raise UnsupportedOperationError(self.display_name or self.code, "direct_api_sale")

    async def direct_api_auth(self, order: OrderContext, card: RawCard) -> PaymentResult:
        raise UnsupportedOperationError(self.display_name or self.code, "direct_api_auth")

    async def capture(
        self, transaction: Any, amount_minor: Optional[int] = None
    ) -> PaymentResult:
        raise UnsupportedOperationError(self.display_name or self.code, "capture")

    async def refund(
        self, transaction: Any, amount_minor: Optional[int] = None
    ) -> PaymentResult:
        raise UnsupportedOperationError(self.display_name or self.code, "refund")

    async def void(self, transaction: Any) -> PaymentResult:
        raise UnsupportedOperationError(self.display_name or self.code, "void")

    async def tokenize(self, card: RawCard, order: Optional[OrderContext] = None) -> TokenResult:
        raise UnsupportedOperationError(self.display_name or self.code, "tokenize")

    async def charge_with_token_cit(self, token: Any, order: OrderContext) -> PaymentResult:
        raise UnsupportedOperationError(
            self.display_name or self.code, "charge_with_token_cit"
        )

    async def charge_with_token_mit(self, token: Any, order: OrderContext) -> PaymentResult:
        raise UnsupportedOperationError(
            self.display_name or self.code, "charge_with_token_mit"
        )

    async def query_order(self, transaction: Any) -> OrderStatusResult:
        raise UnsupportedOperationError(self.display_name or self.code, "query_order")

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        raise UnsupportedOperationError(self.display_name or self.code, "verify_webhook")

    # -- webhook interpretation -------------------------------------------

    def parse_webhook(self, body: Any) -> "WebhookEvent":
        """
        Normalise a verified callback into something the platform can act on.

        Separate from :meth:`verify_webhook` so an unverified payload is still
        stored and inspectable (§4k) without being trusted.
        """
        raise UnsupportedOperationError(self.display_name or self.code, "parse_webhook")


@dataclass(slots=True)
class WebhookEvent:
    """A callback reduced to the fields the reconciliation logic needs."""

    #: Gateway-supplied unique event id, when there is one. Used to detect a
    #: replay so a duplicate callback cannot double-process (§4k).
    event_reference: Optional[str] = None
    merchant_reference: Optional[str] = None
    gateway_order_id: Optional[str] = None
    gateway_transaction_id: Optional[str] = None
    status: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw: Any = None
