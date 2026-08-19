"""
The documentation-based half of the benchmark, as data.

Everything here comes from the research pass recorded in
docs/02_api_flow_comparison.md - each gateway's own live documentation, fetched
2026-08-18. Nothing here is measured; it is what the vendors *say*. The scripts
measure what the sandboxes actually *do*, and the workbook's "Documented vs
Measured" tab is where the two meet.

Where a claim could not be confirmed on the vendor's own domain, the entry says so
rather than reading as settled fact.
"""

from __future__ import annotations

from typing import Any, Dict, List

GATEWAYS: List[Dict[str, str]] = [
    {"name": "geidea", "label": "Geidea", "role": "baseline"},
    {"name": "adyen", "label": "Adyen", "role": "competitor"},
    {"name": "stripe", "label": "Stripe", "role": "competitor"},
    {"name": "checkout_com", "label": "Checkout.com", "role": "competitor"},
    {"name": "hyperpay", "label": "HyperPay", "role": "competitor"},
    {"name": "moyasar", "label": "Moyasar", "role": "competitor"},
]

METHODOLOGY: List[str] = [
    "'API call' means one outbound HTTP call from the merchant's backend to the "
    "gateway. Inbound webhooks are noted but not counted - the merchant does not "
    "initiate them, so they are not a round trip the merchant can time or retry.",

    "Client-side work (widget loads, browser tokenization via Frames/Stripe.js/Adyen "
    "encryption) is not counted as a merchant-server call. Where a gateway *requires* "
    "that step, it is flagged, because a low server-side count can understate real "
    "end-to-end latency.",

    "Call counts are the frictionless minimum - no 3DS challenge - unless the cell "
    "says otherwise. Ranges reflect a conditional step that only fires on a challenge.",

    "Each flow is repeated 30 times by default with 2 warmup runs discarded, so p95 "
    "reflects a real distribution rather than one anecdote. Percentiles are "
    "nearest-rank: every reported figure is a latency that was actually observed.",

    "Setup a real merchant would already have done - creating a payment so there is "
    "something to refund, minting a token so there is something to charge - is issued "
    "as an untimed prep call and excluded from both the count and the stats.",

    "Run the scripts from the same region your merchants are in. Cross-continental "
    "latency from a cloud session says nothing useful about a MENA merchant's "
    "experience, and it dominates every other difference being measured.",

    "Checkout.com returns 202 Accepted on capture/refund/void, and Adyen returns "
    "'received'. Those timings are time-to-ack, not time-to-settled; a comparison "
    "against a gateway that answers synchronously undercounts them.",
]

KEY_FINDINGS: List[str] = [
    "Geidea's Direct API is the only one of the six with a FIXED 4-call sequence "
    "(Session -> Initiate Authentication -> Authenticate Payer -> Pay) regardless of "
    "whether a 3DS challenge actually occurs. Every competitor documents a 1-2 call "
    "frictionless minimum with the extra call conditional on a real challenge. This is "
    "the primary hypothesis the live run exists to confirm or refute.",

    "Geidea and Moyasar are the only two that need a precursor object per recurring "
    "charge (Geidea a fresh session, Moyasar a token exchange). Adyen, Stripe, "
    "Checkout.com and HyperPay all charge a stored token in a single call.",

    "Hosted Checkout confirmation splits into two camps: webhook-only with no "
    "documented pull alternative (Adyen, Stripe) versus a pull-based GET treated as "
    "the primary server-side verification (Geidea, Checkout.com, HyperPay, Moyasar). "
    "Only the second camp lets the merchant time time-to-authoritative-status at all.",

    "Card-data handling makes the Direct API counts less comparable than they look. "
    "Only Geidea and HyperPay accept a raw PAN server-side for a first charge. "
    "Checkout.com requires client-side tokenization below SAQ D, Adyen's documented "
    "path assumes client-side encryption, and Moyasar's own docs contradict each "
    "other. A strict apples-to-apples raw-API comparison is clean only between Geidea "
    "and HyperPay.",

    "HyperPay has the largest documentation gap of the six - its dedicated 3DS page "
    "and both back-office tutorial pages 404'd, so its refund/void URL shape is "
    "inferred from Peach Payments on the same OPPWA engine. Its numbers carry the "
    "lowest confidence until sandbox-confirmed.",
]

# --------------------------------------------------------------------------- #
# Call-count matrix
# --------------------------------------------------------------------------- #

