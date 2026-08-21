"""
Geidea endpoint catalogue, with the provenance of every entry.

WHY THIS FILE EXISTS
--------------------
§0.1 requires each Geidea documentation page to be fetched and read before the
matching endpoint is implemented, and §0.2 forbids inventing a field name,
status code or error format. During this build ``docs.geidea.net`` was
**unreachable** -- the domain is refused by the deployment's egress proxy
(``403`` on CONNECT for ``docs.geidea.net``, ``geidea.net``, ``www.geidea.net``
and ``api.merchant.geidea.net``). No page could be fetched, so nothing here was
verified against a live doc during this build.

Rather than silently shipping remembered shapes, every endpoint carries an
explicit :class:`Provenance`, and the adapter refuses outright to call anything
marked ``UNDOCUMENTED``. The provenance is exposed through the API and rendered
in the UI, so an operator can see which flows rest on unverified ground before
they trust a measurement taken with them.

HOW TO CLEAR THE GAPS
---------------------
Fetch each page below as markdown (``<url>.md`` returns clean markdown) and
update the entry: set ``provenance=Provenance.DOC_VERIFIED``, fill in
``verified_fields``, and set ``verified_on`` to the date. The adapter needs no
other change -- ``DocumentationRequiredError`` stops being raised as soon as an
operation's endpoints are no longer ``UNDOCUMENTED``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

DOCS_ROOT = "https://docs.geidea.net/docs"
REFERENCE_ROOT = "https://docs.geidea.net/reference"


class Provenance(str, enum.Enum):
    """How much evidence stands behind an endpoint definition."""

    #: Read from the live doc page during this build. Nothing has this yet --
    #: see the module docstring.
    DOC_VERIFIED = "doc_verified"

    #: Endpoint path, method and field names recorded in this repository's
    #: earlier documentation study (``docs/02_api_flow_comparison.md``), which
    #: cites the doc page it came from. Not re-verified during this build
    #: because the docs host is unreachable.
    DOC_DERIVED_UNVERIFIED = "doc_derived_unverified"

    #: A shape that was never stated in the documentation and was reconstructed
    #: by inference. The previous release's README lists these explicitly as
    #: open questions. Sent only where a wrong value produces a clean rejection
    #: rather than an incorrect money movement -- and never silently.
    INFERRED = "inferred"

    #: No information at all. The adapter raises DocumentationRequiredError
    #: instead of calling anything.
    UNDOCUMENTED = "undocumented"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One Geidea HTTP endpoint and the evidence behind it."""

    step_name: str
    method: str
    path: str
    doc_url: str
    provenance: Provenance
    #: Request fields believed to be required, with the same caveat as the
    #: endpoint itself.
    request_fields: tuple[str, ...] = ()
    #: What specifically is missing. Rendered in the UI and in the error raised
    #: when the operation is blocked.
    missing: str = ""
    verified_on: Optional[str] = None
    verified_fields: tuple[str, ...] = field(default=())

    @property
    def usable(self) -> bool:
        """True when the adapter is permitted to send this request."""
        return self.provenance in (
            Provenance.DOC_VERIFIED, Provenance.DOC_DERIVED_UNVERIFIED
        )

    def format(self, base: str, **params: str) -> str:
        return f"{base.rstrip('/')}{self.path.format(**params)}"


# ---------------------------------------------------------------------------
# Regional hosts
# ---------------------------------------------------------------------------
# Same paths, different host. A SAR-denominated sandbox is almost always
# KSA-issued. Source: docs/02_api_flow_comparison.md, citing the Geidea docs.

REGION_HOSTS: dict[str, str] = {
    "eg": "https://api.merchant.geidea.net",
    "egypt": "https://api.merchant.geidea.net",
    "global": "https://api.merchant.geidea.net",
    "ksa": "https://api.ksamerchant.geidea.net",
    "sa": "https://api.ksamerchant.geidea.net",
    "uae": "https://api.geidea.ae",
    "ae": "https://api.geidea.ae",
}
DEFAULT_REGION = "ksa"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

CREATE_SESSION = Endpoint(
    step_name="create_session",
    method="POST",
    path="/payment-intent/api/v2/direct/session",
    doc_url=f"{DOCS_ROOT}/geidea-checkout-v2.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=(
        "amount", "currency", "merchantReferenceId", "timestamp", "signature",
        "callbackUrl", "returnUrl",
    ),
    missing=(
        "The exact signature payload and timestamp format are NOT documented "
        "and are reconstructed by inference -- see signing.py."
    ),
)

ORDER_QUERY = Endpoint(
    step_name="order_query",
    method="GET",
    path="/pgw/api/v1/direct/order/{order_id}",
    doc_url=f"{DOCS_ROOT}/fetch-1.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    missing=(
        "The full order status vocabulary is not enumerated in the study this "
        "was taken from; unknown statuses are mapped to PENDING rather than "
        "guessed into SUCCESS or FAILED."
    ),
)

INITIATE_AUTHENTICATION = Endpoint(
    step_name="initiate_authentication",
    method="POST",
    path="/pgw/api/v6/direct/authenticate/initiate",
    doc_url=f"{DOCS_ROOT}/initiate-authentication-v-2.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=("sessionId", "paymentMethod"),
    missing=(
        "Whether a frictionless (no-challenge) result may skip Authenticate "
        "Payer is not documented. The adapter measures what the sandbox "
        "actually requires instead of assuming either way."
    ),
)

