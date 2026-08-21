"""
Typed errors for the gateway layer.

Every failure mode §6 requires to be handled separately has its own class, so a
handler can distinguish "the network broke" from "the gateway said no" from
"the gateway sent something we could not parse". Collapsing these into one
exception type would make the benchmark's central comparison -- reliability per
gateway -- unmeasurable.

``DocumentationRequiredError`` is the odd one out and deserves an explanation:
§0.1 requires the live documentation to be read before an endpoint is
implemented, and §0.2 forbids guessing a field name or status code. When a doc
page could not be retrieved, the adapter method raises this instead of shipping
an invented request shape. It names the exact page needed, so the gap is a
visible, actionable TODO in the UI and the API rather than a silent wrong call
to a real payment gateway.
"""

from __future__ import annotations

from typing import Any, Optional


class GatewayError(Exception):
    """Base class for every failure originating in the gateway layer."""

    #: Stored in ``transactions.error_code``. Short, stable, greppable.
    code = "gateway_error"
    #: The transaction status this failure maps to. ``ERROR`` unless the
    #: gateway genuinely answered.
    status = "ERROR"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class UnsupportedOperationError(GatewayError):
    """
    The adapter does not implement this operation (§3).

    The API surfaces supported operations per gateway so the frontend can hide
    or disable the control rather than letting a user hit a dead end.
    """

    code = "unsupported_operation"

    def __init__(self, gateway: str, operation: str) -> None:
        super().__init__(
            f"{gateway} does not support the {operation} operation."
        )
        self.gateway = gateway
        self.operation = operation


class DocumentationRequiredError(GatewayError):
    """
    The operation is intentionally unimplemented pending doc verification.

    Raised rather than guessing a request shape (§0.2). ``doc_url`` names the
    page that must be read before this method can be written.
    """

    code = "documentation_required"

    def __init__(self, operation: str, doc_url: str, missing: str) -> None:
        super().__init__(
            f"{operation} is not implemented because its request/response shape "
            f"could not be verified against {doc_url}. Missing: {missing}. "
            f"Per the build rules, an unverified shape is never sent to a real "
            f"payment gateway."
        )
        self.operation = operation
        self.doc_url = doc_url
        self.missing = missing


class GatewayTimeoutError(GatewayError):
    """The gateway did not respond inside the configured timeout."""

    code = "timeout"
    status = "TIMEOUT"

    def __init__(self, message: str, *, timeout_seconds: float) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds


class GatewayNetworkError(GatewayError):
    """DNS, TLS, connection reset -- the request never completed."""

    code = "network_error"


class GatewayHTTPStatusError(GatewayError):
    """A non-2xx response. The gateway answered; it answered badly."""

    code = "http_status"

    def __init__(self, message: str, *, status_code: int, body: Any = None) -> None:
        super().__init__(message, detail=body)
        self.status_code = status_code


class GatewayResponseParseError(GatewayError):
    """A 2xx response whose body was not the JSON the adapter expected."""

    code = "parse_error"


class GatewayDeclined(GatewayError):
    """
    The gateway completed the round trip and declined.

    This is a ``FAILED`` transaction, not an ``ERROR`` one: the call itself
    worked, which is what the latency benchmark is measuring.
    """

    code = "declined"
    status = "FAILED"

    def __init__(
        self,
        message: str,
        *,
        gateway_code: Optional[str] = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.gateway_code = gateway_code


class CredentialsMissing(GatewayError):
    """A credential the adapter needs is not configured for this gateway."""

    code = "credentials_missing"

    def __init__(self, gateway: str, keys: list[str]) -> None:
        super().__init__(
            f"{gateway} is missing required credentials: {', '.join(keys)}. "
            f"Add them on the Settings page."
        )
        self.gateway = gateway
        self.keys = keys


class ProductionGatewayBlocked(GatewayError):
    """
    A live-flagged credential was used while ALLOW_PRODUCTION_GATEWAYS is off.

    Enforced at the adapter layer, not in the UI, so calling the API directly
    does not bypass it (§7b).
    """

    code = "production_blocked"

    def __init__(self, gateway: str) -> None:
        super().__init__(
            f"{gateway} is configured with production credentials and "
            f"ALLOW_PRODUCTION_GATEWAYS is false. Refusing to transact."
        )
        self.gateway = gateway


class FeatureDisabled(GatewayError):
    """A server-side feature gate refused the request (e.g. raw card entry)."""

    code = "feature_disabled"

    def __init__(self, feature: str, env_var: str) -> None:
        super().__init__(
            f"The {feature} feature is disabled on this deployment. "
            f"Set {env_var} to enable it."
        )
        self.feature = feature
        self.env_var = env_var


class MerchantCapabilityError(GatewayError):
    """
    The gateway accepted the call but the merchant account cannot do this.

    §4c needs this distinct from a generic failure: pre-authorisation is
    platform-restricted at Geidea, and "your merchant account does not support
    pre-auth" must not read to the user as "pre-auth is broken".
    """

    code = "merchant_capability"
    status = "FAILED"

    def __init__(self, message: str, *, gateway_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.gateway_code = gateway_code


class InvalidTransactionState(GatewayError):
    """
    The requested operation is not legal for the transaction's current state.

    Refunding an uncaptured authorisation (§4e), capturing more than was
    authorised (§4d), voiding a captured payment (§4f). Blocked before any
    outbound call, so the gateway is never asked a question we already know the
    answer to.
    """

    code = "invalid_state"
    status = "FAILED"
