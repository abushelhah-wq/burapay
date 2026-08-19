# API Flow Comparison: Geidea vs. Adyen, Stripe, Checkout.com, HyperPay, Moyasar

This document maps each gateway's documented API call sequence for Hosted Checkout, Direct/Server-to-Server API, and stand-alone operations (Capture, Refund, Void, MIT), based on direct review of each gateway's official documentation — not general knowledge or assumptions. Every claim is sourced; documentation gaps are flagged explicitly rather than filled in with guesses.

This is the **call-count and structural half** of the benchmark. The response-time numbers come from running `/scripts` against live sandboxes — see `../README.md` and `01_sandbox_signup_guide.md`.

**Research date:** 2026-08-18.

**Methodology note:** "Merchant-server API call" = an outbound HTTP call the merchant's backend makes to the gateway. Inbound webhooks are counted separately and flagged, since they are pushed by the gateway, not timed by the merchant as a request/response round trip. Client-side/browser-only calls (widget loads, SDK tokenization in the browser) are noted but not counted in the merchant-server minimum unless otherwise stated.

---

## 1. Summary comparison table

Call counts are the minimum merchant-server-to-gateway API calls to go from "start" to "know the outcome," in the frictionless (no 3DS challenge) case unless noted. Ranges reflect a step that fires conditionally.

| Flow | Geidea | Adyen | Stripe | Checkout.com | HyperPay | Moyasar |
|---|---|---|---|---|---|---|
| **Hosted Checkout** | 2 (create + confirm) | 1 out + 1 webhook in | 1 out + 1 webhook in (+1 GET recommended) | 2 (create + confirm) | 2 (create + confirm, mandatory) | 2 (create + confirm) |
| **Direct API (S2S)** | 4 fixed (Session → Initiate Auth → Authenticate Payer → Pay) | *2–3* (paymentMethods → payments [→ details]) | *1–2* (create+confirm, +1 if raw card) | *1–2* (payments [→ GET status]) | *1–2* (payments [→ GET status], unverified) | *2–3* (payments → GET, or tokenize first) |
| **Capture** | 1 call | 1 call | 1 call | 1 call (async, 202) | 1 call (URL shape unverified) | 1 call |
| **Refund** | 1 call | 1 call | 1 call | 1 call (async, 202) | 1 call (URL shape unverified) | 1 call |
| **Void** | 1 call | 1 call (2 variants) | 1 call | 1 call (async, 202) | 1 call (URL shape unverified) | 1 call |
| **MIT (recurring)** | 2 (session + pay/token) | 1 call | 1 call | 1 call | 1 call | 2 calls (no session concept) |

### Headline finding

**Geidea's Direct/Server-to-Server API is the only one of the six with a *fixed* 4-call sequence regardless of whether a 3DS challenge actually occurs.** Every competitor documents a lower frictionless-case minimum (1–2 calls), with 3DS conditionally adding a call only when a challenge is actually presented. This is the single most concrete, well-sourced structural finding from the research phase, and the primary hypothesis worth confirming with live timing data.

Geidea's MIT flow is also the only one requiring a fresh session-creation call before each recurring charge — every competitor except Moyasar charges a stored token/registration directly in one call, and Moyasar's second call is a one-time token mint rather than a per-charge cost.

---

## 2. Per-gateway detail

### Geidea (baseline)

**Docs verified:** `docs.geidea.net` — note two separate namespaces, `/docs/...` (narrative) and `/reference/...` (API spec). Void and Capture only have precise specs under `/reference/`.

**Regional hosts** (same paths, different host): `api.merchant.geidea.net` (Egypt/global), `api.ksamerchant.geidea.net` (KSA), `api.geidea.ae` (UAE).

**Auth:** HTTP Basic (Public Key + API Password).

**Hosted Checkout — 2 calls**

1. `POST /payment-intent/api/v2/direct/session` — create session (amount, currency, timestamp, signature, callbackUrl required; 15-minute TTL).
2. `GET /pgw/api/v1/direct/order/{orderId}` — confirm final status server-side.