AUTHENTICATE_PAYER = Endpoint(
    step_name="authenticate_payer",
    method="POST",
    path="/pgw/api/v6/direct/authenticate/payer",
    doc_url=f"{DOCS_ROOT}/authenticate-payer-v2.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=("sessionId", "paymentMethod", "returnUrl"),
)

PAY = Endpoint(
    step_name="pay",
    method="POST",
    path="/pgw/api/v2/direct/pay",
    doc_url=f"{DOCS_ROOT}/pay-v2.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=("sessionId", "paymentMethod", "paymentOperation"),
    missing=(
        "The full paymentOperation enum was NOT found in the documentation "
        "study. 'Pay' and 'Authorize' are the two values in use; any other "
        "value would be a guess."
    ),
)

CAPTURE = Endpoint(
    step_name="capture",
    method="POST",
    path="/pgw/api/v1/direct/capture",
    doc_url=f"{REFERENCE_ROOT}/capture-transaction-1",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=("orderId", "captureAmount"),
)

REFUND = Endpoint(
    step_name="refund",
    method="POST",
    path="/pgw/api/v2/direct/refund",
    doc_url=f"{DOCS_ROOT}/refund-2.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=("orderId", "refundAmount", "currency", "timestamp", "signature"),
)

VOID = Endpoint(
    step_name="void",
    method="POST",
    path="/pgw/api/v3/direct/void",
    doc_url=f"{DOCS_ROOT}/void-1.md",
    provenance=Provenance.DOC_DERIVED_UNVERIFIED,
    request_fields=("orderId",),
)

PAY_WITH_TOKEN = Endpoint(
    step_name="pay_with_token",
    method="POST",
    path="/pgw/api/v2/direct/pay/token",
    doc_url=f"{DOCS_ROOT}/merchant-initiated-mit.md",
    # The PATH is doc-derived, but the fields that mark a charge as
    # merchant-initiated versus customer-initiated are not. §4i is explicit
    # that those indicator flags must come from the doc rather than be
    # guessed, so the operations that need them stay blocked.
    provenance=Provenance.UNDOCUMENTED,
    missing=(
        "The initiator/indicator field names and their permitted values "
        "(merchant- vs customer-initiated, agreement type, consent record "
        "schema) are not field-level specified in the material available. §4i "
        "requires these to be read from the doc, not inferred."
    ),
)

TOKENIZE = Endpoint(
    step_name="tokenize",
    method="POST",
    path="",
    doc_url=f"{DOCS_ROOT}/save-card.md",
    provenance=Provenance.UNDOCUMENTED,
    missing=(
        "No endpoint path, request shape or response shape for a stand-alone "
        "save-card / tokenization call is available. Both save-card.md and "
        "tokenization.md are required."
    ),
)

CANCEL_ORDER = Endpoint(
    step_name="cancel_order",
    method="POST",
    path="",
    doc_url=f"{DOCS_ROOT}/cancel-order-1.md",
    provenance=Provenance.UNDOCUMENTED,
    missing="No endpoint path or request shape available for Cancel Order.",
)

WEBHOOK_SIGNATURE = Endpoint(
    step_name="verify_webhook",
    method="",
    path="",
    doc_url=f"{DOCS_ROOT}/sample-callback-responses.md",
    provenance=Provenance.UNDOCUMENTED,
    missing=(
        "The callback signature algorithm, the header carrying it, and the "
        "exact string it is computed over are all unknown. A verifier written "
        "without them would either reject every genuine callback or, worse, "
        "accept forged ones -- so callbacks are stored with "
        "signature_valid = NULL ('not verifiable') rather than a fabricated "
        "true/false."
    ),
)


#: Every endpoint, for the capability/gap report the API exposes.
ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    CREATE_SESSION, ORDER_QUERY, INITIATE_AUTHENTICATION, AUTHENTICATE_PAYER,
    PAY, CAPTURE, REFUND, VOID, PAY_WITH_TOKEN, TOKENIZE, CANCEL_ORDER,
    WEBHOOK_SIGNATURE,
)


# ---------------------------------------------------------------------------
# Response conventions
# ---------------------------------------------------------------------------

#: Geidea reports the business outcome in ``responseCode``, not only in the
#: HTTP status: '000' is success. Recorded in the documentation study.
#:
#: TODO(docs): the remainder of the code table -- and in particular which codes
#: mean "this merchant account is not enabled for pre-authorisation" (§4c) --
#: requires https://docs.geidea.net/docs/api-response-codes-and-messages.md,
#: which could not be fetched. Until then the adapter reports the gateway's own
#: code and message verbatim instead of translating an unknown code into a
#: category it may not belong to.
SUCCESS_RESPONSE_CODE = "000"

#: TODO(docs): populate from api-response-codes-and-messages.md. Empty on
#: purpose: an empty mapping degrades to "report the raw code", whereas a
#: half-remembered mapping would mislabel real failures.
RESPONSE_CODE_MEANINGS: dict[str, str] = {}

#: TODO(docs): populate from https://docs.geidea.net/docs/test-cards.md. §5
#: requires the "use a Geidea test card" helper to be populated from the doc
#: rather than hardcoded from memory, so the API serves this list and the UI
#: shows an explanatory empty state while it is empty.
TEST_CARDS: tuple[dict[str, str], ...] = ()
