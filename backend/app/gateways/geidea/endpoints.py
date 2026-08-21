"""
Geidea endpoint catalogue, with the provenance of every entry.

WHY THIS FILE EXISTS
--------------------
§0.1 requires each Geidea documentation page to be fetched and read before the
matching endpoint is implemented, and §0.2 forbids inventing a field name,
status code or error format. ``docs.geidea.net`` is still unreachable from this
build environment -- the egress proxy refuses the domain -- so pages cannot be
fetched here directly.

Four pages were fetched externally and committed to ``docs/geidea/``:

    01-tokenization.md                  tokenization / CIT
    02-merchant-initiated-mit.md        MIT, incl. its exact signature algorithm
    03-standalone-save-card.md          the separate save-card session endpoint
    04-webhook-callback-notifications.md callback signature + lifecycle edges

Everything those pages cover is now ``DOC_VERIFIED`` and implemented. What they
do NOT cover stays blocked or flagged, in particular the **base/HPP session
signature**, which all four pages defer to ``geidea-checkout-v2#signature``
without reproducing. See ``docs/geidea/00-README.md`` for the outstanding list.

HOW TO CLEAR THE REMAINING GAPS
-------------------------------
Fetch the page (``<url>.md`` returns clean markdown), commit it under
``docs/geidea/``, then update the entry here: set
``provenance=Provenance.DOC_VERIFIED``, fill in ``verified_fields`` and
``verified_on``. The adapter needs no other change --
``DocumentationRequiredError`` stops being raised as soon as an operation's
endpoints are no longer ``UNDOCUMENTED``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

DOCS_ROOT = "https://docs.geidea.net/docs"
REFERENCE_ROOT = "https://docs.geidea.net/reference"


class Provenance(str, enum.Enum):
    """How much evidence stands behind an endpoint definition."""

    #: Read from the documentation page itself. The page is committed under
    #: ``docs/geidea/`` so the claim is auditable rather than asserted.
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
    #: True for an entry that is not a callable endpoint but a caveat attached
    #: to one -- BASE_SIGNATURE is a computed value, not a request. An advisory
    #: entry is reported as a gap but never blocks an operation, because there
    #: is no call for it to block.
    advisory: bool = False

    @property
    def usable(self) -> bool:
        """True when the adapter is permitted to send this request."""
        return self.provenance in (
            Provenance.DOC_VERIFIED, Provenance.DOC_DERIVED_UNVERIFIED
        )

    @property
    def blocks_operation(self) -> bool:
        return not self.usable and not self.advisory

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
    # Path and body fields are confirmed by worked samples in
    # docs/geidea/01-tokenization.md and docs/geidea/02-merchant-initiated-mit.md.
    # The signature VALUE is tracked separately as BASE_SIGNATURE below, because
    # it is the one thing those pages defer rather than state.
    provenance=Provenance.DOC_VERIFIED,
    verified_on="2026-08-21",
    verified_fields=(
        "amount", "currency", "merchantReferenceId", "timestamp", "signature",
        "callbackUrl", "returnUrl", "language", "cardOnFile", "tokenId",
        "initiatedBy", "cofAgreement", "agreementId", "agreementType",
        "paymentOperation",
    ),
    request_fields=(
        "amount", "currency", "merchantReferenceId", "timestamp", "signature",
        "callbackUrl", "returnUrl",
    ),
    missing=(
        "The signature VALUE for this call is still inferred -- see "
        "BASE_SIGNATURE. Every other field on this endpoint is confirmed."
    ),
)

BASE_SIGNATURE = Endpoint(
    step_name="base_signature",
    method="",
    path="",
    doc_url=f"{DOCS_ROOT}/geidea-checkout-v2.md",
    provenance=Provenance.INFERRED,
    advisory=True,
    missing=(
        "The base/HPP session signature formula is the one thing all four "
        "fetched pages defer to geidea-checkout-v2#signature without "
        "reproducing. The implemented concatenation is a reconstruction. It is "
        "shipped because a wrong signature produces a clean, fully logged "
        "rejection -- it cannot move money to the wrong place -- and because "
        "the field order and timestamp format are both overridable from "
        "gateway config without a redeploy. The three signature formulas that "
        "ARE documented (MIT, save-card, callback) are implemented verbatim "
        "and are not affected by this gap."
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
    provenance=Provenance.DOC_VERIFIED,
    verified_on="2026-08-21",
    # Note "sessionid" -- lowercase 'i' -- exactly as the doc's sample shows.
    # Not normalised to sessionId: the sample is the only evidence available,
    # and "correcting" it would be inventing a field name.
    verified_fields=(
        "sessionid", "callbackUrl", "initiatedBy", "agreementId",
        "agreementType", "signature",
    ),
    request_fields=(
        "sessionid", "callbackUrl", "initiatedBy", "agreementId",
        "agreementType", "signature",
    ),
)

SAVE_CARD_SESSION = Endpoint(
    step_name="save_card_session",
    method="POST",
    path="/payment-intent/api/v2/direct/session/saveCard",
    doc_url=f"{DOCS_ROOT}/save-card.md",
    provenance=Provenance.DOC_VERIFIED,
    verified_on="2026-08-21",
    verified_fields=(
        "currency", "callbackUrl", "returnUrl", "language", "appearance",
        "cofAgreement", "merchantReferenceId", "signature", "timeStamp",
    ),
    request_fields=(
        "currency", "callbackUrl", "returnUrl", "merchantReferenceId",
        "signature", "timeStamp", "cofAgreement",
    ),
    # The page's prose says "/savecard/create-session" while its worked sample
    # posts to "/payment-intent/api/v2/direct/session/saveCard". The sample is
    # taken as authoritative: it is a literal, complete request, whereas the
    # prose is a paraphrase.
    missing=(
        "The page's prose names the endpoint /savecard/create-session, which "
        "contradicts its own worked sample. The sample's path is used. Confirm "
        "against a sandbox call before relying on it."
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
    provenance=Provenance.DOC_VERIFIED,
    verified_on="2026-08-21",
    # The signature travels in the callback BODY, as a top-level "signature"
    # key alongside "order" -- not in a header. Confirmed by the worked
    # callback payload in docs/geidea/01-tokenization.md.
    verified_fields=(
        "signature", "order.amount", "order.currency", "order.orderId",
        "order.status", "order.merchantReferenceId", "timestamp",
    ),
    missing=(
        "The page gives the field list and a Python reference implementation "
        "but no worked numeric example, and asks for the result to be checked "
        "against one real sandbox callback. Which timestamp field feeds the "
        "signature is not stated explicitly, so several candidates are tried "
        "and the one that verifies is recorded."
    ),
)


#: Every endpoint, for the capability/gap report the API exposes.
ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    CREATE_SESSION, BASE_SIGNATURE, ORDER_QUERY, INITIATE_AUTHENTICATION,
    AUTHENTICATE_PAYER, PAY, CAPTURE, REFUND, VOID, PAY_WITH_TOKEN,
    SAVE_CARD_SESSION, CANCEL_ORDER, WEBHOOK_SIGNATURE,
)

# ---------------------------------------------------------------------------
# Documented field values
# ---------------------------------------------------------------------------
# Verbatim from the fetched pages. Named constants rather than inline literals
# so a value that turns out to be wrong is corrected in one place, and so it is
# obvious which strings are Geidea's vocabulary rather than ours.

#: docs/geidea/02: the initiator on a customer-present tokenizing payment.
INITIATED_BY_INTERNET = "Internet"
#: docs/geidea/02: the initiator on a merchant-initiated charge.
INITIATED_BY_MERCHANT = "Merchant"
#: docs/geidea/02 and 03. The samples show "Unscheduled" in prose and
#: "unscheduled" in the JSON bodies; the JSON casing is used.
AGREEMENT_TYPE_UNSCHEDULED = "unscheduled"
#: docs/geidea/03: distinguishes a save-card session from a payment session.
PAYMENT_OPERATION_SAVE_CARD = "SaveCard"
PAYMENT_OPERATION_PAY = "Pay"
PAYMENT_OPERATION_AUTHORIZE = "Authorize"

#: docs/geidea/04, "Verification checklist before acting on a callback".
#: ALL of these must hold before a callback is treated as a success. A valid
#: signature with a non-000 response code is an authentic *failure* notice.
CALLBACK_SUCCESS_CHECKS: dict[str, str] = {
    "responseCode": "000",
    "responseMessage": "Success",
    "detailedResponseCode": "000",
    "detailedResponseMessage": "The operation was successful",
}

#: docs/geidea/04: an order stays InProgress while the customer is still on the
#: 3DS page or is retrying, and NO callback is sent during that time. Treating
#: "no callback yet" as failure would be wrong.
ORDER_STATUS_IN_PROGRESS = "InProgress"

#: docs/geidea/04: the one abandonment case that DOES produce a callback.
CANCELLED_BY_USER_MESSAGE = "Transaction Cancelled By User."


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