CALL_COUNT_MATRIX: Dict[str, Dict[str, str]] = {
    "geidea": {
        "hosted_checkout": "2 (create session + confirm order). Webhook can substitute "
                           "the second call; mandatoriness is not stated either way.",
        "direct_api": "4 FIXED (session -> initiate auth -> authenticate payer -> pay). "
                      "No documented frictionless shortcut.",
        "capture": "1 - POST /pgw/api/v1/direct/capture",
        "refund": "1 - POST /pgw/api/v2/direct/refund (supports repeated partials)",
        "void": "1 - POST /pgw/api/v3/direct/void (authorized-not-captured only)",
        "mit": "2 - fresh session per charge, then pay/token",
    },
    "adyen": {
        "hosted_checkout": "1 outbound + 1 inbound webhook. No pull-based status "
                           "endpoint found in the Sessions flow guide.",
        "direct_api": "2 frictionless (paymentMethods -> payments), 3 with a challenge "
                      "(+ payments/details). Adyen explicitly documents the one-call "
                      "frictionless resolution.",
        "capture": "1 - POST /payments/{psp}/captures",
        "refund": "1 - POST /payments/{psp}/refunds (must be captured first)",
        "void": "1 - POST /payments/{psp}/cancels (legacy /cancels valid <=24h)",
        "mit": "1 - POST /payments with storedPaymentMethodId + ContAuth",
    },
    "stripe": {
        "hosted_checkout": "1 outbound + 1 mandatory webhook (+1 optional GET; Stripe's "
                           "own reference handler makes it, so 2 outbound in practice).",
        "direct_api": "1-2 (create+confirm in one call; +1 if a PaymentMethod must be "
                      "minted from raw card data). 3DS adds 0 calls on automatic "
                      "confirmation, 1 on manual.",
        "capture": "1 - POST /v1/payment_intents/{id}/capture",
        "refund": "1 - POST /v1/refunds",
        "void": "1 - POST /v1/payment_intents/{id}/cancel",
        "mit": "1 - POST /v1/payment_intents with off_session=true, confirm=true",
    },
    "checkout_com": {
        "hosted_checkout": "2 (create + confirm). Docs explicitly warn against trusting "
                           "the front-end redirect alone.",
        "direct_api": "1 frictionless (201 is final), 2 with a challenge (202 + GET). "
                      "Assumes a pre-tokenized source below SAQ D.",
        "capture": "1 - POST /payments/{id}/captures (202 async; auto-voids after 7 days)",
        "refund": "1 - POST /payments/{id}/refunds (202 async)",
        "void": "1 - POST /payments/{id}/voids (202 async)",
        "mit": "1 - POST /payments with source.type=id, merchant_initiated=true",
    },
    "hyperpay": {
        "hosted_checkout": "2 (prepare checkout + mandatory status GET). Status GET is "
                           "throttled to 2/min and the checkout id is single-use.",
        "direct_api": "1 frictionless, 2 with a challenge - UNVERIFIED, rests on an "
                      "overview page; the dedicated 3DS reference page 404'd.",
        "capture": "1 (paymentType=CP) - URL shape UNVERIFIED on a HyperPay page",
        "refund": "1 (paymentType=RF) - URL shape UNVERIFIED on a HyperPay page",
        "void": "1 (paymentType=RV) - URL shape UNVERIFIED on a HyperPay page",
        "mit": "1 - POST /v1/registrations/{id}/payments with standingInstruction",
    },
    "moyasar": {
        "hosted_checkout": "2 (create invoice + confirm). callback_url optional, same "
                           "ambiguity as Geidea's callbackUrl.",
        "direct_api": "2 per the API reference (raw card + fetch) or 3 per the "
                      "recommended Custom UI guide (tokenize first). The docs disagree "
                      "with themselves.",
        "capture": "1 - POST /v1/payments/{id}/capture (14 days on Mada)",
        "refund": "1 - POST /v1/payments/{id}/refund",
        "void": "1 - POST /v1/payments/{id}/void (~2h window post-capture)",
        "mit": "2 - token minted first, then POST /v1/payments with source.type=token",
    },
}

# --------------------------------------------------------------------------- #
# Endpoint map
# --------------------------------------------------------------------------- #

