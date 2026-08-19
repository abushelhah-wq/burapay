"""
The adapter contract (specification section 6).

Every gateway implements the same interface; nothing gateway-specific lives outside
an adapter. Adding a seventh gateway means writing one file and registering it — no
route, service or model changes (section 61).

Why the interface has the shape it does
---------------------------------------
HPP is inherently two-legged: the server creates a session, the browser leaves for
the gateway's page, and only when it comes back can the server confirm the outcome.
That is ``create_hpp_session()`` then ``confirm_hpp_payment()``, with the customer's
time on the gateway's page sitting between them — recorded as customer interaction
time, never as gateway latency (section 39).

Direct API completes inside one server-side flow: ``process_direct_payment()``.

Adapters declare their credential fields rather than reading environment variables,
so the Settings page can render a form for a gateway it has never heard of and the
same fields drive validation, masking and "not configured" detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

from app.core.errors import GatewayError, NotConfigured, NotSupported
from app.gateways.http import InstrumentedClient
from app.models.enums import IntegrationType, TransactionStatus


@dataclass(frozen=True)
class CredentialField:
    """One configurable credential, as rendered by the Settings page."""

    key: str
    label: str
    required: bool = True
    #: Secret fields are masked on the way out and never returned in full.
    secret: bool = False
    placeholder: str = ""
    help_text: str = ""
    #: A non-secret field with a sensible default, e.g. an API base URL.
    default: Optional[str] = None
    choices: Sequence[str] = ()


@dataclass
class Card:
    """Test card details.

    Only ever a sandbox test card, only ever held in memory for the duration of one
    request, and never written anywhere (specification section 25). The sanitizer
    truncates any PAN that reaches a log or a stored snippet regardless.
    """

    number: str
    month: str
    year: str
    cvc: str
    holder: str = "BuraPay Benchmark"


@dataclass
class PaymentRequest:
    """What is being charged, plus the URLs the gateway needs to reach us."""

    amount: float
    currency: str
    reference: str
    description: str = "BuraPay benchmark transaction"
    return_url: str = ""
    webhook_url: str = ""
    card: Optional[Card] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HppSession:
    """The result of the first HPP leg."""

    redirect_url: str
    gateway_reference: str
    #: Whatever ``confirm_hpp_payment`` needs to look the payment up later. Sanitized
    #: before storage; must never hold a credential.
    context: Dict[str, Any] = field(default_factory=dict)
    #: ``redirect`` for a plain 303 to the gateway's page; ``widget`` when the gateway
    #: hands back an id for its own browser SDK (Adyen Sessions, HyperPay Copy&Pay)
    #: and the browser must mount that instead.
    mode: str = "redirect"


@dataclass
class PaymentResult:
    """The outcome of a payment operation, normalized but not flattened."""

    status: TransactionStatus
    gateway_reference: Optional[str] = None
    #: The gateway's own code and message, preserved verbatim (section 37).
    gateway_code: Optional[str] = None
    gateway_message: Optional[str] = None
    three_ds_required: bool = False
    context: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is TransactionStatus.SUCCESS


@dataclass
class HealthProbe:
    """The result of a read-only reachability check (section 36)."""

    available: bool
    response_time_ms: Optional[float] = None
    http_status: Optional[int] = None
    detail: str = ""


class PaymentGatewayAdapter:
    """Base class for every gateway adapter."""

    #: Stable identifier used in URLs, the database and the webhook path.
    code: str = "adapter"
    display_name: str = "Adapter"
    supports_hpp: bool = False
    supports_direct: bool = False
    #: Currencies this gateway is known to accept. Empty means "not declared" — the
    #: UI says so rather than implying universal support (section 3).
    supported_currencies: Sequence[str] = ()
    credential_fields: Sequence[CredentialField] = ()
    docs_url: str = ""
    #: Caveats shown next to this gateway's results.
    notes: Sequence[str] = ()
    #: What the vendor documents as the minimum call count per integration, shown
    #: beside the measured count so a divergence is visible immediately.
    documented_calls: Mapping[str, str] = {}

    def __init__(self, credentials: Mapping[str, Any], *, environment: str = "sandbox",
                 public_base_url: str = "") -> None:
        self.credentials = dict(credentials or {})
        self.environment = environment
        self.public_base_url = public_base_url.rstrip("/")

    # -- configuration ---------------------------------------------------- #

    @classmethod
    def secret_field_names(cls) -> set[str]:
        return {f.key for f in cls.credential_fields if f.secret}

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = self.credentials.get(key)
        if value is None or value == "":
            field_default = next((f.default for f in self.credential_fields
                                  if f.key == key), None)
            return default if field_default is None else field_default
        return str(value)

    def missing_credentials(self) -> List[str]:
        return [f.key for f in self.credential_fields
                if f.required and not self.credentials.get(f.key)]

    def is_configured(self) -> bool:
        return not self.missing_credentials()

    def require_configured(self) -> None:
        missing = self.missing_credentials()
        if missing:
            raise NotConfigured(
                f"{self.display_name} is not configured: missing "
                f"{', '.join(missing)}. Set these on the Settings page.")

    def require_supports(self, integration: IntegrationType) -> None:
        supported = (self.supports_hpp if integration is IntegrationType.HPP
                     else self.supports_direct)
        if not supported:
            raise NotSupported(
                f"{self.display_name} does not support "
                f"{'HPP / Hosted Checkout' if integration is IntegrationType.HPP else 'Direct API'}.")

    def supports_currency(self, currency: str) -> bool:
        if not self.supported_currencies:
            return True          # not declared — allow, and let the gateway decide
        return currency.upper() in {c.upper() for c in self.supported_currencies}

    # -- HTTP ------------------------------------------------------------- #

    def known_secrets(self) -> List[str]:
        return [str(self.credentials[key]) for key in self.secret_field_names()
                if self.credentials.get(key)]

    def auth(self) -> Optional[httpx.Auth]:
        """Auth applied to every request. Overridden per gateway."""
        return None

    def default_headers(self) -> Dict[str, str]:
        return {"User-Agent": "BuraPay-Benchmark/1.0"}

    def build_client(self, timeout: float = 30.0) -> InstrumentedClient:
        """A fresh instrumented client for one transaction."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), headers=self.default_headers(),
            auth=self.auth(), follow_redirects=False)
        return InstrumentedClient(gateway_code=self.code, client=client,
                                  known_secrets=self.known_secrets())

    # -- the payment interface -------------------------------------------- #

    async def create_hpp_session(self, client: InstrumentedClient,
                                 request: PaymentRequest) -> HppSession:
        """HPP leg 1: create the session and return where to send the browser."""
        raise NotSupported(f"{self.display_name}: HPP is not implemented.")

    async def confirm_hpp_payment(self, client: InstrumentedClient,
                                  request: PaymentRequest, context: Mapping[str, Any],
                                  params: Mapping[str, str]) -> PaymentResult:
        """HPP leg 2: confirm the outcome server-side after the customer returns."""
        raise NotSupported(f"{self.display_name}: HPP confirmation is not implemented.")

    async def process_direct_payment(self, client: InstrumentedClient,
                                     request: PaymentRequest) -> PaymentResult:
        """Direct API: the whole flow, server-side, in one call sequence."""
        raise NotSupported(f"{self.display_name}: Direct API is not implemented.")

    async def create_payment(self, client: InstrumentedClient, request: PaymentRequest,
                             integration: IntegrationType) -> Any:
        """Dispatch to the right integration. Convenience for callers."""
        if integration is IntegrationType.HPP:
            return await self.create_hpp_session(client, request)
        return await self.process_direct_payment(client, request)

    async def get_payment_status(self, client: InstrumentedClient,
                                 payment_id: str) -> PaymentResult:
        raise NotSupported(f"{self.display_name}: status lookup is not implemented.")

    async def refund_payment(self, client: InstrumentedClient, payment_id: str,
                             amount: Optional[float] = None,
                             currency: Optional[str] = None) -> PaymentResult:
        raise NotSupported(f"{self.display_name}: refund is not implemented.")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        """Turn a raw webhook into ``{event_type, reference, gateway_reference,
        status, signature_verified, payload}``.

        ``signature_verified`` is ``None`` when the gateway offers no signature or the
        secret is not configured — never ``True`` on the strength of an unchecked
        request.
        """
        raise NotSupported(f"{self.display_name}: webhook parsing is not implemented.")

    async def health_probe(self, client: InstrumentedClient) -> HealthProbe:
        """A read-only reachability probe. Never a payment (section 36)."""
        raise NotSupported(f"{self.display_name}: no safe health endpoint is available.")

    # -- shared helpers --------------------------------------------------- #

    @staticmethod
    def minor_units(amount: float) -> int:
        """Smallest currency unit, for gateways that transmit integer amounts."""
        return int(round(amount * 100))

    @staticmethod
    def decimal_amount(amount: float) -> str:
        """Two-decimal string, for gateways that sign or transmit decimal amounts."""
        return f"{amount:.2f}"

    @staticmethod
    async def json_body(client: InstrumentedClient, response: httpx.Response,
                        what: str) -> Dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayError(
                f"{what}: expected JSON, got HTTP {response.status_code}",
                http_status=response.status_code) from exc
        if not isinstance(body, dict):
            raise GatewayError(f"{what}: expected a JSON object, got {type(body).__name__}",
                               http_status=response.status_code)
        return body

    async def expect_ok(self, client: InstrumentedClient, response: httpx.Response,
                        what: str, accept: Sequence[int] = (200, 201, 202)) -> Dict[str, Any]:
        """Assert an acceptable HTTP status and return the parsed body."""
        if response.status_code not in tuple(accept):
            body_excerpt = response.text[:300]
            raise GatewayError(f"{self.display_name}: {what} returned HTTP "
                               f"{response.status_code}: {body_excerpt}",
                               http_status=response.status_code)
        return await self.json_body(client, response, what)