A webhook to `callbackUrl` also fires, but the docs do not state outright that skipping it and relying solely on the pull-based GET (or vice versa) is disallowed — flagged as an open ambiguity.

Docs: [geidea-checkout-v2](https://docs.geidea.net/docs/geidea-checkout-v2), [sample-callback-responses](https://docs.geidea.net/docs/sample-callback-responses)

**Direct API — 4 calls, fixed regardless of 3DS outcome**

1. `POST /payment-intent/api/v2/direct/session` — create session.
2. `POST /pgw/api/v6/direct/authenticate/initiate` — 3DS method / device fingerprint (raw card data enters here).
3. `POST /pgw/api/v6/direct/authenticate/payer` — 3DS challenge (OTP/ACS).
4. `POST /pgw/api/v2/direct/pay` — authorize + capture (Sale).

Whether a frictionless (no-challenge) path can skip step 3 is **not documented** — treat 4 as the confirmed minimum until sandbox-tested otherwise. `scripts/geidea.py` detects a frictionless marker in the initiate response and records a 3-call run if it finds one, so a live run answers this rather than assuming it.

Docs: [pg-direct-api](https://docs.geidea.net/docs/pg-direct-api), [initiate-authentication-v-2](https://docs.geidea.net/docs/initiate-authentication-v-2), [authenticate-payer-v2](https://docs.geidea.net/docs/authenticate-payer-v2), [pay-v2](https://docs.geidea.net/docs/pay-v2)

**Stand-alone**

| Op | Endpoint | Method | Note |
|---|---|---|---|
| Capture | `/pgw/api/v1/direct/capture` | POST | `orderId`, optional `captureAmount`. Synchronous-style response. |
| Refund | `/pgw/api/v2/direct/refund` | POST | `orderId`, `refundAmount`, `signature`. Supports repeated partials. |
| Void | `/pgw/api/v3/direct/void` | POST | `orderId`. Authorized-not-captured only. |
| MIT (2 calls) | `/payment-intent/api/v2/direct/session` then `/pgw/api/v2/direct/pay/token` | POST, POST | Fresh session object per charge. |
| Status query | `/pgw/api/v1/direct/order/{orderId}` | GET | |

MIT requires storing `tokenId` (minted only if the original session had `cardOnFile: true`) plus a merchant-generated `agreementId` and a consent record whose schema is not field-level specified in the docs.

Docs: [reference/capture-transaction-1](https://docs.geidea.net/reference/capture-transaction-1), [refund-2](https://docs.geidea.net/docs/refund-2), [reference/void-payment-1](https://docs.geidea.net/reference/void-payment-1), [merchant-initiated-mit](https://docs.geidea.net/docs/merchant-initiated-mit), [fetch-1](https://docs.geidea.net/docs/fetch-1)

**Documentation gaps:** full `paymentOperation` enum on the Pay endpoint not found; frictionless 3DS shortcut not confirmed either way; MIT consent-record schema not specified; webhook mandatoriness ambiguous.

---

### Adyen

**Docs verified:** `docs.adyen.com`, Checkout API v71/v72. Test base: `https://checkout-test.adyen.com/v71/...`. Two parallel integration paths exist — Sessions flow (≈ Geidea's Hosted Checkout) and Advanced flow (≈ Geidea's Direct API).

**Hosted Checkout (Sessions flow) — 1 outbound + 1 webhook**

1. `POST /sessions` — Adyen's own docs state this is deliberately "a single Checkout API request," positioned as the flow's advantage over Advanced flow's 3 requests.
2. Confirmation is webhook-only (`AUTHORISATION` event) per the pages reached — **no pull-based polling endpoint was found** in the Sessions flow guide. Flagged as not fully verifiable; Adyen may expose a lookup API elsewhere.

Docs: [Sessions flow guide](https://docs.adyen.com/online-payments/build-your-integration/sessions-flow), [Create a payment session](https://docs.adyen.com/api-explorer/Checkout/latest/post/sessions), [Webhooks](https://docs.adyen.com/development-resources/webhooks)

**Direct API (Advanced flow) — 2 calls frictionless, 3 with a challenge**

1. `POST /paymentMethods` — populate the payment method list for the transaction context.
2. `POST /payments` — make the payment; card data is client-side tokenized in the standard integration, so a raw PAN does not reach the merchant server (unlike Geidea).
3. *(conditional)* `POST /payments/details` — only if the response returned an `action` object (3DS2 challenge, redirect, etc.).

Adyen's docs **explicitly confirm** a frictionless one-call resolution within `/payments` ("Only one API call is needed") — the clearest documented contrast with Geidea's unconditional 4-call sequence.

Docs: [Advanced flow guide](https://docs.adyen.com/online-payments/build-your-integration/advanced-flow), [Native 3DS2](https://docs.adyen.com/online-payments/3d-secure/native-3ds2), [/payments API Explorer](https://docs.adyen.com/api-explorer/Checkout/latest/post/payments)

**Stand-alone**

| Op | Endpoint | Method | Note |
|---|---|---|---|
| Capture | `/payments/{pspReference}/captures` | POST | Returns `received` — final outcome by webhook |
| Refund | `/payments/{pspReference}/refunds` | POST | Must be captured first |
| Void | `/payments/{pspReference}/cancels` or legacy `/cancels` (`paymentReference`) | POST | Legacy variant valid ≤24h post-auth; impossible after capture |
| MIT (1 call) | `/payments` with `storedPaymentMethodId` + `shopperInteraction: ContAuth` + `recurringProcessingModel` | POST | No separate session step |

The token itself is delivered asynchronously via the `recurring.token.created` webhook, not in the synchronous store-card response.

Docs: [Capture](https://docs.adyen.com/online-payments/capture), [Refund](https://docs.adyen.com/online-payments/refund), [Cancel](https://docs.adyen.com/online-payments/cancel), [Create tokens](https://docs.adyen.com/online-payments/tokenization/create-tokens), [Make token payments](https://docs.adyen.com/online-payments/tokenization/make-token-payments)

**Documentation gaps:** no polling alternative to the webhook found for the Sessions flow; the raw-server-side-card ("API only") path's call count not separately verified; `sessionResult` verification mechanics on the merchant server not confirmed against an official page.

---

### Stripe

**Docs verified:** `docs.stripe.com` (`/api/...` reference plus narrative guides), fetched live 2026-08-18. Base: `https://api.stripe.com/v1/...`.

Full detail in [`03_stripe_flow_mapping.md`](03_stripe_flow_mapping.md).

**Hosted Checkout (Checkout Sessions) — 1 outbound + webhook (2 outbound in Stripe's own reference implementation)**

1. `POST /v1/checkout/sessions` — create session.
2. Stripe's fulfillment guide is unusually explicit: **"Webhooks are required for fulfillment... you can't rely on triggering fulfillment only from your checkout landing page."** The reference implementation also calls `GET /v1/checkout/sessions/{id}` from inside the webhook handler as a defensive re-check — 2 outbound calls plus 1 inbound webhook in practice.
3. A special option: if a webhook is registered, Checkout holds the browser redirect for up to 10 seconds waiting for the webhook handler to respond.

**Direct API (PaymentIntents) — 1–2 calls frictionless, +0/+1 for 3DS depending on `confirmation_method`**

1. `POST /v1/payment_intents` with `confirm=true` and an existing `payment_method` ID — **can complete in 1 call**.
2. *(conditional)* `POST /v1/payment_methods` first, if raw card data needs to become a PaymentMethod (+1 call).
3. *(conditional, 3DS challenge)* With `confirmation_method: automatic` (default), the client SDK handles the challenge with **no additional server call**. With `manual`, the server must confirm again after the challenge (+1 call).

**Caveat Stripe states about itself:** their own quickstart advises against using the PaymentIntent API unless explicitly needed, because it requires significantly more code — Stripe actively steers integrators toward Checkout Sessions/Elements. The 1-call minimum is real but is not Stripe's recommended default path, and the benchmark should not be read as "Stripe direct API always faster" without that caveat.

**Stand-alone**

| Op | Endpoint | Method |
|---|---|---|
| Capture | `/v1/payment_intents/{id}/capture` | POST |
| Refund | `/v1/refunds` | POST |
| Void | `/v1/payment_intents/{id}/cancel` | POST |
| Status query | `/v1/payment_intents/{id}` | GET |
| MIT (1 call) | `/v1/payment_intents` (`customer` + `payment_method` + `off_session=true` + `confirm=true`) | POST |

**Notable MIT failure mode:** off-session SCA-exemption requests can fail outright (PaymentIntent → `requires_payment_method`, **not** `requires_action`), requiring a full on-session re-confirmation with the customer present to recover — a more disruptive failure path than a simple retry, and not mirrored in Geidea's MIT docs.

**Documentation gaps:** exact SCA-exemption rule set not enumerated; webhook retry schedule referenced but not sourced from a dedicated page; the "up to 10 seconds" webhook-wait window not cross-verified.

---

### Checkout.com

**Docs verified:** `www.checkout.com/docs/...` and `api-reference.checkout.com`. Base: `https://{prefix}.api.checkout.com` (prod) / `https://{prefix}.api.sandbox.checkout.com` (sandbox).

**Hosted Checkout (Hosted Payments Page) — 2 calls**

1. `POST /hosted-payments` — create session.
2. `GET /hosted-payments/{id}` (or `GET /payments/{id}`, or webhook) — the docs explicitly warn against trusting the front-end redirect alone as proof of payment, which makes this second signal mandatory in substance.

Docs: [Accept a payment on a hosted page](https://www.checkout.com/docs/payments/accept-payments/accept-a-payment-on-a-hosted-page), [API Reference](https://api-reference.checkout.com/)

**Direct API (Payments API) — 1 call frictionless, 2 with a challenge**

Full card PAN/CVV submission is gated behind **SAQ D PCI compliance**; the standard path requires a pre-tokenized `source` (`token`, `network_token`, or stored `id`). Client-side tokenization therefore happens in the browser before the merchant-server call — an implicit step not counted in the server-side total.

1. `POST /payments` with a tokenized `source` — a `201` response is a final frictionless result. A `202` means 3DS is required.
2. *(conditional)* `GET /payments/{id}` or webhook, to learn the outcome after a `202`/Pending 3DS redirect.

Docs: [Accept a payment using the Payments API](https://www.checkout.com/docs/payments/accept-payments/accept-a-payment-using-the-payments-api), [Get payment details](https://www.checkout.com/docs/payments/manage-payments/get-payment-details)

**Stand-alone**

| Op | Endpoint | Method | Note |
|---|---|---|---|
| Capture | `/payments/{id}/captures` | POST | Async (202); auto-voids if not captured within 7 days |
| Refund | `/payments/{id}/refunds` | POST | Async (202) |
| Void | `/payments/{id}/voids` | POST | Async (202) |
| MIT (1 call) | `/payments` (`source.type: id`, `merchant_initiated: true`, `payment_type: Unscheduled`, `previous_payment_id`) | POST | Explicitly SCA-exempt, no 3DS |

**Important semantic note:** Capture/Refund/Void all return `202` (accepted, not completed) — a "successful" response is an acknowledgment, not confirmation. True completion requires a webhook (`payment_captured` / `payment_refunded` / `payment_voided`). This matters if the benchmark measures time-to-authoritative-status rather than time-to-202.

**Documentation gaps:** required-field list for `/hosted-payments` inconsistent between pages; a possible separate "fast refund" endpoint referenced but not verified; whether `reference` is truly required on captures not cross-checked against the schema.

---

### HyperPay

**Docs verified:** `hyperpay.docs.oppwa.com`. HyperPay is a white-label deployment of the **OPPWA** payment engine — the same platform Peach Payments runs on. Sandbox host: `eu-test.oppwa.com` (a HyperPay-branded equivalent is issued at onboarding). **No self-serve sandbox signup.**

**Hosted Checkout (Copy&Pay) — 2 calls, explicitly mandatory**

1. `POST /v1/checkouts` — prepare checkout, get `checkoutId`.
2. *(browser: widget loads via `paymentWidgets.js?checkoutId=...` — not a merchant-server call)*
3. `GET /v1/checkouts/{checkoutId}/payment` (via a `resourcePath` appended to the redirect URL) — mandatory status confirmation. Throttled to **2 calls/minute per checkout**, and the checkout ID becomes single-use after a successful response. This reads as more explicitly mandatory than Geidea's webhook-or-poll ambiguity.

Docs: [integrations/widget](https://hyperpay.docs.oppwa.com/integrations/widget)

**Direct API (Server-to-Server) — 1 call frictionless, 2 with a challenge (flagged as needing sandbox confirmation)**

1. `POST /v1/payments` (`paymentType: DB`) with card data and, apparently, 3DS/device data inline — no separate "Initiate Authentication" call analogous to Geidea's step 2 was found.
2. *(conditional)* A follow-up `GET {resourcePath}` after a challenge redirect.

The dedicated 3DS reference page **404'd** during research, so this claim rests on an overview page only. PCI-DSS compliance is explicitly required for raw card collection, the same as Geidea. No mandatory tokenization call before a first charge.

Docs: [integrations/server-to-server](https://hyperpay.docs.oppwa.com/integrations/server-to-server), [reference/parameters](https://hyperpay.docs.oppwa.com/reference/parameters)

**Stand-alone**

| Op | `paymentType` | Verified? |
|---|---|---|
| Capture | `CP` | Mechanism confirmed on HyperPay's own reference table; **exact URL shape not confirmed** on a HyperPay page — two dedicated tutorial pages 404'd, cross-referenced against [Peach Payments](https://support.peachpayments.com/support/solutions/articles/47001240819-pre-authorization-pa-and-capture-cp-scenario) (same engine) as `POST /v1/payments/{id}/payments` |
| Refund | `RF` | Same caveat; Peach's newer API nests `id` in the body while classic OPPWA nests it in the path — possibly different API versions |
| Void/Reversal | `RV` | Same caveat; cutoff-time limited |
| Tokenization | `POST /v1/registrations`, `POST /v1/registrations/{registrationId}/payments` | Both confirmed on HyperPay's own reference page |
| MIT (1 call) | `POST /v1/registrations/{registrationId}/payments` | **Confirmed** on HyperPay's own Card-on-File tutorial page |

MIT uses `standingInstruction.mode/source/type` fields (CIT/MIT, INITIAL/REPEATED) — more granular and better documented than Geidea's prose-only consent description. A single `registrationId` covers both the "token" and "agreement" roles that Geidea splits into two separate fields.

Docs: [tutorials/card-on-file](https://hyperpay.docs.oppwa.com/tutorials/card-on-file)

**Documentation gaps (largest of the six):** exact refund/reversal URL shape unverified (404s); dedicated 3DS reference page unreachable, so the "1–2 call" Direct API claim rests on an overview page — **recommend a live sandbox test before publishing this specific claim**; webhook mandatoriness for Copy&Pay not confirmed either way; whether `card.holder` is conditionally required by scheme not confirmed.

---

### Moyasar

**Docs verified:** `docs.moyasar.com` (`/api/...` reference, `/guides/...` integration guides). Base: `https://api.moyasar.com`. Auth: HTTP Basic (secret key as username, empty password).

**Hosted Checkout (Invoices) — 2 calls, identical shape to Geidea**

1. `POST /v1/invoices` — create invoice, get hosted `url`.
2. `GET /v1/invoices/{id}` — confirm final status. (`callback_url` is optional — the same ambiguity as Geidea's `callbackUrl`.)

Docs: [Create Invoice](https://docs.moyasar.com/api/invoices/01-create-invoice/), [Fetch Invoice](https://docs.moyasar.com/api/invoices/04-show-invoice/)

**Direct API — the documentation is split between two different minimums**

- **Per the API reference schema: 2 calls** — `POST /v1/payments` (raw card, `source.type: creditcard`) → `GET /v1/payments/{id}` to confirm after a 3DS redirect.
- **Per Moyasar's own recommended "Custom UI" guide: 3 calls** — `POST /v1/tokens` (tokenize first) → `POST /v1/payments` (charge with token) → `GET /v1/payments/{id}`.

3DS is not a separate call — it is a `transaction_url` field on the Create Payment response, confirmed via the same Fetch call the flow needs regardless of 3DS outcome. `scripts/moyasar.py` implements both via `MOYASAR_DIRECT_MODE`, so a live run settles which one the sandbox actually supports.

Docs: [Create Payment](https://docs.moyasar.com/api/payments/01-create-payment/), [Create Token](https://docs.moyasar.com/api/other/tokens/create-token/), [Custom UI guide](https://docs.moyasar.com/guides/card-payments/custom-ui/), [Fetch Payment](https://docs.moyasar.com/api/payments/02-fetch-payment)

**Stand-alone**

| Op | Endpoint | Method | Note |
|---|---|---|---|
| Capture | `/v1/payments/{id}/capture` | POST | Must capture within 14 days on Mada |
| Refund | `/v1/payments/{id}/refund` | POST | |
| Void | `/v1/payments/{id}/void` | POST | ~2-hour window post-capture; docs recommend void over refund inside it (no processing fee) |
| MIT (2 calls) | `POST /v1/tokens` (or `save_card: true` on the first charge), then `POST /v1/payments` with `source.type: token` | POST | No session concept — same call shape as a normal payment, just with a token source. 3DS explicitly not triggered on token payments. |

**Documentation gaps:** raw-card vs. tokenize-first as "the" recommended production path is unresolved between two doc pages; `callback_url` mandatoriness ambiguous (same pattern as Geidea); no documented customer-ID/consent-record concept for MIT beyond the token itself — which could be a genuinely simpler model, or just undocumented.

---

## 3. Data gaps requiring live sandbox verification

The full 22-item list is maintained as data in `scripts/reference_data.py` (`DATA_GAPS`) and rendered into the **Data Gaps** tab of `results/gateway_benchmark_workbook.xlsx`, so it stays in one place rather than drifting between documents.

The six that most affect the published comparison:

1. **Geidea:** whether a frictionless (no 3DS challenge) Direct API path can skip the Authenticate Payer call, dropping below the documented 4-call fixed sequence. *This is the benchmark's headline question.*
2. **HyperPay:** the "1 call frictionless / 2 with challenge" claim for the Direct API — the dedicated 3DS reference page returned a 404, so this rests on an overview page only.
3. **HyperPay:** exact URL shape for Capture/Refund/Void — both dedicated tutorial pages 404'd; the scripts use the most consistent inferred shape but it is unverified against a HyperPay-branded page.
4. **Adyen:** whether a pull-based (polling) alternative to the webhook exists for the Sessions flow.
5. **Moyasar:** whether raw-card submission (`source.type: creditcard`) is a fully supported production path, or whether the real-world PCI stance effectively requires tokenize-first.
6. **Checkout.com:** whether a distinct "fast refund without reference" endpoint exists as a sibling to the documented refund-with-reference path.

None of these block starting the timing benchmark. The documented minimum sequences in each gateway module are usable as-is for the flows that are confirmed, and these should be resolved opportunistically as sandbox access arrives.

---

## 4. Key structural differences

**Geidea has the most rigid Direct API sequence of all six** — a fixed 4-call chain with no documented frictionless shortcut. Every other gateway documents or plausibly supports a lower-call frictionless path (1–2 calls) when no 3DS challenge is presented. This is the largest and best-evidenced structural gap in the set, and the biggest lever for narrowing raw Direct API latency versus competitors on the *most common* case.

**MIT/recurring charges:** Geidea requires re-establishing a session object on every single charge. Adyen, Stripe, Checkout.com and HyperPay all collapse recurring charges into one call against a stored token/registration. Moyasar's documented "2 calls" is a one-time token mint plus a per-charge call, so its actual per-charge cost is 1 — which the scripts measure explicitly rather than inheriting the doc's number.

**Hosted Checkout confirmation splits into two camps:**

- *Webhook-only, no documented pull alternative:* Adyen (Sessions flow), Stripe (mandatory `checkout.session.completed`, though an optional non-authoritative GET exists).
- *Pull-based GET always available and treated as the primary/mandatory server-side verification:* Geidea, Checkout.com, HyperPay, Moyasar.

This matters for benchmarking: a pull-based confirmation is directly timeable by the merchant, whereas a webhook-only design means "time to authoritative confirmation" is bounded by the gateway's webhook delivery latency — not a request the merchant controls or can retry on demand.

**Async ack (`202`) vs. synchronous-style response on Capture/Refund/Void:** Checkout.com explicitly returns `202` on all three, requiring a webhook for true completion. Adyen returns `received` with the same implication. Geidea, Stripe and Moyasar's docs read as synchronous-style. If the benchmark reports "time to authoritative completion" rather than "time to HTTP response," Checkout.com's and Adyen's numbers need the webhook leg added, or the comparison undercounts their real latency.

**Card-data handling / PCI scope varies the comparability of "Direct API" call counts:**

- Geidea and HyperPay accept a raw PAN directly server-side (with a PCI-DSS callout) for a first-time charge — no separate tokenization call required.
- Checkout.com requires client-side tokenization (Frames/Flow SDK) before the merchant server touches the payment, unless the merchant holds SAQ D. Its low call count understates real end-to-end latency unless the browser tokenization round trip is added back in.
- Adyen's documented Advanced flow similarly assumes client-side encrypted card data.
- Stripe blocks raw PANs on `/v1/payment_methods` for accounts without PCI enablement, and actively steers integrators away from the path entirely.
- Moyasar's docs are internally inconsistent on this exact point.

**Implication:** a strict apples-to-apples "raw API latency" comparison is only clean between **Geidea and HyperPay**. Comparisons against the others need to either (a) explicitly scope to "merchant-server calls only, tokenization treated as a black-box precondition" — which is what these scripts do, issuing tokenization as an untimed prep call — or (b) add the client-side tokenization round trip into the total, which will narrow or reverse Geidea's apparent disadvantage. Whichever you choose, say which one in the write-up.

**Documentation completeness itself varies and affects confidence.** HyperPay had the most 404s and gaps, making its 1–2 call Direct API claim the least reliable of the six. Geidea and Moyasar's docs are comparably thorough but each leaves the "is the webhook mandatory" question explicitly unanswered. HyperPay's MIT documentation (`standingInstruction` semantics) was, by contrast, the most granular and field-complete of any gateway studied.

---

## 5. General caveat

**None of these call counts have been sandbox-timed.** This document maps *call-count minimums per vendor documentation*, not measured latency. Call count alone is not a reliable proxy for response time — Checkout.com and HyperPay return async `202`s that defer real completion to a webhook, which a naive "time to first response" measurement would miss entirely. The next step is executing each documented minimum sequence against test/sandbox endpoints and measuring wall-clock time per call and end to end, which is what `/scripts` does.

## 6. Sources

Every endpoint, field and call-count claim above was fetched from the gateway's own live documentation on 2026-08-18, cross-referenced against a second source only where explicitly noted (HyperPay capture/refund/void, verified via Peach Payments' identical OPPWA engine). The complete URL list is maintained as data in `scripts/reference_data.py` (`SOURCES`) and rendered into the workbook's **Sources** tab.