def _step(step: int, endpoint: str, note: str = "", flagged: bool = False) -> Dict[str, Any]:
    return {"step": step, "endpoint": endpoint, "note": note, "flagged": flagged}


ENDPOINT_MAP: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "geidea": {
        "hosted_checkout": [
            _step(1, "POST /payment-intent/api/v2/direct/session",
                  "amount, currency, timestamp, signature, callbackUrl; 15-min TTL"),
            _step(2, "GET /pgw/api/v1/direct/order/{orderId}",
                  "server-side confirmation; a webhook to callbackUrl also fires"),
        ],
        "direct_api": [
            _step(1, "POST /payment-intent/api/v2/direct/session", "create session"),
            _step(2, "POST /pgw/api/v6/direct/authenticate/initiate",
                  "3DS method / device fingerprint; raw card data enters here"),
            _step(3, "POST /pgw/api/v6/direct/authenticate/payer",
                  "3DS challenge (OTP/ACS). Whether a frictionless path may skip this "
                  "is NOT documented", flagged=True),
            _step(4, "POST /pgw/api/v2/direct/pay", "authorize + capture (Sale)"),
        ],
        "capture": [_step(1, "POST /pgw/api/v1/direct/capture", "orderId, optional captureAmount")],
        "refund": [_step(1, "POST /pgw/api/v2/direct/refund", "orderId, refundAmount, signature")],
        "void": [_step(1, "POST /pgw/api/v3/direct/void", "orderId; authorized-not-captured only")],
        "mit": [
            _step(1, "POST /payment-intent/api/v2/direct/session", "fresh session per charge"),
            _step(2, "POST /pgw/api/v2/direct/pay/token",
                  "needs tokenId (from a cardOnFile:true session) + agreementId + a "
                  "consent record whose schema is not field-level specified", flagged=True),
        ],
    },
    "adyen": {
        "hosted_checkout": [
            _step(1, "POST /sessions", "Adyen positions this as deliberately one request"),
            _step(2, "(inbound) AUTHORISATION webhook",
                  "not a merchant-initiated call; no pull alternative found", flagged=True),
        ],
        "direct_api": [
            _step(1, "POST /paymentMethods", "payment method list for the transaction context"),
            _step(2, "POST /payments", "card data client-side encrypted in the standard path"),
            _step(3, "POST /payments/details", "conditional - only if an action object came back"),
        ],
        "capture": [_step(1, "POST /payments/{pspReference}/captures", "returns 'received'")],
        "refund": [_step(1, "POST /payments/{pspReference}/refunds", "must be captured first")],
        "void": [_step(1, "POST /payments/{pspReference}/cancels",
                       "legacy POST /cancels valid <=24h; impossible after capture")],
        "mit": [_step(1, "POST /payments",
                      "storedPaymentMethodId + shopperInteraction=ContAuth + "
                      "recurringProcessingModel; no session step")],
    },
    "stripe": {
        "hosted_checkout": [
            _step(1, "POST /v1/checkout/sessions", "create session, redirect to session.url"),
            _step(2, "GET /v1/checkout/sessions/{id}",
                  "the defensive re-check Stripe's own fulfillment sample makes"),
            _step(3, "(inbound) checkout.session.completed webhook",
                  "'Webhooks are required for fulfillment' - explicit callout", flagged=True),
        ],
        "direct_api": [
            _step(1, "POST /v1/payment_methods",
                  "only if raw card data must become a PaymentMethod; requires PCI compliance"),
            _step(2, "POST /v1/payment_intents", "confirm=true combines create and confirm"),
            _step(3, "POST /v1/payment_intents/{id}/confirm",
                  "only with confirmation_method=manual after a challenge"),
        ],
        "capture": [_step(1, "POST /v1/payment_intents/{id}/capture",
                          "only from status requires_capture")],
        "refund": [_step(1, "POST /v1/refunds", "charge or payment_intent")],
        "void": [_step(1, "POST /v1/payment_intents/{id}/cancel",
                       "auto-refunds any amount_capturable")],
        "mit": [_step(1, "POST /v1/payment_intents",
                      "customer + payment_method + off_session=true + confirm=true. A "
                      "refused SCA exemption lands in requires_payment_method and needs "
                      "a full on-session re-auth", flagged=True)],
    },
    "checkout_com": {
        "hosted_checkout": [
            _step(1, "POST /hosted-payments", "create the hosted session"),
            _step(2, "GET /hosted-payments/{id}",
                  "or GET /payments/{id}, or webhook; the redirect alone is not proof"),
        ],
        "direct_api": [
            _step(0, "POST /tokens (browser, public key)",
                  "client-side; not a merchant-server call unless the merchant holds SAQ D"),
            _step(1, "POST /payments", "201 is a final frictionless result; 202 means 3DS"),
            _step(2, "GET /payments/{id}", "conditional - only after a 202/Pending"),
        ],
        "capture": [_step(1, "POST /payments/{id}/captures",
                          "202 ack only; payment_captured webhook confirms", flagged=True)],
        "refund": [_step(1, "POST /payments/{id}/refunds",
                         "202 ack only; payment_refunded webhook confirms", flagged=True)],
        "void": [_step(1, "POST /payments/{id}/voids",
                       "202 ack only; payment_voided webhook confirms", flagged=True)],
        "mit": [_step(1, "POST /payments",
                      "source.type=id, payment_type=Unscheduled, merchant_initiated=true, "
                      "previous_payment_id. Explicitly SCA-exempt")],
    },
    "hyperpay": {
        "hosted_checkout": [
            _step(1, "POST /v1/checkouts", "prepare checkout, returns checkoutId"),
            _step(2, "GET /v1/checkouts/{checkoutId}/payment",
                  "mandatory; throttled 2/min per checkout and single-use once final"),
        ],
        "direct_api": [
            _step(1, "POST /v1/payments (paymentType=DB)",
                  "raw card and 3DS data inline; no separate initiate-auth call found"),
            _step(2, "GET {resourcePath}",
                  "conditional after a challenge redirect. The 1-2 call claim rests on "
                  "an overview page - the dedicated 3DS page 404'd", flagged=True),
        ],
        "capture": [_step(1, "POST /v1/payments/{id} (paymentType=CP)",
                          "URL shape inferred from Peach Payments on the same OPPWA "
                          "engine; unconfirmed on a HyperPay page", flagged=True)],
        "refund": [_step(1, "POST /v1/payments/{id} (paymentType=RF)",
                         "same caveat as capture", flagged=True)],
        "void": [_step(1, "POST /v1/payments/{id} (paymentType=RV)",
                       "same caveat as capture; cutoff-time limited", flagged=True)],
        "mit": [_step(1, "POST /v1/registrations/{registrationId}/payments",
                      "standingInstruction.mode=REPEATED, source=MIT. Confirmed on "
                      "HyperPay's own card-on-file page - the best-documented MIT "
                      "semantics of the six")],
    },
    "moyasar": {
        "hosted_checkout": [
            _step(1, "POST /v1/invoices", "returns the hosted url"),
            _step(2, "GET /v1/invoices/{id}", "confirm final status; callback_url is optional"),
        ],
        "direct_api": [
            _step(1, "POST /v1/tokens",
                  "tokenize-first path only, per the recommended Custom UI guide"),
            _step(2, "POST /v1/payments",
                  "source.type=token, or raw creditcard fields per the API reference", flagged=True),
            _step(3, "GET /v1/payments/{id}",
                  "needed regardless of 3DS; 3DS is a transaction_url field, not a call"),
        ],
        "capture": [_step(1, "POST /v1/payments/{id}/capture", "within 14 days on Mada")],
        "refund": [_step(1, "POST /v1/payments/{id}/refund", "")],
        "void": [_step(1, "POST /v1/payments/{id}/void",
                       "~2h window post-capture; docs prefer void over refund inside it")],
        "mit": [
            _step(1, "POST /v1/tokens (or save_card on the first charge)", "mint the token"),
            _step(2, "POST /v1/payments",
                  "source.type=token. No session concept; no consent/agreement field "
                  "documented beyond the token itself", flagged=True),
        ],
    },
}

