# Stripe API — Flow Mapping Report

**Docs verified against:** `docs.stripe.com` — API reference under `/api/...`, narrative guides elsewhere. All endpoints and fields below were fetched directly from Stripe's live documentation on 2026-08-18, not inferred from general knowledge. Source URLs are cited per section.

This is the deep-dive companion to the Stripe section of [`02_api_flow_comparison.md`](02_api_flow_comparison.md). Stripe gets its own document because it is the gateway whose documented minimum diverges most sharply from Geidea's, and because the divergence comes with caveats that a one-row table cannot carry.

---

## 1. Hosted Checkout (Checkout Session, `ui_mode: hosted_page`)

**Docs:** [checkout/quickstart](https://docs.stripe.com/checkout/quickstart), [api/checkout/sessions/create](https://docs.stripe.com/api/checkout/sessions/create), [checkout/fulfillment](https://docs.stripe.com/checkout/fulfillment)

### Create session

`POST https://api.stripe.com/v1/checkout/sessions` — HTTP Basic auth with the secret key.

- **Required:** `line_items` (conditionally — needed for `payment`/`subscription` mode), `mode` (`payment` | `setup` | `subscription`), `success_url` (conditionally — not allowed if `ui_mode` is `embedded_page`/`elements`).
- **Common optional:** `cancel_url`, `customer_email`, `customer`, `client_reference_id`, `automatic_tax`, `payment_method_types`, `billing_address_collection`, `expires_at` (30 min–24 hr, defaults to 24 hr).
- **Response:** `session.id` (`cs_...`), `session.url` (the hosted page to redirect to), `session.status` (`open`/`complete`/`expired`), `session.payment_status` (`unpaid`/`paid`/`no_payment_required`).

The merchant server redirects the customer's browser (HTTP 303) to `session.url`; Stripe's hosted page collects card details directly, so card data never touches merchant servers.

### Confirming payment status after redirect

Stripe's docs are explicit here, and more strongly worded than Geidea's. The fulfillment guide states plainly that webhooks are required for fulfillment, and that a merchant cannot rely on triggering fulfillment from the checkout landing page, since a customer visiting that page is not guaranteed.

Three mechanisms, per Stripe's own fulfillment guide:

1. **Webhook (mandatory per Stripe's docs).** Stripe POSTs `checkout.session.completed` (plus `checkout.session.async_payment_succeeded` / `_failed` for delayed methods like ACH) to a registered endpoint. Stripe explicitly retries failed deliveries multiple times.
2. **Landing-page fulfillment trigger (recommended, not sufficient alone).** On `success_url` redirect (with the `{CHECKOUT_SESSION_ID}` placeholder), the handler calls `GET /v1/checkout/sessions/{id}` with `line_items` expanded and checks `payment_status`. Stripe explicitly says this is *not* reliable alone.
3. **Special synchronous option.** If a webhook endpoint listening for `checkout.session.completed` is registered *and* `success_url` is set, Checkout holds the redirect for **up to 10 seconds** waiting for the merchant's webhook handler to respond — effectively letting webhook processing finish before the browser redirect fires. Not available for org-account webhook endpoints.

### Minimum merchant-server call count: 1 outbound + webhook receipt

1. `POST /v1/checkout/sessions` (outbound).
2. Confirmation is push-based: Stripe → merchant `checkout.session.completed` webhook (inbound), which the merchant's `fulfill_checkout` function processes by calling `GET /v1/checkout/sessions/{id}` to re-verify — a defensive re-check Stripe's own sample code performs even inside the webhook handler.

So the documented reference implementation is **2 outbound calls plus 1 inbound webhook**, mirroring Geidea's shape almost exactly. `scripts/stripe_gw.py` measures both outbound calls, since both are things a merchant server actually issues.

**Mandatory vs optional:** Stripe's callout box titled "Webhooks are required for fulfillment" is notably stronger than Geidea's softer, implicit language — a genuine, citable phrasing difference, not just semantics. The landing-page GET is "recommended" for immediate UX but explicitly documented as insufficient alone.

---

## 2. Direct API / Server-to-Server (PaymentIntents)

**Docs:** [api/payment_intents/create](https://docs.stripe.com/api/payment_intents/create), [api/payment_intents/confirm](https://docs.stripe.com/api/payment_intents/confirm), [api/payment_methods/create](https://docs.stripe.com/api/payment_methods/create), [payments/3d-secure/authentication-flow](https://docs.stripe.com/payments/3d-secure/authentication-flow)

**Framing note from Stripe's own docs:** the quickstart explicitly discourages this pattern, advising against the PaymentIntent API unless the integrator specifically needs it, since it requires significantly more code. Stripe steers integrators toward Checkout Sessions or Elements instead of raw server-to-server PaymentIntents with raw card data. This is a meaningful product-positioning difference from Geidea, which documents server-to-server Direct API as a first-class, explicitly required pattern ("Do not call APIs from your front-end application").

**Raw card data / PCI note:** `POST /v1/payment_methods` states directly that providing a card number requires meeting PCI compliance requirements, and recommends Stripe.js instead. Same PCI-scope implication as Geidea's Direct API, but Stripe frames it as actively discouraged rather than as the standard documented path. This is why `scripts/stripe_gw.py` charges a test token rather than a raw PAN — a raw-PAN variant would fail on most sandbox accounts and would not be comparable anyway.

### Call sequence (minimum path, automatic confirmation, no 3DS challenge)

1. **Create + Confirm PaymentIntent** — `POST /v1/payment_intents`.
   Required: `amount`, `currency`. To confirm in the same call: `confirm=true` plus `payment_method` (an existing PaymentMethod ID). Alternative: create only, then `POST /v1/payment_intents/{id}/confirm` separately.
   Response includes `status` (`requires_payment_method` / `requires_confirmation` / `requires_action` / `processing` / `requires_capture` / `succeeded`) and `next_action` (null unless further action is needed).

2. **(Conditional) Create PaymentMethod first** — `POST /v1/payment_methods`, if raw card data is being collected server-side rather than using a pre-existing PaymentMethod/token.
   Required: `type` (e.g. `card`) plus the `card` hash (`number`, `exp_month`, `exp_year`, `cvc`). A genuine prerequisite if you do not already hold a `payment_method` ID — bringing the no-3DS minimum to **2 calls**.

3. **(Conditional) 3DS challenge round trip** — if confirm returns `status: requires_action` with `next_action.type: redirect_to_url`, the customer authenticates out of band. After that:
   - With `confirmation_method: automatic` (default) and the client SDK handling the redirect via `client_secret`, **no additional server confirm call is required** — Stripe.js updates the PaymentIntent once the action resolves.
   - With `confirmation_method: manual`, the PaymentIntent returns to `requires_confirmation` and **the server must call confirm again**.

### Minimum call count: 1–2 (no 3DS), +0 or +1 (3DS, depending on `confirmation_method`)

- Best case — existing PaymentMethod ID, frictionless, automatic confirmation: **1 call**.
- With raw card data needing a PaymentMethod first: **2 calls**.
- With a 3DS challenge and manual confirmation: **2–3 calls**.

This is structurally very different from Geidea's fixed 4-call sequence. Stripe's PaymentIntents API can complete a card charge in a single call when a reusable PaymentMethod exists and no authentication is triggered, whereas Geidea specifies Session → Initiate Authentication → Authenticate Payer → Pay unconditionally, whether or not 3DS is actually invoked.

**Caveat worth repeating:** Stripe discourages this integration path. The 1-call best case is a real documented capability but not Stripe's recommended default, so the benchmark should not be read as "Stripe's direct API is always faster" without that context.

---

## 3. Stand-alone APIs

**Docs:** [api/payment_intents/capture](https://docs.stripe.com/api/payment_intents/capture), [api/payment_intents/cancel](https://docs.stripe.com/api/payment_intents/cancel), [api/refunds/create](https://docs.stripe.com/api/refunds/create), [payments/save-during-payment](https://docs.stripe.com/payments/save-during-payment)

| Operation | Endpoint | Method | Key required fields |
|---|---|---|---|
| **Capture** | `/v1/payment_intents/{id}/capture` | POST | Path `id` only. Optional `amount_to_capture` (≤ `amount_capturable`), `final_capture`. Valid only from status `requires_capture` (i.e. `capture_method: manual` was set at creation). |
| **Refund** | `/v1/refunds` | POST | One of `charge` or `payment_intent`. Optional `amount` (partial, repeatable until fully refunded), `reason`. |
| **Void/Cancel** | `/v1/payment_intents/{id}/cancel` | POST | Path `id` only. Optional `cancellation_reason`. Valid from `requires_payment_method`, `requires_capture`, `requires_confirmation`, `requires_action`, rarely `processing`. From `requires_capture`, any `amount_capturable` is auto-refunded. |
| **Status query** | `/v1/payment_intents/{id}` | GET | Path `id` only |
| **MIT — save payment method** | side effect of a regular payment: `payment_method_options[card][setup_future_usage]=off_session` (or top-level `setup_future_usage`) on the initial create | POST | `customer` must be attached for the resulting PaymentMethod to be reusable |
| **MIT — off-session charge** | `/v1/payment_intents` | POST | `amount`, `currency`, `customer`, `payment_method`, `off_session=true`, `confirm=true`, `return_url` |

### MIT / recurring — minimum 1 call for the charge itself

1. *(One-time, from the initial transaction)* `POST /v1/payment_intents` with `setup_future_usage` set and `customer` attached — persists a reusable `payment_method` on the customer.
2. *(Each recurring charge)* `POST /v1/payment_intents` with `customer`, `payment_method`, `off_session=true`, `confirm=true` — a single call, since create and confirm combine.

Optionally, before step 2, `GET /v1/payment_methods?customer={id}&type=card` to look up the stored PaymentMethod ID if not cached — a lookup convenience, not a documented hard requirement.

**Data to store from the initial transaction:** the `customer` ID and the resulting `payment_method` ID. Both are needed for the later off-session create+confirm.

### 3DS/SCA on off-session MIT charges — an explicitly documented failure mode

- `off_session=true` tells Stripe the customer cannot respond to an authentication challenge; Stripe attempts **SCA exemptions** using data from the original on-session transaction.
- If exemption conditions are not met, the off-session confirm **errors out**: the PaymentIntent transitions to `requires_payment_method`, **not** `requires_action` — the SDK explicitly cannot drive `requires_action` handling from an off-session failure state.
- To recover, the merchant must re-attempt with `off_session` unset/`false` to force the PaymentIntent into `requires_action`, then complete a genuine on-session 3DS challenge with the customer present — i.e. contact the customer and bring them back.

This is a materially more disruptive failure-recovery path than a simple retry, and it is worth flagging distinctly in the write-up: Geidea's MIT docs describe no equivalent.

---

## 4. Cross-flow notes for the benchmark

**Hosted Checkout** is structurally near-identical to Geidea's — one create call, webhook-driven confirmation, redirect landing page as a secondary non-authoritative signal. Stripe's mandatory-webhook language is unambiguous where Geidea's is soft. That is a citable phrasing difference, not just a matter of interpretation.

**Direct/Server-to-Server** is structurally very different. Geidea mandates a fixed 4-call sequence regardless of whether a challenge fires; Stripe can complete in as few as 1 call, scaling up conditionally. But Stripe steers integrators away from this path, so the low count should not be presented as "Stripe direct API always faster" without that caveat.

**Capture/Refund/Void** are all single-call operations with the same shape as Geidea's, keyed off `payment_intent`/`charge` IDs rather than Geidea's `orderId`.

**MIT** is a minimum of 1 call for the recurring charge if the PaymentMethod ID is stored — fewer than Geidea's 2, because Stripe's architecture needs no per-charge session object. The SCA-exemption failure path is a documented complexity with no Geidea equivalent.

---

## 5. Documentation gaps

1. The exact conditions under which Stripe's SCA-exemption request for off-session MIT charges succeeds versus fails are not enumerated with a precise rule set in the pages fetched — only that Stripe requests exemptions using information from a previous on-session transaction, and that failure is possible.
2. The precise webhook delivery retry schedule and backoff ("Stripe retries multiple times") is referenced but was not itself fetched from a dedicated reference page.
3. Whether the 10-second webhook-wait-before-redirect behaviour is configurable or fixed at exactly 10s in all cases (versus an approximate/best-effort window) is stated as "waits up to 10 seconds" — taken as authoritative from the fulfillment doc, not cross-verified against a second source.

## 6. Sources

- https://docs.stripe.com/checkout/quickstart
- https://docs.stripe.com/api/checkout/sessions/create
- https://docs.stripe.com/checkout/fulfillment
- https://docs.stripe.com/api/payment_intents/create
- https://docs.stripe.com/api/payment_intents/confirm
- https://docs.stripe.com/api/payment_intents/capture
- https://docs.stripe.com/api/payment_intents/cancel
- https://docs.stripe.com/api/refunds/create
- https://docs.stripe.com/api/payment_methods/create
- https://docs.stripe.com/payments/3d-secure/authentication-flow
- https://docs.stripe.com/payments/save-during-payment
- https://support.stripe.com/questions/manual-confirmation-for-off-session-payments-requiring-strong-customer-authentication-(sca)
