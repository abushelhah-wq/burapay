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
              ┌─────────────────┐
              │  nginx (TLS)    │  busrapay.com
              └────────┬────────┘
                  ┌────┴─────┐
                  ▼          ▼
          ┌────────────┐  ┌──────────────┐
          │  frontend  │  │   backend    │  /api/
          │ React+Vite │  │   FastAPI    │
          └────────────┘  └──────┬───────┘
                                 ▼
                          ┌─────────────┐
                          │ PostgreSQL  │
                          └─────────────┘
```

| Layer | Technology |
| ----- | ---------- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, httpx |
| Database | PostgreSQL 16 |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, Recharts |
| Proxy | nginx, with Let's Encrypt via certbot |

---

## Quick start with Docker

```bash
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

Set `POSTGRES_PASSWORD`, `BOOTSTRAP_ADMIN_PASSWORD` and `DOMAIN`, then:

```bash
docker compose build
docker compose up -d
docker compose ps
```

TLS needs one manual step for the first certificate — see [`nginx/README.md`](nginx/README.md).
Once it is issued:

```bash
curl https://busrapay.com/api/health
# {"status":"healthy","database":"connected","version":"1.0.0",...}
```

Sign in at `https://busrapay.com` with `BOOTSTRAP_ADMIN_EMAIL` and
`BOOTSTRAP_ADMIN_PASSWORD`, then change the password immediately.

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
| Geidea | Payment session → hosted page | Session → initiate → authenticate payer → pay | HPP 2, Direct 4 (fixed) |
| Stripe | Checkout Session | PaymentIntent create+confirm | HPP 1 + webhook, Direct 1–2 |
| Adyen | Sessions flow (Drop-in) | Advanced flow `/payments` | HPP 1 + webhook, Direct 2–3 |
| Checkout.com | Hosted payment link | `/payments` with a token | HPP 2, Direct 1–2 |
| HyperPay | Copy&Pay widget | `/v1/payments` | HPP 2, Direct 1–2 |
| Moyasar | Invoice | Token or raw-card `/v1/payments` | HPP 2, Direct 2–3 |
| MockPay | Simulated | Simulated | n/a — a simulator |

The documented figure sits next to the measured one everywhere, so a gateway that
needs more calls than its documentation claims is visible immediately. That divergence
is one of the more useful things this platform produces.

Where a detail could not be confirmed from a vendor's public documentation, the
adapter says so in its notes and makes the detail configurable rather than guessing
silently. Three such cases are live today: Geidea's signature field order and
timestamp format, HyperPay's back-office URL shape for refunds, and Moyasar's
contradictory guidance on raw-card versus tokenize-first Direct payments.

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
cd backend && pytest -q           # 135 tests
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

## Production deployment

```bash
git pull
docker compose build
docker compose up -d
docker compose ps
curl https://busrapay.com/api/health
```

Migrations run in the backend entrypoint before the server starts.

The compose file publishes ports only on nginx. The backend and PostgreSQL are
reachable on the internal Docker network alone, so there is no path to the API or the
database that does not pass through the proxy.

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
* **No cardholder data is stored.** No PAN, no CVV, no track data. The sandbox test
  card lives in application settings, exists in memory for the length of one request,
  and is never written against a transaction.
* HPP flows redirect to the provider's own page. Direct API flows use tokenization
  where the provider offers it.
* Passwords are hashed with bcrypt. Sessions are short-lived HS256 JWTs carrying only
  a user id, email and role.
* `.env` is git-ignored, and so is `nginx/certs/`.

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
├── nginx/                 reverse proxy template and TLS notes
├── docs/                  gateway research: sandbox signup, API flow comparison
├── scripts/               documentation workbook generator
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
