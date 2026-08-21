# BuraPay Gateway Benchmark

A platform for measuring and comparing the technical performance of payment gateways,
from the customer's point of view: how long each gateway API call takes, how long a
complete transaction takes, and where the time actually goes.

Six gateways are supported — **Geidea, Stripe, Adyen, Checkout.com, HyperPay,
Moyasar** — plus a local **MockPay** simulator for working on the platform itself
before any sandbox credentials exist. Each is measured in both integration models the
gateway offers: **HPP / Hosted Checkout** and **Direct API**.

Runs at **https://busrapay.com**. Sandbox environments only, unless production access
is explicitly and deliberately unlocked.

---

## Contents

- [What this measures, and what it refuses to](#what-this-measures-and-what-it-refuses-to)
- [Architecture](#architecture)
- [Quick start with Docker](#quick-start-with-docker)
- [Traefik integration](#traefik-integration)
- [Local development](#local-development)
- [Environment variables](#environment-variables)
- [Database migrations](#database-migrations)
- [Gateway architecture](#gateway-architecture)
- [Adding a new payment gateway](#adding-a-new-payment-gateway)
- [Running the tests](#running-the-tests)
- [Production deployment](#production-deployment)
- [Credentials and security](#credentials-and-security)
- [Benchmark methodology](#benchmark-methodology)
- [Metric definitions](#metric-definitions)
- [Project layout](#project-layout)

---

## What this measures, and what it refuses to

The platform exists to answer one question honestly: *which gateway is technically
faster, and by how much?* Several design decisions follow from taking that seriously.

**Gateway latency and customer time are never mixed.** A customer who takes fifteen
seconds to type an OTP does not make the gateway's API fifteen seconds slow. Four
figures are recorded separately for every transaction and never summed into one score:

| Figure | What it is | Whose responsibility |
| ------ | ---------- | -------------------- |
| Gateway API time | Sum of the timed merchant-server calls | The gateway |
| 3DS / authentication time | Between 3DS initiation and completion | Issuer and customer |
| Customer interaction time | Time spent on a hosted page | The customer |
| End-to-end time | Start to final state | Everything above combined |

**Timing is server-side and monotonic.** Every outbound call goes through one
instrumented client that measures with `time.perf_counter()` from immediately before
the request to the moment the response body has been fully read. Reading the body is
inside the measurement deliberately: a gateway that returns headers fast and dribbles
the body out is slower, and stopping at the headers would hide that. Browser timings
are collected too, where the Performance API allows it, but they are stored in their
own table and never merged with the server-side numbers.

**Setup calls are recorded but not charged to the gateway.** Minting a card token is a
browser-side step in any real integration. Counting it as merchant-server latency
would make a gateway that requires tokenization look slower than it is. Those calls
appear in the transaction detail, marked, and excluded from the reported API time.

**Nothing is presented that could not be measured.** A metric whose bracketing events
did not both occur is stored and rendered as null — an em dash in the UI, never `0 ms`.
Adyen documents no pull-based status endpoint for its Sessions flow, so the platform
reports the transaction as pending rather than inventing a status call to time.

**No ranking on thin data.** Percentiles are nearest-rank, so every figure reported is
a latency that actually occurred rather than an interpolation between two that did.
Groups below twenty samples are flagged as unreliable; below ten they are excluded
from any ranking entirely, with the reason shown.

**Averages never travel alone.** Every group carries count, min, max, mean, median,
p50, p90, p95, p99, standard deviation and success rate together.

---

## Architecture

```
                    Internet
                       │
                       ▼
            ┌─────────────────────┐
            │  Traefik  (TLS)     │  already running on the VPS,
            └──────────┬──────────┘  not managed by this repository
                  ┌────┴─────┐
                  ▼          ▼
          ┌────────────┐  ┌──────────────┐
          │  frontend  │  │   backend    │  Host(busrapay.com)
          │ React+Vite │  │   FastAPI    │  && PathPrefix(/api)
          └────────────┘  └──────┬───────┘
                                 ▼
                          ┌─────────────┐
                          │ PostgreSQL  │  internal network only
                          └─────────────┘
```

Two Docker networks. The **frontend** and **backend** join the existing Traefik
network, because Traefik has to reach them. **PostgreSQL** sits alone with the backend
on an `internal: true` network that has no route to or from the internet, and carries
`traefik.enable=false` so it can never be exposed by accident. No service publishes a
host port; the only way in is through Traefik.

| Layer | Technology |
| ----- | ---------- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, httpx |
| Database | PostgreSQL 16 |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, Recharts |
| Proxy | the VPS's existing Traefik, via Docker labels |

---

## Quick start with Docker

```bash
cd /opt
git clone https://github.com/abushelhah-wq/burapay.git
cd burapay
cp .env.example .env
```

Generate the two secrets and put them in `.env`:

```bash
# APP_SECRET_KEY
openssl rand -hex 32

# ENCRYPTION_KEY — back this up; losing it means losing every stored credential
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set `POSTGRES_PASSWORD`, `BOOTSTRAP_ADMIN_PASSWORD` and `DOMAIN`.

Then read the Traefik settings off the VPS rather than guessing them — this is the one
part of the deployment where a wrong value can affect something else on the host:

```bash
./scripts/inspect-traefik.sh
```

It reports which Docker network Traefik is on, which entrypoint and certificate
resolver the host's other applications already use, whether a global HTTP→HTTPS
redirect exists, and whether anything already routes `busrapay.com`. It reads; it
changes nothing. Put its suggestions in `.env` after checking them against its output,
then:

```bash
docker compose build
docker compose up -d
docker compose ps

curl https://busrapay.com/api/health
# {"status":"healthy","database":"connected","version":"1.0.0",...}
```

TLS is Traefik's. This project installs no certbot, stores no certificate, and changes
no existing router, network or resolver. Details in
[Traefik integration](#traefik-integration).

### Signing in and configuring a gateway

Sign in at `https://busrapay.com` with the `BOOTSTRAP_ADMIN_EMAIL` and
`BOOTSTRAP_ADMIN_PASSWORD` from `.env`. That account is created on first boot, only
when the platform has no users at all — changing those variables later does nothing,
because by then the account exists. Change the password from **Settings** immediately.

Gateway credentials are then entered **in the browser**, not in a file:

1. **Settings → Gateway credentials → Geidea** (or any other gateway). The form is
   generated from what the adapter declares, with help text on every field.
2. Paste the sandbox values and save. They are encrypted with `ENCRYPTION_KEY` before
   they touch the database, and no endpoint ever returns one in full again — reads come
   back masked, and re-saving without retyping a secret leaves the stored value alone.
3. **Gateways → Run health check** confirms they authenticate, using a read-only
   endpoint. No payment is created.
4. **Run Benchmark** → the gateway is now selectable and the button is live.

Rotating a key is the same three clicks. There is no `.env` edit and no redeploy.

### First run without any gateway credentials

MockPay is configured out of the box. Go to **Run Benchmark**, pick MockPay, choose
Direct API, and start a run of twenty transactions: the timeline, the statistics, the
comparison table and the exports all populate. MockPay is a simulator, so its timings
measure this platform's own overhead rather than any gateway's — it is excluded from
rankings and labelled everywhere it appears.

---

## Local development

Two terminals. Backend:

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export DATABASE_URL="postgresql+asyncpg://burapay:burapay@localhost:5432/burapay"
export APP_SECRET_KEY="dev-secret"
export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export PUBLIC_BASE_URL="http://localhost:8000"
export CORS_ORIGINS="http://localhost:5173"

alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxying /api to :8000
```

The dev server proxies `/api` to the backend so the browser talks to a single origin,
exactly as it does in production. Without that, cookie-based auth and the gateway
return leg would behave differently in development than in production.

Only MockPay works fully over plain HTTP. Real gateway sandboxes reject non-HTTPS
return and callback URLs, so HPP flows against them need a public HTTPS origin —
a tunnel to your machine, or the deployed instance.

API documentation is at `http://localhost:8000/api/docs`.

---

## Environment variables

Full list with commentary in [`.env.example`](.env.example). The ones that matter:

| Variable | Purpose |
| -------- | ------- |
| `APP_SECRET_KEY` | Signs session tokens. The application refuses to start in production with the development default. |
| `ENCRYPTION_KEY` | Fernet key encrypting stored gateway credentials. **Back it up.** There is no recovery path if it is lost. |
| `DATABASE_URL` | PostgreSQL DSN with the asyncpg driver. |
| `PUBLIC_BASE_URL` | The public HTTPS origin. Gateways redirect browsers back to it and POST webhooks to it. |
| `CORS_ORIGINS` | Comma-separated browser origins allowed to call the API. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | The first administrator, created only when the platform has no users at all. |
| `DOMAIN` | The host Traefik routes to this stack. |
| `TRAEFIK_NETWORK` | Only when Traefik runs in its own Docker network — see [Traefik integration](#traefik-integration). |
| `TRAEFIK_ENTRYPOINT` | The existing HTTPS entrypoint name. |
| `TRAEFIK_CERT_RESOLVER` | The existing certificate resolver. |
| `ALLOW_PRODUCTION_GATEWAYS` | Defaults to `false`. Sandbox-only enforcement lives in the benchmark engine, not the UI. |
| `BENCHMARK_MIN_INTERVAL_SECONDS` | Floor on the pause between automated transactions. |

Gateway credentials are **not** environment variables. They are entered on the
Settings page and stored encrypted, so rotating a sandbox key needs no redeploy.

---

## Database migrations

```bash
cd backend

alembic upgrade head                              # apply
alembic revision --autogenerate -m "add a thing"  # create after a model change
alembic downgrade -1                              # roll back one
alembic check                                     # models and migrations agree?
```

`alembic upgrade head` runs automatically in the backend container's entrypoint, before
the server starts, so a deploy never serves traffic against an un-migrated schema. CI
runs `alembic check` against a real PostgreSQL instance, so a model change with no
migration fails the build rather than the deployment.

---

## Gateway architecture

Every gateway lives behind one interface in `backend/app/gateways/`. Nothing
gateway-specific exists anywhere else — not in a route, not in a service, not in the
database layer.

```python
class PaymentGatewayAdapter:
    async def create_hpp_session(client, request) -> HppSession
    async def confirm_hpp_payment(client, request, context, params) -> PaymentResult
    async def process_direct_payment(client, request) -> PaymentResult
    async def get_payment_status(client, payment_id) -> PaymentResult
    async def refund_payment(client, payment_id, amount, currency) -> PaymentResult
    def parse_webhook(headers, body) -> dict
    async def health_probe(client) -> HealthProbe
```

HPP is two-legged because it genuinely is: the server creates a session, the browser
leaves for the gateway's page, and only when it returns can the outcome be confirmed.
The customer's time in between is recorded as customer interaction time and never as
gateway latency.

Adapters declare their credential fields rather than reading environment variables:

```python
credential_fields = (
    CredentialField("secret_key", "Secret Key", secret=True, placeholder="sk_test_…"),
    CredentialField("api_base", "API Base URL", required=False,
                    default="https://api.example.com"),
)
```

That single declaration drives the Settings form, validation, masking, and the
"Not Configured" state — so a gateway added later gets a working configuration UI with
no frontend change.

### What each gateway is measured on

| Gateway | HPP | Direct API | Documented minimum calls |
| ------- | --- | ---------- | ------------------------ |
| Geidea | Payment session → mounted checkout script | Session → initiate → authenticate payer → pay | HPP 2, Direct 4 (fixed) |
| Stripe | Checkout Session | PaymentIntent create+confirm | HPP 1 + webhook, Direct 1–2 |
| Adyen | Sessions flow (Drop-in) | Advanced flow `/payments` | HPP 1 + webhook, Direct 2–3 |
| Checkout.com | Hosted payment link | `/payments` with a token | HPP 2, Direct 1–2 |
| HyperPay | Copy&Pay widget | `/v1/payments` | HPP 2, Direct 1–2 |
| Moyasar | Invoice | Token or raw-card `/v1/payments` | HPP 2, Direct 2–3 |
| MockPay | Simulated | Simulated | n/a — a simulator |

The documented figure sits next to the measured one everywhere, so a gateway that
needs more calls than its documentation claims is visible immediately. That divergence
is one of the more useful things this platform produces.

Three of the six do not redirect for hosted checkout at all. Geidea, Adyen and
HyperPay each return an identifier for their own browser component, so the platform
mounts that component in a page of its own rather than sending the customer away.
Geidea's script URL follows the configured region and can be overridden on the
Settings page for an account served from a different host.

Where a detail could not be confirmed from a vendor's public documentation, the
adapter says so in its notes and makes the detail configurable rather than guessing
silently. Three such cases are live today: Geidea's signature field order and
timestamp format, HyperPay's back-office URL shape for refunds, and Moyasar's
contradictory guidance on raw-card versus tokenize-first Direct payments.

### Geidea: stored cards and agreements

Geidea can store a card against an agreement and charge it later without card details.
That is a different flow with a different call count, so it is a **payment mode** on
the Run Benchmark page rather than something hidden inside the ordinary Direct flow —
and every comparison groups by it, because averaging a two-call token charge together
with a four-call card payment would describe neither.

| Mode | What it does | Calls |
| ---- | ------------ | ----- |
| Standard card payment | A one-off payment with card details | 4 (3 when frictionless) |
| Store card (tokenize) | Pays *and* asks Geidea to keep the card, returning a token | 4, plus the token |
| Stored token | Charges the stored card, MIT or CIT. No card details, so nothing to authenticate | 2 — Geidea still needs a fresh session per charge |

The round trip:

1. **Run Benchmark → Geidea → Direct API → Store card (tokenize)**, enter a sandbox
   test card, and run one payment. Leave **Agreement ID** blank and one is minted.
2. The transaction page shows **Charge this card again** — the card token and the
   agreement id, each with a copy button.
3. **Stored token** mode, paste both into the payment form, pick **MIT** or **CIT**, and
   run it. Or save them on **Settings → Geidea** to stop typing them.

Nothing has to go through Settings: the payment form takes both directly, so a token
minted a minute ago can be charged without a detour.

The platform never writes a minted token into the gateway's credentials by itself. A
card token belongs to a cardholder rather than to the merchant account, and storing one
is a decision to take deliberately — so it is shown once, on the transaction that
created it, and copied across on purpose.

All four tokenisation fields are optional. Geidea works for ordinary card payments with
none of them set.

### Paying by Direct API: what to enter, and where

Geidea's Direct API is four calls, and the three that carry a card **do not agree on
shape**:

    POST /payment-intent/api/v2/direct/session
    POST /pgw/api/v6/direct/authenticate/initiate   cardNumber at the top level
    POST /pgw/api/v6/direct/authenticate/payer      card nested under paymentMethod
    POST /pgw/api/v2/direct/pay                     card optional; threeDSecureId binds it

That difference is not cosmetic. Sending Initiate Authentication a `paymentMethod`
object — the shape the *next two* calls want — means Geidea finds neither a card number
nor a token where it looks for them, and answers `responseCode=100` "General error"
with `detailedResponseCode=069` **Missing Token Id**. The error names the token half of
a check that failed for want of a card, which is why it reads as though a token were
missing when the real problem is a misplaced card number.

Three more details that are easy to get wrong and produce nothing but a generic error:

* `expiryDate` is an object holding two zero-padded **strings** —
  `{"month": "01", "year": "39"}` — not `expiryMonth` / `expiryYear`, and not integers.
* `source` is an enum, not free text. Geidea's own samples use `DirectAPI`.
* Initiate Authentication's samples always carry `merchantName`, `cardOnFile`,
  `isSetPaymentMethodEnabled`, `isCreateCustomerEnabled` and `restrictPaymentMethods`.
  Omitting them gets `detailedResponseCode=013` "Internal Server Error", which does not
  say which field it disliked — so the adapter always sends all of them.

### Moyasar Direct API: it pauses too

A Moyasar Direct payment authorises nothing on its own. It is created as `initiated` and
stays there until the cardholder has been through the 3DS page at
`source.transaction_url`. A run that reports `initiated` and stops has recorded a payment
that was never actually attempted — so the platform sends them there and finishes on the
way back, the same two-legged shape as Geidea's challenge.

`callback_url` is where Moyasar sends the *customer*, so it is this platform's return
leg, not the webhook URL. Pointing it at the webhook is what left those payments stuck.

### HyperPay result code 100.390.106

`Transaction rejected because of error in 3DSecure configuration` is an **account
setting, not a payload problem**. OPPWA rejects the payment before it ever reaches the
issuer, because 3-D Secure is not enabled for that entity and card brand. No request
change fixes it: ask HyperPay to enable 3DS2 for the entity id, quoting the clearing
institute in `resultDetails` if there is one.

Against a **sandbox** entity there is a way to test the flow anyway. Set the *3DS test
scenario* on HyperPay's settings page to `challenge` or `frictionless` and the adapter
sends OPPWA's documented test parameters — `testMode=EXTERNAL`,
`customParameters[3DS2_enrolled]=true`, `customParameters[3DS2_flow]` — so a test entity
simulates an enrolled card. It is off by default and should stay off on an entity that is
genuinely 3DS-enabled: it replaces a real authentication outcome with a simulated one,
which is not a thing to benchmark by accident.

`resultDetails` is now reported alongside `result.code` on every HyperPay outcome. On a
rejection it is frequently the only part that says which end the problem is at.

### Call Log

Geidea answers a malformed request with `responseCode=100` "General error" and a detailed
code that is often no more specific. There is no way to reason about that from the
outside, so **Call Log** in the sidebar lists every request this platform has made to a
gateway and every response that came back, with the time each one took.

Filter by gateway, operation or outcome, search across endpoint / operation / message,
and expand any row for the request and the response side by side. That last part is the
point: a working call is what you compare a broken one against. Each transaction page
links straight through to its own calls.

Both bodies were sanitized before they were ever stored — card numbers truncated to a
last four, CVVs dropped by name, secrets redacted. There is no unsanitized copy anywhere
for this to be compared against; the scrubbing happens on the way in, not on the way out,
and a test asserts a PAN cannot reach the log.

`DELETE /v1/logs` (the **Clear log** button, admin only) drops recorded calls. Transaction
timings are columns on the transaction row, so clearing the log never rewrites a number
that has already been reported.

**Run Benchmark → Direct API** has a *Payment details* step:

* **Card details** — a sandbox test card. Only shown when `ALLOW_DIRECT_CARD_ENTRY=true`;
  see [Card entry](#card-entry).
* **Card token ID** — always available. A token is not card data, so it works on an
  account not cleared to receive card numbers.
* **Agreement ID** — the agreement a stored card is charged under.

The browser also sends its user agent, language and time-zone offset, because 3DS risk
engines score a payment partly on those and a server-to-server call cannot see them. An
issuer given none of it challenges more often. Whatever the browser could not see is
left out rather than invented.

### Tokens, and charging a card again (MIT / CIT)

**There is no tokenize endpoint.** A token is minted *by a payment*: the session carries
`cardOnFile: true` and a `cofAgreement`, and the token comes back on the successful
payment. That initial payment is customer-initiated and must complete 3DS.

**The agreement ID is yours, not Geidea's.** You choose the value, send it on the
card-on-file payment, and quote the same value on every later charge. Leave the field
blank on a **Store card (tokenize)** payment and this platform mints one for you.

Any payment that returns a token shows it — not only the mode named "store card". The
transaction page has a **Charge this card again** panel with both halves and a copy
button each. Both are needed: a token without its agreement is refused, and the
agreement is a value this platform chose, so there is nowhere else to look it up.

A stored-card charge goes to `POST /pgw/api/v2/direct/pay/token` and asks who is
initiating it:

| Choice | Sent as | When |
| ------ | ------- | ---- |
| Merchant-initiated (MIT) | `initiatedBy: "Merchant"` | An unattended charge — a subscription renewal, a retry. Nobody is watching. |
| Customer-initiated (CIT) | `initiatedBy: "Internet"` | The cardholder is here, picking a card they saved earlier. |

The card networks price and authorise those differently, so it is chosen per payment on
the form rather than fixed in settings. Settings holds the default.

### 3-D Secure: the payment stops for the cardholder

When the issuer wants a challenge, a Direct payment cannot finish server-side, so it
does not pretend to. The adapter returns after Authenticate Payer, the transaction stays
`PENDING`, and the browser is sent to the challenge — either the URL the issuer gave, or
its auto-submitting form rendered in a sandboxed iframe on `/three-ds/{id}`. A sandboxed
frame is its own opaque origin: the issuer's markup can post itself and navigate, and it
cannot read this application's DOM, storage or session.

The issuer returns the cardholder to `/api/v1/transactions/{id}/return` — the same
return leg hosted checkout uses — and the server runs the Pay call and records the final
status. **Only the two server legs count as gateway API time.** The OTP, the issuer's
page and the round trip through the browser land in `customer_interaction_time_ms`,
where they can neither flatter nor penalise the gateway. A payment left waiting shows a
*Continue verification* link on its transaction page.

**The challenge runs at the top level, never in a frame.** An iframe seems like the
obvious place for it, and it does not work: the challenge *ends* by sending the browser
back to this application's return URL, and inside a frame that return is refused by our
own `X-Frame-Options: DENY` — the cardholder is left looking at an empty box. At the top
level the header never applies.

That leaves the other problem: when the issuer hands back a form rather than a URL, that
markup would then be running on our own host. So it is served from
`/api/v1/transactions/{id}/three-ds/challenge` under
`Content-Security-Policy: sandbox allow-forms allow-scripts allow-top-navigation`. With
no `allow-same-origin` the document lands in an opaque origin: it can post itself to the
issuer and navigate the window, and it cannot read this application's cookies, storage
or DOM. The route answers only while that transaction is actually waiting.

**A fragment is not a page.** What gateways return under names like `htmlBodyContent` is
a body fragment: a form of *hidden* inputs addressed to the issuer's access control
server. Hidden inputs render as nothing, and plenty of gateways expect the merchant to
submit the form rather than shipping a script that does it — so serving the fragment
directly produces a blank screen. It is wrapped in a real document that submits the form
itself, says what it is doing while it happens, and offers a button if the automatic
submit does not fire.

If what arrived contains no form at all, the page says so and shows what was stored,
escaped rather than injected. A gateway sending something unexpected here is a fact
worth seeing, not something to render blindly.

The card **does not survive the pause**. It is not written down, not held between
requests and not put in the transaction's context, so the Pay call on the way back
identifies the payment by `sessionId`, `orderId` and `threeDSecureId` instead — which
Geidea already holds the card against. Holding card data across two requests to avoid
that would be the wrong trade.

---

## Adding a new payment gateway

1. Write `backend/app/gateways/yourgateway.py` subclassing `PaymentGatewayAdapter`.
   Declare `code`, `display_name`, `supports_hpp`, `supports_direct`,
   `supported_currencies` and `credential_fields`, then implement the flows the
   gateway offers.
2. Append the class to `ADAPTER_CLASSES` in `backend/app/gateways/registry.py`.
3. Add tests in `backend/tests/test_adapters.py` driving it against a mock transport.
4. Restart. The gateway appears in the catalogue, the Settings page renders its
   credential form, `/api/webhooks/yourgateway` starts accepting callbacks, and it
   becomes selectable on the Run Benchmark page.

No migration, no route, no frontend change. The architecture is built for Amazon
Payment Services, Tap, PayTabs, Network International, MPGS, Cybersource and the
wallet providers to arrive this way.

Two rules when writing an adapter:

* **Use the provider's documented API.** Do not invent parameters. If something is
  undocumented, make it configurable and say so in `notes`.
* **Issue every call through the instrumented client.** A call made any other way is
  a call nobody timed, and it will silently corrupt the comparison.

---

## Running the tests

```bash
cd backend && pytest -q           # 146 tests
cd backend && ruff check app tests

cd frontend && npm test
cd frontend && npm run build      # includes the type check
```

The backend suite runs against SQLite and needs no services. It covers timing
measurement, percentile maths, credential encryption, sensitive-data sanitization,
error normalization, all seven adapters, the transaction lifecycle, webhook
processing, exports and the admin/viewer boundary.

CI runs all of it on every push, plus `alembic check` against real PostgreSQL and a
build of both Docker images.

---

## Traefik integration

BuraPay runs behind the Traefik that is **already** on the VPS. It starts no Traefik of
its own, creates no certificates, and changes nothing belonging to the other
applications on that host.

### The three values to read, never guess

```bash
./scripts/inspect-traefik.sh
```

| `.env` variable | What it is | How it fails if wrong |
| --------------- | ---------- | --------------------- |
| `TRAEFIK_ENTRYPOINT` | The HTTPS entrypoint name (`websecure`, `https`, …) | Quietly. Traefik never routes the domain and requests fall through to whatever its default is. |
| `TRAEFIK_CERT_RESOLVER` | The certificate resolver (`letsencrypt`, `le`, `cloudflare`, …) | Quietly. Traefik serves its own self-signed certificate and browsers refuse the site. |
| `TRAEFIK_NETWORK` | **Only** for a Traefik that runs in its own Docker network — see below | Loudly, and only if you use the override: the network is `external`, so compose stops rather than starting a stack Traefik cannot see. |

### Two Traefik topologies, and which you have

`./scripts/inspect-traefik.sh` tells you which, and prints the command to use.

**Traefik in host network mode** — the common single-VPS arrangement. There is no
shared network to join: Traefik discovers containers through the Docker provider and
connects to them by their address on an ordinary bridge. Nothing extra to configure:

```bash
docker compose up -d
```

**Traefik in its own Docker network.** Set `TRAEFIK_NETWORK` and add the override that
joins it:

```bash
docker compose -f docker-compose.yml -f docker-compose.traefik-network.yml up -d
```

Either way the services sit on a `web` bridge with a fixed name (`burapay_web`) so the
`traefik.docker.network` label is predictable regardless of the directory this was
cloned into — and PostgreSQL stays off it entirely.

The script reads them off the running Traefik container and off what the host's other
applications already do, then prints a suggested `.env` block. Confirm each value
against the output rather than pasting it blind — the two quiet failures above are
worth thirty seconds of checking.

It also answers two questions that protect what is already running:

* **Does anything already route `busrapay.com`?** Two routers matching one host is how
  a working application gets taken off the air.
* **Is there already a global HTTP→HTTPS redirect?** If there is, BuraPay must not add
  a second one — that is a redirect loop. The per-domain redirect labels are in
  `docker-compose.yml`, commented out, for hosts that have no global one.

### Routing, and why there is no path rewriting

```
Host(`busrapay.com`) && PathPrefix(`/api`)   →  backend  :8000   priority 100
Host(`busrapay.com`)                         →  frontend :80     priority 1
```

There is **no StripPrefix middleware**, deliberately. FastAPI is mounted at `/api`
through its `root_path`, so `/api/health`, `/api/docs` and `/api/v1/…` are the
application's real paths, and Traefik forwards the request byte for byte. Nothing
rewrites anything, so the classic `/api/api/v1/…` cannot arise — a request for it
returns 404, as it should.

The backend router carries the higher priority. Traefik's default tie-break on rule
length would almost certainly pick it anyway, since its rule is the longer one; stating
the priority means correctness does not rest on that.

Both containers also carry `traefik.docker.network`. That label matters whenever a
container is on more than one network: without it Traefik can pick the internal address
it cannot reach, and the route fails intermittently in a way that is unpleasant to
diagnose.

### If TLS is terminated somewhere else

If the VPS's Traefik sits behind Cloudflare or another terminator and does not issue
certificates itself, drop these two labels from both services in `docker-compose.yml`:

```yaml
- "traefik.http.routers.burapay-*.tls=true"
- "traefik.http.routers.burapay-*.tls.certresolver=${TRAEFIK_CERT_RESOLVER}"
```

and point `TRAEFIK_ENTRYPOINT` at whichever entrypoint that setup uses. Keep
`PUBLIC_BASE_URL` on `https://` regardless: it is what gateways are told to redirect
and post webhooks to, and every sandbox rejects a plaintext URL for either.

### What this deployment will not touch

No Traefik container, no global Traefik configuration, no existing router, network,
entrypoint, certificate or resolver. The only network this project creates is its own
`internal` one; the Traefik network is joined, never modified. PostgreSQL is never
exposed to it.

## Production deployment

```bash
cd /opt/burapay
git pull
docker compose build
docker compose up -d
docker compose ps

curl https://busrapay.com/api/health     # {"status":"healthy",...}
open https://busrapay.com/api/docs       # OpenAPI documentation
open https://busrapay.com                # the application
```

Migrations run in the backend entrypoint before the server starts.

If a route does not come up, Traefik saw the container but disagreed with its labels.
Its own log says which: `docker logs <traefik-container> | grep -i burapay`.

> **Not yet built in anger.** The images have not been built end to end: the
> environment this was developed in blocks Docker Hub's blob CDN, so
> `docker compose build` could not run here. Everything else was verified against a
> real PostgreSQL 16 and a real browser. Expect the first `docker compose build` on
> the VPS to be the first true test of the Dockerfiles.

No service publishes a host port. The frontend and backend are reachable only through
Traefik, and PostgreSQL is on an `internal: true` network with no route to the internet
at all — so there is no path to the API or the database that does not pass through the
proxy.

Every service declares a health check. The backend's exercises `/health`, which
touches the database — a process that is up but cannot reach PostgreSQL reports
unhealthy rather than returning a cheerful 200.

To deploy from GitHub Actions, add a workflow that SSHes to the VPS and runs the
commands above, with the host and key held in **GitHub Secrets**. No VPS credential
belongs in this repository.

### Backups

```bash
docker compose exec postgres pg_dump -U burapay burapay | gzip > burapay-$(date +%F).sql.gz
```

A database backup is useless without `ENCRYPTION_KEY`: the credential blobs cannot be
decrypted with anything else. Back the key up separately, and not on the same server.

---

## Credentials and security

* Gateway secrets are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before
  storage. The database alone is useless to anyone without `ENCRYPTION_KEY`.
* **No API endpoint returns a secret in full** — not for admins, not for debugging.
  Reads are masked: `sk_test_************X92D`. A test asserts this against every
  endpoint that touches credentials.
* Secrets never reach the browser, never appear in HTML, and never appear in a log.
* Saving credentials is a merge: a masked value echoed back by the UI leaves the
  stored secret alone, so the Settings form can safely show masks.
* Every log line and every persisted response excerpt passes through a sanitizer that
  redacts secrets by field name and truncates anything that passes a Luhn check to its
  last four digits — including card numbers that appear inside a free-text error
  message.
* **No cardholder data is stored.** No PAN, no CVV, no track data. A card — whether
  typed on the payment form or read from application settings — exists in memory for
  the length of one request and is never written against a transaction.
* HPP flows redirect to the provider's own page. Direct API flows use tokenization
  where the provider offers it.

<a id="card-entry"></a>
### Card entry

`ALLOW_DIRECT_CARD_ENTRY` defaults to `false`, and with it off the API **rejects** a
request carrying card details rather than quietly ignoring it. A card number typed into
a web application is card data held by that application, which is a decision for whoever
runs the deployment rather than a default.

To turn it on — sandbox test cards only:

```bash
# on the VPS, in the deployment's .env
ALLOW_DIRECT_CARD_ENTRY=true

# the services are named backend and frontend; there is no "api" service
docker compose up -d --build backend frontend
docker compose exec backend printenv ALLOW_DIRECT_CARD_ENTRY   # confirm it took
```

A **Card token ID** needs no such permission and is offered either way: a token is not
card data, and it is the only way to run a Direct payment on an account that cannot
receive raw card numbers. Card details are also refused outright on a hosted-checkout
request, where the provider's own page collects them — accepting one there would mean
receiving card data for no reason at all.
* Passwords are hashed with bcrypt. Sessions are short-lived HS256 JWTs carrying only
  a user id, email and role.
* `.env` is git-ignored. No TLS private key or certificate is stored in this
  repository — those belong to Traefik on the VPS.

### Test mode

`ALLOW_PRODUCTION_GATEWAYS` defaults to `false`. With it off, any attempt to start a
transaction against a production gateway environment is refused by the benchmark
engine — not merely hidden in the UI. A **TEST MODE** banner is visible on every page,
and turns red if production access is ever enabled.

---

## Benchmark methodology

**All benchmarks originate from the same server.** Network location and infrastructure
stay constant across gateways, so the differences that show up are the gateways'.

**Comparison tests run sequentially, not in parallel.** Measuring six gateways at once
from one host means they compete for the same CPU, network interface and connection
pool, and that contention shows up as gateway latency. Serial execution costs
wall-clock time and buys comparability.

**Automated runs are rate limited.** One transaction every two seconds by default,
with a maximum per run. Both are configurable by an administrator, and both are
enforced in the benchmark engine rather than suggested in the UI. Requesting more than
the maximum clamps the run and records both what was asked for and what was applied.

**Failed transactions are excluded from latency, counted in the success rate.** A run
that died on call two of four has a duration that is not comparable with one that
finished; averaging them together would flatter whichever gateway fails fastest.

**Automated HPP runs measure session creation only.** Completing a hosted payment needs
a person on the gateway's page. Those transactions stay pending and the run says so,
rather than reporting a hosted-checkout figure nobody actually completed.

**Cold / warm / mixed labels describe our own methodology**, not the gateway's internal
state, which is not observable from outside.

**Every run records its environment** — application version, hostname, region, Python
version, and the applied configuration — so results from different dates or hosts can
be told apart before they are compared.

---

## Metric definitions

| Metric | Definition |
| ------ | ---------- |
| `duration_ms` | One HTTP call: from immediately before the request to the response body being fully read. Monotonic clock. |
| `gateway_api_time_ms` | Sum of the **timed** calls for a transaction. Excludes setup calls, redirects, 3DS waiting and customer time. |
| `three_ds_time_ms` | Between 3DS initiation and completion. Issuer and customer, not the gateway's API. |
| `customer_interaction_time_ms` | Time the customer spent on the gateway's hosted page. |
| `redirect_time_ms` | Redirect initiation to hosted page loaded, where the browser could report it. |
| `page_load_time_ms` | Hosted page load, from the browser's Performance API. Absent when cross-origin rules withhold it. |
| `total_duration_ms` | Benchmark start to final state. Includes everything above. |
| `webhook_latency_ms` | Transaction start to the gateway's webhook arriving. Stamped before any parsing. |
| `app_overhead_ms` | Total elapsed minus time spent inside gateway calls — this platform's own cost, tracked so it can never inflate a gateway's number. |
| `api_call_count` | Timed calls only. Compared against the vendor's documented minimum. |
| P50 / P90 / P95 / P99 | Nearest-rank percentiles over completed transactions. Never interpolated. |
| Success rate | `SUCCESS` as a percentage of all transactions. A decline is a failure to complete, even though the gateway behaved correctly. |
| Internal Benchmark Score | A weighted roll-up defined by this platform. **Not an industry standard.** Relative to the best value in the same comparison set, and only produced with enough samples. |

Formatting is consistent everywhere: `125 ms`, `1.25 sec`, `99.97%`. A metric that
could not be measured renders as `—`, never as `0`.

---

## Project layout

```
burapay/
├── backend/
│   ├── app/
│   │   ├── api/           routes: auth, gateways, transactions, benchmarks,
│   │   │                  comparison, reports, settings, webhooks
│   │   ├── benchmarks/    transaction timeline, Internal Benchmark Score
│   │   ├── core/          config, crypto, security, logging, sanitizer,
│   │   │                  error normalization, statistics
│   │   ├── db/            engine, session, declarative base
│   │   ├── gateways/      instrumented HTTP client + one file per gateway
│   │   ├── models/        SQLAlchemy models and shared enums
│   │   ├── schemas/       Pydantic request and response models
│   │   ├── services/      credentials, benchmark engine, runner, analytics,
│   │   │                  export, health
│   │   └── main.py
│   ├── migrations/        Alembic
│   ├── tests/
│   └── Dockerfile
│
├── frontend/
│   ├── src/
│   │   ├── api/           typed client and response types
│   │   ├── components/    layout, charts, shared UI
│   │   ├── lib/           formatting rules
│   │   └── pages/         dashboard, run, transactions, comparison,
│   │                      reports, gateways, settings
│   └── Dockerfile
│
├── docs/                  gateway research: sandbox signup, API flow comparison
├── scripts/               Traefik inspection helper, workbook generator
├── results/               the documentation-based comparison workbook
├── docker-compose.yml
├── .env.example
└── README.md
```

### API surface

```
/api/health
/api/docs                        OpenAPI documentation
/api/v1/auth
/api/v1/gateways
/api/v1/transactions
/api/v1/benchmarks
/api/v1/comparison
/api/v1/reports
/api/v1/settings
/api/webhooks/{gateway}          geidea, stripe, adyen, checkout, hyperpay, moyasar
```

---

## Background research

The `docs/` directory holds the research the adapters were built from — where to get
each sandbox, what each provider's documented call sequence is, and which claims could
not be confirmed on the vendor's own domain. `scripts/build_workbook.py` renders that
into a spreadsheet, and can fold measured results from a BuraPay JSON export back into
the same workbook so documented and measured figures sit side by side.