# --------------------------------------------------------------------------- #
# Open data gaps
# --------------------------------------------------------------------------- #

DATA_GAPS: List[Dict[str, Any]] = [
    {"id": 1, "gateway": "Geidea",
     "gap": "Full enum of paymentOperation on the Pay endpoint - only 'Pay' is confirmed; "
            "an Authorize-only value is referenced in prose but never shown as a literal.",
     "resolution": "Postman/OpenAPI reference, or a Capture cross-link"},
    {"id": 2, "gateway": "Geidea",
     "gap": "Whether a frictionless (no-challenge) 3DS path can skip the Authenticate "
            "Payer call, dropping the Direct API below its documented fixed 4 calls. "
            "This is the single most consequential open question in the benchmark.",
     "resolution": "A live sandbox Direct API run with a non-enrolled test card"},
    {"id": 3, "gateway": "Geidea",
     "gap": "Exact schema for the cardholder consent record stored for MIT - prose only, "
            "no field-level spec.",
     "resolution": "Field-level MIT reference doc"},
    {"id": 4, "gateway": "Geidea",
     "gap": "Whether callbackUrl/webhooks are strictly mandatory or strongly recommended.",
     "resolution": "An explicit policy statement from Geidea"},
    {"id": 5, "gateway": "Adyen",
     "gap": "No pull/poll 'get session status' endpoint surfaced in the Sessions flow "
            "guide - only the webhook path is documented.",
     "resolution": "Adyen reporting/lookup API docs"},
    {"id": 6, "gateway": "Adyen",
     "gap": "Whether the raw-server-side-card ('API only') path has a different Advanced "
            "flow call count than the standard client-encrypted path.",
     "resolution": "The custom-card-integration doc"},
    {"id": 7, "gateway": "Adyen",
     "gap": "Exact JWT/sessionResult verification mechanics on the merchant server "
            "post-redirect - only referenced via an Android SDK field name.",
     "resolution": "An official narrative page on sessionResult verification"},
    {"id": 8, "gateway": "Stripe",
     "gap": "Exact SCA-exemption rule set for off-session MIT charges - when it succeeds "
            "versus falling to requires_payment_method.",
     "resolution": "Not enumerated in the pages fetched; needs Stripe support confirmation"},
    {"id": 9, "gateway": "Stripe",
     "gap": "Precise webhook delivery retry schedule and backoff.",
     "resolution": "A dedicated webhook retry reference page"},
    {"id": 10, "gateway": "Stripe",
     "gap": "Whether the 'up to 10 seconds' webhook-wait window on the Checkout redirect "
            "is a hard cap or a best-effort approximation.",
     "resolution": "A second source cross-verifying the fulfillment doc"},
    {"id": 11, "gateway": "Checkout.com",
     "gap": "Exact required fields for POST /hosted-payments - amount is 'required in "
            "practice' but not in the strict schema, and pages disagree.",
     "resolution": "A sandbox call that omits each candidate field"},
    {"id": 12, "gateway": "Checkout.com",
     "gap": "Whether a distinct 'fast refund without reference' endpoint exists as a "
            "sibling to the documented refund-with-reference path.",
     "resolution": "Fetch the sibling refund page"},
    {"id": 13, "gateway": "Checkout.com",
     "gap": "Whether POST /payments/{id}/captures has strictly-required fields beyond "
            "amount and reference.",
     "resolution": "Cross-check the OpenAPI schema"},
    {"id": 14, "gateway": "Checkout.com",
     "gap": "The raw-card server-to-server comparison against Geidea is asymmetric - "
            "SAQ D gate versus no stated gate. Methodological, not a fetch gap.",
     "resolution": "Flag the asymmetry in the write-up rather than resolving it"},
    {"id": 15, "gateway": "HyperPay",
     "gap": "Exact refund/void endpoint URL shape - both dedicated tutorial pages 404'd; "
            "current scripts use the shape inferred from Peach Payments.",
     "resolution": "A live sandbox call, or merchant-portal-gated docs"},
    {"id": 16, "gateway": "HyperPay",
     "gap": "The '1-2 call' Direct API claim rests on an overview page only - the "
            "dedicated 3DS reference page 404'd. Lowest-confidence headline number of "
            "the six.",
     "resolution": "A live sandbox Direct API run before publishing this claim"},
    {"id": 17, "gateway": "HyperPay",
     "gap": "Webhook mandatoriness for Copy&Pay - the nav lists a 'Webhook notifications' "
            "section whose content was not reachable.",
     "resolution": "Fetch that sub-page"},
    {"id": 18, "gateway": "HyperPay",
     "gap": "Whether card.holder is truly optional or conditionally required by scheme.",
     "resolution": "Sandbox test omitting card.holder"},
    {"id": 19, "gateway": "Moyasar",
     "gap": "Whether raw-card source.type=creditcard straight to /v1/payments is a fully "
            "supported production path, or whether the PCI stance effectively mandates "
            "tokenize-first. The reference schema and the guide disagree.",
     "resolution": "A reconciling page, or Moyasar support; MOYASAR_DIRECT_MODE lets you "
                   "measure both paths in the meantime"},
    {"id": 20, "gateway": "Moyasar",
     "gap": "Whether callback_url is truly optional or a de facto production requirement "
            "(same ambiguity as Geidea gap #4).",
     "resolution": "Explicit policy statement"},
    {"id": 21, "gateway": "Moyasar",
     "gap": "No documented customer-ID/consent-record field for MIT beyond the token id - "
            "could be a genuinely simpler model, or just undocumented.",
     "resolution": "Not verifiable from the pages fetched"},
    {"id": 22, "gateway": "Moyasar",
     "gap": "Exact capture/void timing windows for non-Mada card schemes - the Payment "
            "Operations guide states Mada-specific numbers only.",
     "resolution": "Scheme-specific documentation or support confirmation"},
]

# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

SOURCES: List[Dict[str, str]] = [
    {"gateway": "Geidea", "covers": "API overview",
     "url": "https://docs.geidea.net/docs/overview"},
    {"gateway": "Geidea", "covers": "Hosted Checkout v2",
     "url": "https://docs.geidea.net/docs/geidea-checkout-v2"},
    {"gateway": "Geidea", "covers": "Direct API",
     "url": "https://docs.geidea.net/docs/pg-direct-api"},
    {"gateway": "Geidea", "covers": "Initiate Authentication v2",
     "url": "https://docs.geidea.net/docs/initiate-authentication-v-2"},
    {"gateway": "Geidea", "covers": "Authenticate Payer v2",
     "url": "https://docs.geidea.net/docs/authenticate-payer-v2"},
    {"gateway": "Geidea", "covers": "Pay v2",
     "url": "https://docs.geidea.net/docs/pay-v2"},
    {"gateway": "Geidea", "covers": "Capture",
     "url": "https://docs.geidea.net/reference/capture-transaction-1"},
    {"gateway": "Geidea", "covers": "Refund",
     "url": "https://docs.geidea.net/docs/refund-2"},
    {"gateway": "Geidea", "covers": "Void",
     "url": "https://docs.geidea.net/reference/void-payment-1"},
    {"gateway": "Geidea", "covers": "MIT / recurring",
     "url": "https://docs.geidea.net/docs/merchant-initiated-mit"},
    {"gateway": "Geidea", "covers": "Fetch order status",
     "url": "https://docs.geidea.net/docs/fetch-1"},
    {"gateway": "Geidea", "covers": "Prerequisites / credentials",
     "url": "https://docs.geidea.net/docs/pre-requisites"},

    {"gateway": "Adyen", "covers": "Sessions flow (hosted)",
     "url": "https://docs.adyen.com/online-payments/build-your-integration/sessions-flow"},
    {"gateway": "Adyen", "covers": "Advanced flow (S2S)",
     "url": "https://docs.adyen.com/online-payments/build-your-integration/advanced-flow"},
    {"gateway": "Adyen", "covers": "Create a payment session",
     "url": "https://docs.adyen.com/api-explorer/Checkout/latest/post/sessions"},
    {"gateway": "Adyen", "covers": "/payments API explorer",
     "url": "https://docs.adyen.com/api-explorer/Checkout/latest/post/payments"},
    {"gateway": "Adyen", "covers": "Native 3DS2",
     "url": "https://docs.adyen.com/online-payments/3d-secure/native-3ds2"},
    {"gateway": "Adyen", "covers": "Capture / Refund / Cancel",
     "url": "https://docs.adyen.com/online-payments/capture"},
    {"gateway": "Adyen", "covers": "Tokenization / token payments",
     "url": "https://docs.adyen.com/online-payments/tokenization/make-token-payments"},
    {"gateway": "Adyen", "covers": "Webhooks",
     "url": "https://docs.adyen.com/development-resources/webhooks"},

    {"gateway": "Stripe", "covers": "Checkout quickstart",
     "url": "https://docs.stripe.com/checkout/quickstart"},
    {"gateway": "Stripe", "covers": "Create Checkout Session",
     "url": "https://docs.stripe.com/api/checkout/sessions/create"},
    {"gateway": "Stripe", "covers": "Fulfillment / webhook requirement",
     "url": "https://docs.stripe.com/checkout/fulfillment"},
    {"gateway": "Stripe", "covers": "Create PaymentIntent",
     "url": "https://docs.stripe.com/api/payment_intents/create"},
    {"gateway": "Stripe", "covers": "Confirm PaymentIntent",
     "url": "https://docs.stripe.com/api/payment_intents/confirm"},
    {"gateway": "Stripe", "covers": "Create PaymentMethod (PCI note)",
     "url": "https://docs.stripe.com/api/payment_methods/create"},
    {"gateway": "Stripe", "covers": "Refunds",
     "url": "https://docs.stripe.com/api/refunds/create"},
    {"gateway": "Stripe", "covers": "3DS authentication flow",
     "url": "https://docs.stripe.com/payments/3d-secure/authentication-flow"},
    {"gateway": "Stripe", "covers": "Save card during payment (MIT)",
     "url": "https://docs.stripe.com/payments/save-during-payment"},

    {"gateway": "Checkout.com", "covers": "Hosted payments page",
     "url": "https://www.checkout.com/docs/payments/accept-payments/"
            "accept-a-payment-on-a-hosted-page"},
    {"gateway": "Checkout.com", "covers": "Payments API",
     "url": "https://www.checkout.com/docs/payments/accept-payments/"
            "accept-a-payment-using-the-payments-api"},
    {"gateway": "Checkout.com", "covers": "Capture / Refund / Void",
     "url": "https://www.checkout.com/docs/payments/manage-payments/capture-a-payment"},
    {"gateway": "Checkout.com", "covers": "Unscheduled MIT with stored card",
     "url": "https://www.checkout.com/docs/payments/accept-payments/"
            "pay-with-stored-card-details/unscheduled-payments-with-stored-card-details"},
    {"gateway": "Checkout.com", "covers": "API reference",
     "url": "https://api-reference.checkout.com/"},
    {"gateway": "Checkout.com", "covers": "Sandbox signup",
     "url": "https://www.checkout.com/get-test-account"},

    {"gateway": "HyperPay", "covers": "Copy&Pay widget integration",
     "url": "https://hyperpay.docs.oppwa.com/integrations/widget"},
    {"gateway": "HyperPay", "covers": "Server-to-server integration",
     "url": "https://hyperpay.docs.oppwa.com/integrations/server-to-server"},
    {"gateway": "HyperPay", "covers": "Parameter reference",
     "url": "https://hyperpay.docs.oppwa.com/reference/parameters"},
    {"gateway": "HyperPay", "covers": "Card on file / MIT",
     "url": "https://hyperpay.docs.oppwa.com/tutorials/card-on-file"},
    {"gateway": "HyperPay", "covers": "Capture URL shape (cross-reference, same engine)",
     "url": "https://support.peachpayments.com/support/solutions/articles/"
            "47001240819-pre-authorization-pa-and-capture-cp-scenario"},
    {"gateway": "HyperPay", "covers": "Integration guide / test access",
     "url": "https://www.hyperpay.com/integration-guide/"},

    {"gateway": "Moyasar", "covers": "Authentication",
     "url": "https://docs.moyasar.com/api/authentication"},
    {"gateway": "Moyasar", "covers": "Create payment",
     "url": "https://docs.moyasar.com/api/payments/01-create-payment/"},
    {"gateway": "Moyasar", "covers": "Fetch payment",
     "url": "https://docs.moyasar.com/api/payments/02-fetch-payment"},
    {"gateway": "Moyasar", "covers": "Refund / Capture / Void",
     "url": "https://docs.moyasar.com/api/payments/05-refund-payment/"},
    {"gateway": "Moyasar", "covers": "Create invoice",
     "url": "https://docs.moyasar.com/api/invoices/01-create-invoice/"},
    {"gateway": "Moyasar", "covers": "Create token",
     "url": "https://docs.moyasar.com/api/other/tokens/create-token/"},
    {"gateway": "Moyasar", "covers": "Custom UI guide (tokenize-first)",
     "url": "https://docs.moyasar.com/guides/card-payments/custom-ui/"},
    {"gateway": "Moyasar", "covers": "Tokenized cards (MIT)",
     "url": "https://docs.moyasar.com/guides/tokenization/tokenized-cards"},
    {"gateway": "Moyasar", "covers": "Test cards",
     "url": "https://docs.moyasar.com/guides/card-payments/test-cards"},
]
