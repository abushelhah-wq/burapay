# BuraPay — Master Prompt

The canonical specification. Until now this lived only in chat sessions, which is
how it drifted out of step with the code: a session working from a stale copy
"discovered" contradictions that were really just an out-of-date brief. Keep it
here, and change it here.

**Status of a requirement is not the same as the requirement.** The spec is
§0–§13. Appendix A records what is currently implemented on `master`, and is
maintained separately — never edit §0–§13 to match the code.

---

## 0. NON-NEGOTIABLE RULES

1. Before implementing ANY Geidea endpoint, fetch and read the live doc page at
   `https://docs.geidea.net/docs/<page>.md` (the `.md` suffix returns clean
   markdown — use it, don't scrape the HTML). Do not implement a request/response
   shape from memory or assumption. If a field's meaning is ambiguous, fetch the
   API Reference page for that exact endpoint, not just the guide page.
2. Never invent a status code, error format, or field name. If it isn't in the
   docs or in an actual response you logged, mark it TODO with a comment citing
   what's missing; don't guess and ship it.
3. Every outbound HTTP call to a payment gateway MUST be wrapped by a single
   shared client that: signs/authenticates the request per that gateway's spec,
   records a request log row BEFORE sending, records a response log row AFTER
   receiving (or after timeout/error), and returns a normalized internal result
   object. No endpoint handler is allowed to call `httpx`/`requests` directly.
4. All money amounts are integers in minor units (fils/halalas/cents) end to end.
   Never use float for money. Convert to display decimal only at presentation.
5. All timestamps stored in UTC, ISO-8601, with millisecond precision.
6. Idempotency: every payment-creating request from the frontend carries a
   client-generated idempotency key. The backend must never create two
   transactions for the same key, even under retry/double-click.
7. Secrets (API keys, merchant IDs, webhook secrets) live only in env vars loaded
   server-side, or encrypted at rest per §7b. Never in frontend code, never
   logged in plaintext — mask them in logs (first 4 / last 4 only).
8. Card PANs, CVVs, and full token secrets are NEVER stored or logged in full.
   Log only: card brand, last 4, expiry month/year, and the gateway's returned
   token reference. Redact PAN/CVV before writing any raw body to the log store.

## 1. ARCHITECTURE

- Backend: Python, FastAPI (async), SQLAlchemy 2.x async, Alembic. Pydantic v2
  models for every request/response — no raw dicts crossing a function boundary.
- DB: PostgreSQL (not SQLite — real concurrency and JSONB for raw payloads).
- Frontend: a separate service/container from the API. Do not serve templated
  HTML from FastAPI for the main app. React or Svelte over a documented REST API.
- Reverse proxy: Traefik. Document the exact labels used.
- Background jobs: a proper task queue (RQ or Celery with Redis), or FastAPI
  BackgroundTasks only for genuinely fire-and-forget work. Do not block
  request/response cycles on anything not needed for the immediate answer.
- Gateway adapters live in `app/gateways/<gateway_name>/`, each implementing a
  common `GatewayAdapter` interface (§3). Adding a gateway must never require
  touching shared code — only a new adapter module and a registry entry.

## 2. DATA MODEL (minimum)

- `gateways(id, code, display_name, enabled, config_json)`
- `integration_types(id, code, display_name)` — HPP, DIRECT_API, TOKEN_CIT, TOKEN_MIT
- `transactions(id, gateway_id, integration_type_id, operation, parent_transaction_id,
  status, amount_minor, currency, card_brand, card_last4, card_exp_month,
  card_exp_year, token_reference, gateway_order_id, gateway_transaction_id,
  merchant_reference, idempotency_key, error_code, error_message, started_at,
  completed_at, duration_ms, request_count, created_at)`
  - operation: AUTH, SALE, CAPTURE, PARTIAL_CAPTURE, REFUND, PARTIAL_REFUND,
    VOID/REVERSAL, TOKENIZE, CIT_CHARGE, MIT_CHARGE, ORDER_QUERY
  - status: PENDING, SUCCESS, FAILED, ERROR, TIMEOUT
  - `request_count` is the total HTTP calls the operation took, and is a
    first-class product metric, not an implementation detail.
- `api_call_logs(id, transaction_id (nullable), gateway_id, sequence_number,
  step_name, http_method, url, request_headers_json (masked),
  request_body_json (redacted), response_status_code, response_headers_json,
  response_body_json, started_at, completed_at, duration_ms, error_text)`
- `tokens(id, gateway_id, token_reference, card_brand, card_last4, card_exp_month,
  card_exp_year, created_from_transaction_id, created_at, is_active)`
- `webhooks_received` — superseded by the `WebhookEvent` model in §11.

Every transaction MUST be explainable purely from `api_call_logs` joined on
`transaction_id`, in `sequence_number` order, without re-deriving anything from
gateway dashboards.

## 3. GATEWAY ADAPTER INTERFACE

One abstract interface all gateways implement. Adapters that don't support an
operation raise a typed `UnsupportedOperationError`, and the frontend greys
out/hides that control rather than letting the user hit a dead end.

```python
class GatewayAdapter:
    async def create_hpp_session(self, order) -> HPPSessionResult
    async def direct_api_sale(self, order, card) -> PaymentResult
    async def direct_api_auth(self, order, card) -> PaymentResult
    async def capture(self, transaction, amount_minor=None) -> PaymentResult
    async def refund(self, transaction, amount_minor=None) -> PaymentResult
    async def void(self, transaction) -> PaymentResult
    async def tokenize(self, card) -> TokenResult
    async def charge_with_token_cit(self, token, order) -> PaymentResult
    async def charge_with_token_mit(self, token, order) -> PaymentResult
    async def query_order(self, transaction) -> OrderStatusResult
    async def verify_webhook(self, raw_body, headers, credential_profile) -> bool
```

Each method logs every underlying HTTP request to `api_call_logs`, in order, and
increments `transaction.request_count` accordingly. This number is the whole
point of the benchmark — it compares how many round trips each gateway needs for
the same operation.

## 4. GEIDEA ADAPTER

Build first and fully, as the reference implementation. Read the live doc page
before coding each endpoint. Required capabilities, all wired to the frontend,
all logged:

a. HPP checkout — session create, redirect, return + async callback, reconciled
   from BOTH legs (neither is authoritative alone; verify via Fetch/Order Query
   or a verified callback signature).
b. Direct API sale with a raw test card, full flow, 3DS-capable.
c. Pre-authorisation (auth-only) — Saudi-platform-only per Geidea's docs. If the
   merchant account doesn't support it, detect that from the actual error
   response and surface "not supported for this merchant", not a generic failure.
d. Capture — full and partial, including multiple partial captures against one
   authorisation. Track captured-to-date and remaining-capturable on the parent.
e. Refund — full and partial, against captured/sale transactions only. Blocked
   in the UI and the API against an uncaptured auth.
f. Void/Reversal — authorised-but-not-captured only.
g. Tokenization — standalone save-card, storing the token reference, never the PAN.
h. CIT — customer-present charge using a stored token.
i. MIT — merchant-initiated charge using a stored token, with the correct
   indicator flags verified from the doc, not guessed.
j. Order query / fetch — on demand, and automatically as reconciliation after a
   timeout, so a timeout never leaves a transaction stuck in PENDING.
k. Callback/webhook receiver — see §11, which supersedes this item.

## 5. FRONTEND

- Gateway picker → integration-type picker (only types the selected adapter
  supports) → operation form.
- Test card entry form, pre-filled from a "use test card" helper populated from
  the gateway's test-cards documentation, not hardcoded from memory.
- Transaction detail: status, timing, `request_count`, and the full ordered list
  of `api_call_logs` with method, URL, status code, duration, and expandable
  request/response JSON with redaction preserved.
- Transaction list: filter by gateway, integration type, operation, status, date
  range. Real, paginated, backed by the actual tables.
- Status-gated actions: Refund on captured/settled, Capture and Void on pre-auth,
  nothing on a failed transaction.
- Benchmark dashboard: per gateway × integration type, average/median/p95
  response time, success rate, and average `request_count` per operation type,
  computed live from stored transactions. This is the point of the product; it is
  not an afterthought view built last with fake data.

## 6. RELIABILITY

- Every gateway HTTP call has an explicit timeout, and the timeout value is
  itself logged as a field, so "gateway X times out more than gateway Y" is
  measurable rather than a guess.
- Every adapter call is wrapped in try/except catching network errors, non-2xx
  responses, and JSON parse failures **separately**, and always writes an
  `api_call_logs` row and updates transaction status. A crashed call must never
  leave a transaction silently stuck in PENDING with no log explaining why.
- Integration tests against each sandbox flow end-to-end (sale, auth+capture,
  auth+void, refund, tokenize+CIT, tokenize+MIT) using real sandbox test cards,
  asserting the final status AND the expected `request_count` AND the
  `api_call_logs` sequence. These must pass before any deploy.
- `/health` checks DB connectivity and does NOT call any payment gateway —
  health checks must not spend gateway quota or create sandbox noise.
- Structured JSON application logging for everything that isn't a gateway call
  log, separate from `api_call_logs`, which is transaction data.

## 7. DEPLOYMENT

Target: VPS running Docker + Traefik. The deploy workflow must remain:

```bash
git pull
docker compose build
docker compose up -d
```

- Keep existing compose service names, network name, and Traefik router/label
  conventions unless there is a hard reason to change them. Call out any change
  explicitly; never change one silently.
- All new required env vars go into a documented `.env.example`. Never hardcode
  gateway credentials in compose files or source.
- Alembic migrations run automatically on container start, or via a documented
  one-line `docker compose exec` command. No manual DB surgery on the VPS.
- No new host-level dependencies beyond Docker + Traefik. Redis/Postgres are
  compose services, not host installs.
- Confirm Traefik labels match the existing certificate/domain setup so
  `docker compose up -d` doesn't break TLS or routing.

## 7b. CREDENTIAL STORAGE & ENVIRONMENT

`.env.example` is the contract. Do not rename or remove an existing variable, and
do not remove a safety flag. Read the actual file before writing config code;
treat it as ground truth over this section if they disagree.

- `APP_SECRET_KEY` — signs session tokens.
- `ENCRYPTION_KEY` — Fernet key encrypting gateway credentials at rest. All
  encrypt/decrypt goes through one `CredentialStore`. Never derive a second key
  from it; never add a second variable for the same purpose.
- `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` — used only to create the
  first admin when the users table is empty. No effect after that user exists.
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` — compose builds
  `DATABASE_URL` from these; `DATABASE_URL` is set explicitly only for non-Docker
  local runs.
- `DOMAIN`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERT_RESOLVER`, `TRAEFIK_NETWORK` —
  values come from running `scripts/inspect-traefik.sh` on the actual VPS, never
  hardcoded or assumed. Both the host-network and the dedicated-network Traefik
  arrangements must work.
- `PUBLIC_BASE_URL` — the base for every gateway return-URL/callback-URL. Never
  hardcode a domain anywhere in code, so a staging deploy works unmodified.
- `CORS_ORIGINS` — comma-parsed allow-list for the API's CORS middleware.
- `ALLOW_PRODUCTION_GATEWAYS` — when false (default), refuse to transact against
  any credential flagged live, **at the adapter layer**, not just in the UI.
- `BENCHMARK_MIN_INTERVAL_SECONDS` / `BENCHMARK_MAX_TRANSACTIONS_PER_RUN` —
  enforced by the executor. A run that would exceed the max must STOP, not
  truncate silently, and must surface the stop reason.
- `HTTP_TIMEOUT_SECONDS` — the single default timeout for the shared client.
  Per-gateway overrides only where that gateway's docs require it, commented as
  a deviation.
- `ALLOW_DIRECT_CARD_ENTRY` — server-side gate on raw-card endpoints. When false,
  those endpoints reject the request (403), not merely hide the button.

Credential storage:

- `gateway_credentials`: gateway_id, key_name, encrypted_value (Fernet),
  is_production, updated_at, updated_by_user_id.
- Never store plaintext credentials in any table, log, seed script, or migration.
- Settings page: per-gateway credential entry, masked display (last 4 only),
  update-in-place, and a "test connection" action firing one harmless read-only
  sandbox call.
- Adapters resolve credentials through `CredentialStore` at call time — never
  `os.environ`, never a cached plaintext copy outliving the request.

## 8. DELIVERABLE CHECKLIST

- [ ] Every gateway endpoint matches the field names/shapes in the live docs
      fetched during the build; the doc page used is linked per adapter method.
- [ ] HPP, Direct API sale, pre-auth + capture (full/partial), refund
      (full/partial), void, tokenize, CIT, MIT, order query all work end-to-end
      against the sandbox and are covered by passing integration tests.
- [ ] Every transaction has accurate `request_count` and a complete ordered
      `api_call_logs` trail visible in the UI.
- [ ] Dashboard numbers are computed from real stored data and match a hand count.
- [ ] `git pull && docker compose build && docker compose up -d` deploys with no
      manual steps beyond the documented migration command.
- [ ] No PAN/CVV/full token ever appears in any log table or application log.
- [ ] README updated: architecture, how to add a gateway adapter, how to run
      integration tests, exact deploy command sequence.
- [ ] `.env.example` — no gateway credentials added, no variable renamed or
      removed, all safety flags enforced server-side.
- [ ] `scripts/inspect-traefik.sh` run on the actual VPS; its values used verbatim.
- [ ] Both the standalone and the Traefik-network compose paths work.
- [ ] `PUBLIC_BASE_URL` is the only source of return/callback URLs; grep confirms
      no hardcoded domain in source.
- [ ] `gateway_credentials` stores only ciphertext, verified by direct DB
      inspection after a Settings-page save.
- [ ] `ALLOW_PRODUCTION_GATEWAYS=false` blocks live credentials at the adapter
      layer even when the API is called directly.

---

## 9. AUTHENTICATION AND LOGIN

The application must be fully protected by authentication.

### Login flow

1. User opens `https://busrapay.com`.
2. If not authenticated, redirect to the Login screen.
3. User enters username or email, and password.
4. Backend validates the credentials.
5. On success: create a secure authenticated session; redirect to the Dashboard.
6. On failure: show a **generic** authentication error. Do not reveal whether the
   username or the password was wrong.

### Required

- Secure password hashing using a modern algorithm (Argon2id or bcrypt).
- Secure session management, logout, and session expiration.
- Protected frontend routes AND protected backend APIs.
- Secure cookies where cookie-based sessions are used: `HttpOnly`, `Secure`, and
  a suitable `SameSite`.
- CSRF protection where applicable.
- Login rate limiting and brute-force protection.
- Audit logging of successful and failed login attempts.

Passwords must never be stored in plaintext and must never appear in application
logs. A failed-login response must not vary in timing in a way that reveals
whether the account exists.

## 10. USER MANAGEMENT

Authorized administrators can create and manage BuraPay users. Add a `Users`
navigation section, visible and accessible to administrators only.

### User list

Columns: Name, Username, Email, Role, Status, Created Date, Last Login,
Created By, Actions.

Filter/search by: Name, Username, Email, Role, Status.

### Create user

Form: Full Name, Username, Email, Role, Password, Confirm Password, Status.

Validate: username uniqueness; email uniqueness where applicable; password
complexity; valid email; supported role; active/inactive state. Hash passwords
before persistence.

### Roles

At minimum `ADMIN` and `USER`.

**ADMIN** may: create/edit/disable users; reset user passwords; view gateway
configuration; create/update gateway credentials; run payment tests; refund;
capture; reverse/void; CIT; MIT; view transactions, tokens, logs, benchmark
results and webhook events; export reports.

**USER** may: log in; run payment tests; view transactions; perform permitted
transaction operations; view tokens; perform CIT/MIT where permitted; view logs;
view benchmark results; export reports.

By default a normal user may **not**: create or manage users; view or decrypt
gateway credentials; change sensitive system configuration.

Enforce role checks on the backend. Hiding a frontend button is not enforcement.

### Status

`ACTIVE`, `INACTIVE`, `LOCKED`. An inactive or locked user must not be able to
log in.

### Edit, disable, password reset

- Admin can modify Full Name, Email, Role, Status. Changes are audit-logged.
- Do not permanently delete users by default — set `INACTIVE`, so audit history
  and transaction ownership are preserved.
- Admin can reset a user's password. Optionally users may change their own.
  Never display an existing password.

### Initial administrator

A secure bootstrap mechanism, e.g. `python -m app.cli create-admin`, or
environment-based initialization. Where environment initialization is used: only
create the account when it does not already exist; hash the password; never print
it to logs; recommend changing it after first login. **Do not hard-code an
administrator account in source.**

> Naming note: the existing contract (§7b) already defines
> `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`, and §7b forbids renaming
> an existing variable. The `ADMIN_USERNAME` / `ADMIN_EMAIL` /
> `ADMIN_INITIAL_PASSWORD` names given in the request are illustrative ("for
> example"), so the existing names are kept and `BOOTSTRAP_ADMIN_USERNAME` is
> added alongside them. Renaming would break every deployed `.env`.

### Ownership and audit fields

Transactions record `created_by_user_id`. Where appropriate also record
`requested_by_user_id` for Capture, Refund, Void, CIT, MIT and Inquiry, so the
study can identify who executed each operation.

### Audit events

`USER_CREATED`, `USER_UPDATED`, `USER_DISABLED`, `USER_ENABLED`,
`USER_ROLE_CHANGED`, `USER_PASSWORD_RESET`, `LOGIN_SUCCESS`, `LOGIN_FAILED`,
`LOGOUT`.

Each records: `user_id`, `performed_by`, `timestamp`, and IP address / user agent
where appropriate. Never include passwords or authentication secrets.

## 11. WEBHOOKS

Webhook support is a mandatory part of each provider integration where the
provider offers webhooks. It is part of the integration lifecycle, not a future
enhancement. **This section supersedes §4k.**

### Routes

```text
POST /api/webhooks/{provider}
POST /api/webhooks/{provider}/{profile_public_id}
```

for geidea, moyasar, checkout, hyperpay, adyen, stripe, paytabs.

Where multiple credential profiles exist for one provider, the second form
identifies which profile the webhook belongs to. Do not expose internal database
IDs; use an opaque public id. Do not place secrets in webhook URLs unless the
provider explicitly requires it.

### Responsibilities, in order

1. Receive the exact raw request. 2. Identify the provider. 3. Identify the
credential profile. 4. Validate authenticity. 5. Verify signature/HMAC/
certificate per the provider spec. 6. Reject invalid events. 7. Parse the
payload. 8. Determine the provider event type. 9. Correlate to the existing
transaction. 10. Store the event. 11. Update normalized transaction state if
appropriate. 12. Preserve provider-native status information. 13. Deduplicate.
14. Respond within the provider's timeout. 15. Do heavier processing
asynchronously. 16. Log processing errors.

### Signature verification

Every adapter implements the provider's official mechanism where one exists:

```python
async def verify_webhook(
    raw_body: bytes,
    headers: dict,
    credential_profile: ProviderCredentialProfile,
) -> bool: ...
```

Never accept a webhook merely because the provider name matches, or because the
payload contains a valid transaction ID.

**Preserve the raw body.** Read raw bytes → verify → parse JSON. Never parse,
re-serialize, then verify: re-serialization changes the bytes and breaks any
signature computed over the original payload.

### `WebhookEvent` model

```text
id, provider, credential_profile_id, provider_event_id, provider_event_type,
transaction_id, provider_transaction_id, signature_valid, received_at,
processed_at, processing_status, http_headers_sanitized, payload_sanitized,
normalized_status_before, normalized_status_after, error_message, retry_count
```

Unique constraint on provider event identifiers where the provider supplies one.

`processing_status`: `RECEIVED`, `VERIFIED`, `PROCESSED`, `IGNORED`, `FAILED`,
`DUPLICATE`, `INVALID_SIGNATURE`, `UNMATCHED`.

### Deduplication and idempotency

Providers resend. Process an event once; record later deliveries as duplicates.
Handling must be idempotent: receiving `PAYMENT_CAPTURED` twice must not create
two captures; receiving `REFUND_SUCCEEDED` twice must not double the refunded
amount.

### Correlation

Correlate on provider-native references — provider transaction ID, order ID,
payment ID, merchant reference, session ID, payment intent ID, PSP reference —
depending on the provider. Do not assume every provider uses the same identifier.

### Webhook vs redirect

For HPP and 3DS flows the browser redirect is **not** the final source of truth.
Final state may depend on API response + return URL + webhook + inquiry. The
Result page must handle pending states properly:

```text
Customer returns → transaction still PENDING → webhook arrives → CAPTURED
```

### Webhook timing and benchmarking

Record `payment_start_time`, `provider_api_completion_time`, `return_url_time`,
`webhook_received_time`, `final_state_time`, so the study can distinguish:

```text
Provider API Time:      620 ms
Customer/3DS Time:      8.3 s
Webhook Finalization:   1.4 s
End-to-End Time:        10.5 s
```

**Do not include webhook delivery delay in provider API-call latency.** Expose it
separately.

### Webhook logs screen

Under `Logs → Webhooks`, or a Webhooks tab in transaction details. List: Received
At, Provider, Event Type, Provider Event ID, Transaction, Signature Status,
Processing Status, Processing Duration. Detail: headers, sanitized payload,
provider event type, associated transaction, received and processed timestamps,
signature verification result, processing result.

### Webhook management in Settings

Per credential profile show: Webhook URL, Webhook Status, whether a signing
secret is configured, Last Webhook Received, Last Verified Webhook. Provide a
Copy Webhook URL button. **Never expose the signing secret after it is saved.**

### Provider documentation

Each provider doc file records: webhook URL to configure, provider dashboard
location, events required, signature verification method, secret/certificate
requirements, expected retry behaviour, expected acknowledgement response.

Handle events needed to track payment success, payment failure, authorization,
capture, void/cancellation, refund, 3DS/payment completion, and token/stored
credential events, where exposed. Do not subscribe blindly to every event.

### Reconciliation service

```text
API Response + Return Handler + Webhook Event + Inquiry Result
        ↓
Transaction Reconciliation Service
        ↓
Normalized Transaction Status
```

Provider-specific webhook logic must not scatter transaction state changes across
the application. Centralize normalized state transitions.

## 12. NAVIGATION

```text
Dashboard
Test Payment
Transactions
Tokens
Benchmark
Logs
  ├── API Calls
  └── Webhooks
Users            (administrators only)
  ├── User List
  └── Create User
Settings
  ├── Payment Gateways
  └── System
```

## 13. UPDATED ACCEPTANCE CRITERIA

In addition to §8. BuraPay is not complete until all of these are demonstrable.

**Authentication**

1. Open `https://busrapay.com`.
2. Be redirected to Login when unauthenticated.
3. Log in successfully.
4. Log out.
5. Be prevented from accessing protected APIs without authentication.

**User management**

6. Log in as Admin. 7. Open Users. 8. Create a new user. 9. Assign a role.
10. Disable the user. 11. Re-enable the user. 12. Reset the user's password.
13. See the user's last login. 14. Prevent a normal user from reaching
administrator-only user-management APIs. 15. See user-management actions in the
audit log.

**Webhooks**

16. View the correct webhook URL for each provider profile. 17. Configure that
URL in the provider sandbox. 18. Receive a real sandbox webhook. 19. Verify its
provider signature. 20. Correlate it with the correct transaction. 21. Update the
transaction state. 22. Detect duplicate delivery. 23. Avoid double-processing
duplicates. 24. View the webhook in Logs. 25. View the sanitized payload.
26. See whether signature verification passed. 27. See processing duration.
28. See the webhook on the Transaction Detail page. 29. See webhook finalization
time separately from provider API latency. 30. Have HPP/3DS transactions
correctly reconciled using callback + webhook + inquiry where required.

These are mandatory features, not optional enhancements.

---

## Appendix A — Implementation status

Recorded 2026-08-22. **This appendix is status, not requirement.** Update it when the
code changes; never edit §0–§13 to match it.

§9 and §10 are built. §11 and the §12 navigation changes that depend on it are not.

### §9 Authentication — built

| Requirement | Where |
|---|---|
| Login by username **or** email | `app/services/auth.py:find_by_handle`, `POST /v1/auth/login` |
| bcrypt hashing, never stored or logged in the clear | `app/core/security.py` |
| Password complexity | `validate_password`, enforced on create, reset and self-change |
| Generic failure, constant work | one `GENERIC_FAILURE` for all four causes; `burn_password_time` on an unknown handle |
| Session management, logout, expiry | signed HS256 tokens, `ACCESS_TOKEN_EXPIRE_MINUTES`; `POST /v1/auth/logout` clears both cookies |
| Secure cookies | `HttpOnly` + `Secure` (when `PUBLIC_BASE_URL` is HTTPS) + `SameSite=Lax` |
| CSRF | double-submit token, required on cookie-authenticated writes; bearer requests are exempt by construction |
| Login rate limiting | per-address sliding window, `app/services/auth.py:SlidingWindowLimiter` |
| Brute-force protection | per-account lockout to `LOCKED`, self-expiring after `LOGIN_LOCKOUT_MINUTES` |
| Protected frontend routes and backend APIs | `RequireAuth`/`RequireAdmin` in the UI; `require_user`/`require_admin` in `app/api/deps.py` |
| Audit of successful and failed logins | `LOGIN_SUCCESS`, `LOGIN_FAILED`, `LOGOUT`, including handles that match no account |

`SameSite=Lax` rather than `Strict` is deliberate: a gateway returning the browser from
a hosted payment page is a top-level cross-site GET, and `Strict` would drop the session
exactly there. Lax withholds the cookie from cross-site writes, and the CSRF token
covers what remains.

### §10 User management — built

| Requirement | Where |
|---|---|
| Roles `ADMIN` / `USER` | `UserRole`; `normalize_role` still reads the historical `admin`/`viewer` values |
| Status `ACTIVE` / `INACTIVE` / `LOCKED` | `UserStatus`; only `ACTIVE` may sign in |
| Users screen, filters, create/edit/disable/enable/reset | `/v1/users`, `frontend/src/pages/Users.tsx`, `CreateUser.tsx`, `UserDetail.tsx` |
| Disable rather than delete | there is no delete route; `POST /v1/users/{id}/disable` |
| Backend role checks | `require_admin` on every `/v1/users` and `/v1/audit-logs` route |
| `created_by_user_id` on transactions | set in `start_transaction`; shown as "Run by" on Transaction Detail |
| `requested_by_user_id` on operations | set for CIT/MIT with `requested_operation`; extend as capture, refund, void and inquiry endpoints land with §11 |
| Audit events table | `audit_logs`; read-only at `/v1/audit-logs` and **Users → Audit Log** |
| Bootstrap admin | `BOOTSTRAP_ADMIN_USERNAME` / `_EMAIL` / `_PASSWORD`, or `python -m app.cli create-admin` |

Migration `7b41c9de20a4` carries existing data across: usernames are backfilled from
the local part of each email (collisions get a numeric suffix), `is_active` becomes
`status`, and `admin`/`viewer` become `ADMIN`/`USER`. `viewer → USER` is a deliberate
widening — a viewer could not run payment tests and a `USER` can — and it is the
mapping §10 asks for.

An administrator cannot demote or disable their own account; another administrator can.
That is the smallest rule that prevents a deployment being locked out of itself without
needing a racy "how many admins are left" count.

### §11 Webhooks

| Requirement | On `master` | Delta |
|---|---|---|
| Webhook receiver | present (98 references) | exists |
| `WebhookEvent` model | `webhook_events`: gateway_code, transaction_id, received_at, event_type, signature_verified, merchant_reference, payload | **extend** |
| `provider_event_id` + unique constraint | 0 hits | **add** — dedup depends on it |
| `processing_status` enum | 0 hits | **add** |
| `credential_profile_id` | no profile concept at all (0 hits) | **add** — needed for `/{profile_public_id}` routing |
| `signature_valid` | field is named `signature_verified` | naming — pick one, migrate |
| `normalized_status_before/after`, `retry_count`, `error_message`, `processed_at`, `http_headers_sanitized` | absent | **add** |
| Webhook timing fields (5) | absent | **add** |
| Reconciliation service | scattered | **centralize** |
| PayTabs adapter | 0 hits | **add** (7th provider) |

### Sequencing note

Credential profiles are load-bearing: the webhook URL scheme, Settings display
and `WebhookEvent.credential_profile_id` all depend on that model existing. Build
it before the webhook work, or the webhook work gets built twice.

The §12 navigation is likewise deferred with §11. The `Users` section and its
sub-navigation are in place, and `Layout.tsx` now renders nested groups, so the
remaining regrouping (`Logs → API Calls / Webhooks`, `Settings → Payment Gateways /
System`, `Tokens`, `Test Payment`) is a rename of existing routes that belongs in the
same change as the screens it points at.
