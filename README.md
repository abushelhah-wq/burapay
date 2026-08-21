# BuraPay — payment gateway performance benchmarking

Measures how many API round trips each payment gateway needs to complete the
same operation, and how long each one takes, against real sandbox APIs. Every
claim it makes is backed by stored rows: a transaction's full ordered list of
HTTP calls is in the database, and the dashboard's figures are computed from
those rows at request time.

Geidea is the reference implementation. Adding a gateway means writing one
adapter module and adding one line to a registry.

---

## Documentation status

`docs.geidea.net` is unreachable from the build environment — the egress proxy
refuses the domain — so pages cannot be fetched during a build. Four were
fetched externally and are committed under [`docs/geidea/`](docs/geidea/), which
makes every claim below auditable rather than asserted:

| Page | Covers | Status |
|---|---|---|
| `01-tokenization.md` | tokenization / CIT, session shapes | implemented |
| `02-merchant-initiated-mit.md` | MIT flow + its exact signature algorithm | implemented |
| `03-standalone-save-card.md` | the separate save-card session endpoint | implemented |
| `04-webhook-callback-notifications.md` | callback signature, lifecycle edges | implemented |

Everything those pages cover is now built and tested. What they do not cover is
still refused rather than guessed — the rule did not expire when the first
batch arrived.

### Four signature algorithms, not one

The single most dangerous thing about this integration: Geidea uses **four
textually distinct signature schemes**, with different field lists and
different concatenation orders. Pattern-matching one onto another produces a
signature Geidea rejects cleanly, which presents as "the feature doesn't work"
rather than as a signing bug.

| Scheme | Signed string | Status |
|---|---|---|
| base / HPP session | *not published in the fetched pages* | **inferred** |
| MIT `pay/token` | `{publicKey}{sessionId}{timeStamp}` | verified |
| save-card session | `{timeStamp}{publicKey}{currency}` | verified |
| callback verification | `{publicKey}{amount}{currency}{orderId}{status}{merchantRef}{timeStamp}` | verified |

The MIT and callback implementations are checked byte-for-byte against the
reference implementations the docs themselves ship
(`test_signing_matches_the_documented_reference_implementations`).

### Still outstanding

- **The base/HPP session signature.** All four fetched pages defer to
  `geidea-checkout-v2#signature` without reproducing it, so the implemented
  concatenation is a reconstruction. It is shipped because a wrong signature
  produces a clean, fully logged rejection — it cannot move money to the wrong
  place — and because both the field order and the timestamp format are
  overridable from gateway config without a redeploy. It is reported in the UI
  as an advisory gap, not a blocking one.
- **`geidea-checkout-v2.md`** also carries the GeideaCheckout SDK's script URL.
  Geidea's hosted page opens from a `session_id` via that SDK rather than by
  redirecting to a URL, so hosted checkout, CIT and save-card all return a
  session id and say what to do with it instead of embedding a script URL
  nobody has verified.
- **`cancel-order-1.md`** — Cancel Order remains unimplemented and absent from
  the capability set.
- **`test-cards.md`** — the card-entry helper serves an empty list and explains
  why, rather than offering a remembered card number.
- **`api-response-codes-and-messages.md`** — only `000` is known, so an
  unrecognised code is reported verbatim rather than translated into a category
  it may not belong to.

Fetch any of these as `<url>.md`, commit under `docs/geidea/`, and update the
matching `Endpoint` in
[`backend/app/gateways/geidea/endpoints.py`](backend/app/gateways/geidea/endpoints.py).
Nothing else changes.

**No flow has been run against the Geidea sandbox.** That host is blocked from
this environment too, and no credentials were available. What *is* verified is
described under [Tests](#tests).

## Architecture

```
                        ┌──────────────────────────────────────┐
   browser ─── 443 ───▶ │  caddy   (or Traefik, with override) │
                        │  TLS, routing                        │
                        └───────┬───────────────────┬──────────┘
                    /api, /health│                   │ everything else
                                 ▼                   ▼
                     ┌────────────────────┐  ┌──────────────────┐
                     │  app  (FastAPI)    │  │ frontend (React) │
                     │  async, JSON only  │  │ static bundle    │
                     └──┬────────┬────────┘  └──────────────────┘
                        │        │
              ┌─────────┘        └──────────┐
              ▼                             ▼
      ┌───────────────┐            ┌─────────────────┐
      │ db (Postgres) │            │ redis  ◀── worker (RQ)
      │  transactions │            │ queued benchmark runs
      │  api_call_logs│            └─────────────────┘
      │  gateway_credentials (Fernet ciphertext)
      └───────────────┘
                        ▲
                        │ every outbound call, without exception
              ┌─────────┴───────────────────────────────┐
              │  app/gateways/http_client.py            │
              │  authenticate → log request → send with │
              │  timeout → log response/failure → return│
              └─────────┬───────────────────────────────┘
                        ▼
                  Geidea sandbox
```

### The rules the structure exists to enforce

**One HTTP chokepoint.** `app/gateways/http_client.py` is the only module
permitted to import `httpx`. It authenticates the request, writes the
`api_call_logs` row *before* sending, sends with an explicit timeout, and
completes the row after the response, the timeout, or the failure. There is no
path through it that sends a request and leaves no row.

The log row commits on its own connection, so the audit trail survives the
business transaction rolling back. If they shared a session, a handler that
raised would roll back its own logs — and a failed operation, the one worth
investigating, would be the one case with no evidence.

`backend/tests/test_security_invariants.py` scans the source tree and fails if
any other module imports an HTTP library.

**Money is integer minor units, end to end.** No `Numeric` column, no `float`,
no decimal amount in any request model. `app/money.py` carries an explicit ISO
4217 exponent table and *raises* on an unknown currency rather than assuming two
decimals — that assumption charges a three-decimal currency (KWD, BHD, OMR, JOD,
TND) a thousand times wrong. Conversion to a display string happens once, at the
presentation edge.

**Idempotency is a database constraint.** `UNIQUE (gateway_id,
idempotency_key)`. The service layer's check-then-insert is an optimisation on
top of it; the constraint is the guarantee, and the code handles the resulting
`IntegrityError` by returning the winning row. A test races three concurrent
requests on one key and asserts exactly one transaction exists.

**Card data never reaches storage.** PAN and CVV are destroyed by the shared
client before anything is written. Redaction is key-based *and* value-based: a
Luhn-valid, PAN-shaped value is redacted even under an unexpected key, and PANs
embedded in free-text error bodies are caught too. Only what is permitted is
kept — brand, last four, expiry month and year, and the gateway's token
reference. A test runs a real sale with a real PAN and then greps every column
of every table for it.

**Queued jobs carry no card data.** A benchmark run needs a card to charge, but
a queued job is persisted by Redis and RQ writes a `repr` of the call into the
worker log on every state change — so a plaintext card in a job argument would
be card data both on disk and in the application log. The payload is sealed with
the same `ENCRYPTION_KEY` used for stored credentials and opened only inside the
worker. (Both leaks were real and are fixed; `test_queued_card_payload_is_sealed`
is what keeps them fixed.)

**`request_count` is derived, never asserted.** It is `len(result.calls)` — the
calls the shared client actually made. It is also incremented live as each call
completes, which is what keeps the number honest for an operation that crashed
halfway.

**`PUBLIC_BASE_URL` is the only source of a domain.** Every return and callback
URL is built from it in the service layer, so an adapter is never in a position
to hardcode one. A test greps the source tree to confirm no deployment domain
appears anywhere.

### Layout

```
backend/
├── app/
│   ├── config.py            settings; the safety gates are read here
│   ├── money.py             integer minor units + ISO 4217 exponents
│   ├── redaction.py         PAN/CVV destruction, secret masking
│   ├── models.py            the §2 schema, plus credentials/users/runs
│   ├── gateways/
│   │   ├── http_client.py   THE shared client — the only httpx importer
│   │   ├── base.py          GatewayAdapter, Capability, GatewayContext
│   │   ├── errors.py        typed failures, incl. DocumentationRequiredError
│   │   ├── registry.py      code -> adapter class (the one line a gateway adds)
│   │   └── geidea/          adapter.py, endpoints.py (provenance), signing.py
│   ├── security/            crypto, CredentialStore, auth, gates
│   ├── services/            execution, webhooks, analytics, benchmark, bootstrap
│   ├── routers/             the REST API
│   └── workers/             RQ queue + the benchmark job
├── alembic/versions/        migrations, run automatically on container start
└── tests/                   65 tests, real Postgres + a real mock gateway
frontend/                    React + Vite, its own container, REST only
deploy/Caddyfile             public reverse proxy (the default)
docker-compose.yml           the whole stack
docker-compose.traefik-network.yml   override for a host already running Traefik
scripts/inspect-traefik.sh   run this ON THE VPS before using the override
docs/geidea/                 the Geidea pages this adapter was built from
docs/                        earlier cross-gateway documentation research
legacy/                      the previous release — see legacy/README.md
```

---

## Deploying

The backend service is named **`app`** (not `backend`), and the stack is:
`db`, `redis`, `app`, `worker`, `frontend`, plus an optional `caddy`.

```bash
git pull
docker compose build
docker compose up -d          # db, redis, app, worker, frontend
```

Caddy is behind a profile, so it never competes for ports 80/443 on a host that
already terminates TLS:

```bash
docker compose --profile caddy up -d    # add the bundled Caddy + Let's Encrypt
```

Migrations run automatically when the `app` container starts, so there is no
manual database step. To run them yourself instead:

```bash
docker compose exec app alembic upgrade head
```

### First run

```bash
cp .env.example .env
# Generate the two required keys:
python3 -c "import secrets; print('APP_SECRET_KEY=' + secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
# Then set POSTGRES_PASSWORD, DOMAIN, ACME_EMAIL, PUBLIC_BASE_URL and
# BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD.
docker compose build && docker compose up -d
```

The bootstrap admin is created only while the `users` table is empty. After that
the variables are inert — changing the password there does nothing, by design.

**Back up `ENCRYPTION_KEY`.** Losing it makes every stored gateway credential
unreadable; they would have to be re-entered.

### What changed from the previous release

Called out rather than done silently:

| Change | Why |
|---|---|
| `docker-compose.yml` moved from `deploy/` to the repository root | The required sequence is `docker compose build && docker compose up -d` with no `-f`, which only finds a compose file in the working directory. |
| New services `db`, `redis`, `worker`, `frontend` | Postgres and a real queue are requirements; the frontend must be its own service. All are containers — nothing new is installed on the host. |
| `app-data` volume no longer used | It held the SQLite file. All state is in Postgres now. The volume is left in place rather than deleted; remove it yourself once you are sure you do not want the old `results.db`. |
| Service names `app` and `caddy`, and the `caddy-data` / `caddy-config` volumes | **Unchanged.** Your existing Let's Encrypt certificates are in `caddy-data` and survive the upgrade. |

### Deploying behind Traefik

The default stack runs its own Caddy. For a host that already runs Traefik,
**first run the inspection script on that host**:

```bash
./scripts/inspect-traefik.sh
```

It reports the entrypoint, certificate resolver and network that host actually
uses, and prints the labels existing proxied apps use. Copy those values into
`.env`:

```
TRAEFIK_NETWORK=<the shared network name>
TRAEFIK_ENTRYPOINT=<the https entrypoint, often 'websecure'>
TRAEFIK_CERT_RESOLVER=<the resolver, often 'letsencrypt'>
```

Then deploy with the override:

```bash
docker compose -f docker-compose.yml -f docker-compose.traefik-network.yml up -d
```

Do not guess these values. An entrypoint or resolver name that does not exist
produces a router Traefik silently ignores — which looks exactly like the
application being down.

The override adds these labels (routing identical to the Caddyfile: `/api` and
`/health` to the backend, everything else to the frontend; the API router
carries the higher priority so it is not swallowed by the catch-all):

```yaml
app:
  traefik.enable: "true"
  traefik.docker.network: "${TRAEFIK_NETWORK}"
  traefik.http.routers.burapay-api.rule: Host(`${DOMAIN}`) && (PathPrefix(`/api`) || Path(`/health`) || Path(`/healthz`))
  traefik.http.routers.burapay-api.entrypoints: "${TRAEFIK_ENTRYPOINT}"
  traefik.http.routers.burapay-api.tls.certresolver: "${TRAEFIK_CERT_RESOLVER}"
  traefik.http.routers.burapay-api.priority: "100"
  traefik.http.services.burapay-api.loadbalancer.server.port: "8000"

frontend:
  traefik.http.routers.burapay-web.rule: Host(`${DOMAIN}`)
  traefik.http.routers.burapay-web.priority: "1"
  traefik.http.services.burapay-web.loadbalancer.server.port: "8080"
```

It also scales the bundled `caddy` service to zero, since Traefik owns 80/443
and two things cannot bind the same ports. `db`, `redis` and `worker` stay off
the Traefik network deliberately — nothing outside the stack should reach them.

---

## Adding a gateway adapter

Two steps. Nothing else in the codebase changes.

**1. Write `backend/app/gateways/<name>/adapter.py`:**

```python
from app.gateways.base import Capability, GatewayAdapter
from app.gateways.results import PaymentResult
from app.models import IntegrationTypeCode, TransactionStatus

class AcmeAdapter(GatewayAdapter):
    code = "acme"
    display_name = "Acme Payments"
    required_credentials = ("api_key",)

    # Declare only what you implement. Anything absent raises
    # UnsupportedOperationError and is hidden in the UI rather than offered
    # as a button that cannot work.
    capabilities = frozenset({
        Capability.DIRECT_API_SALE,
        Capability.REFUND,
        Capability.QUERY_ORDER,
    })
    supported_integration_types = (IntegrationTypeCode.DIRECT_API,)

    # §8 requires the doc page used to be linked from the code.
    doc_urls = {"direct_api_sale": "https://docs.acme.example/payments"}

    def auth(self):
        creds = self.require_credentials("api_key")
        return BearerAuth(creds["api_key"])          # your GatewayAuth

    async def direct_api_sale(self, order, card):
        self.guard_production()                       # ALLOW_PRODUCTION_GATEWAYS
        async with self.client() as client:           # THE shared client
            call = await client.call(
                step_name="pay", method="POST",
                url=f"{self.base_url}/payments",
                json_body={...},
                raise_on_status=False,
            )
            return PaymentResult(
                calls=client.calls,                   # request_count derives from this
                status=TransactionStatus.SUCCESS,
                gateway_order_id=...,
                card=card.safe_details(),
            )
```

Rules the base class and the client enforce for you: credentials resolve at call
time through `CredentialStore` (never `os.environ`), the production gate is one
`guard_production()` call, every request is logged and redacted, and
`request_count` comes from `len(calls)` so it cannot drift.

Build URLs only from `order.return_url` / `order.callback_url`, which the
service layer already resolved from `PUBLIC_BASE_URL`.

**2. Register it:**

```python
# backend/app/gateways/registry.py
ADAPTERS = {
    GeideaAdapter.code: GeideaAdapter,
    AcmeAdapter.code: AcmeAdapter,     # the one line
}
```

The gateway row, its integration types, and the Settings page fields appear
automatically on next start. If a request shape cannot be verified against the
gateway's documentation, raise `DocumentationRequiredError` with the page URL
instead of guessing — that is what puts it in the UI's Documentation status
panel rather than into a live payment request.

Five more gateways (Stripe, Adyen, Checkout.com, HyperPay, Moyasar) have working
synchronous implementations in [`legacy/app/adapters/`](legacy/README.md) that
have not yet been ported to this interface.

---

## Tests

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# Postgres must be reachable. Create the test database once:
createdb burapay_test

DATABASE_URL=postgresql+asyncpg://burapay:burapay@127.0.0.1:5432/burapay_test \
  .venv/bin/python -m pytest -q
```

**101 tests.** Two choices about fidelity are deliberate:

- They run against **real Postgres**, not SQLite. The idempotency guarantee is a
  UNIQUE constraint and the audit log depends on a second connection seeing
  committed rows — neither is meaningfully exercised by SQLite, and testing a
  different engine from the deployed one would make the results worth less than
  they look.
- The mock gateway is a **real HTTP server on a real port**, not a patched
  transport. The shared client builds its own `httpx.AsyncClient`, so injecting
  a transport would bypass the code under test: connection handling, status
  codes, and actual timeout behaviour.

Every flow test asserts **three** things — the final status, the
`request_count`, *and* the exact ordered `api_call_logs` sequence. Asserting
status alone would let a regression that doubles the number of round trips pass
silently, which is the worst kind of regression this codebase can have.

Covered: direct sale (4 calls), frictionless sale (3), decline vs. error,
pre-auth, capture (full, partial, multiple partial, over-capture refused),
refund (full, partial, refused on an uncaptured auth), void (and refused after
capture), timeout with and without successful reconciliation, network failure,
non-2xx, unparseable body, concurrent idempotency, both safety gates, benchmark
limits, credential encryption at rest, sealed queue payloads, the
no-direct-`httpx` and
no-hardcoded-domain source rules, dashboard arithmetic, and the HTTP layer.

**What the tests do not prove:** that Geidea accepts these requests. The mock
was written from the same material the adapter was, so agreement between them
says nothing about the real API. Only a live sandbox run settles that, and it
has not been possible here — see [the top of this README](#read-this-first-documentation-access).

### Live sandbox verification, when you can run it

On a host that can reach `docs.geidea.net` and the Geidea sandbox:

1. Fetch the doc pages and update `endpoints.py` provenance.
2. Enter sandbox credentials on the Settings page and press **Test connection** —
   one harmless read-only call that creates nothing in your sandbox.
3. Run one of each flow from **New transaction** and check the call trail on the
   transaction detail page against Geidea's merchant dashboard.
4. The first thing to suspect on a signature rejection is the signature field
   order or timestamp format — both are gateway config, editable without a
   redeploy.

---

## What gets measured

**Round trips are the headline.** `request_count` per operation is what the
benchmark compares: a Direct API sale that needs four calls costs four network
latencies, and that is a structural property of the gateway rather than of the
network.

**A decline is not an error.** `FAILED` means the gateway completed the round
trip and said no; `ERROR` and `TIMEOUT` mean it did not answer. The dashboard
counts them separately, because a gateway that declines quickly is not the same
as one that is broken, and averaging them hides exactly the difference worth
measuring.

**Percentiles are nearest-rank, never interpolated.** Every figure shown is a
latency that actually occurred. Sample sizes are shown next to them, and cells
with fewer than 20 timed runs are flagged — a p95 over four samples is not a p95.

**Pending transactions are excluded from latency and from the success rate.** A
hosted-checkout session that nobody has paid yet has not succeeded or failed;
counting it either way would be wrong.

**Where you run it matters more than anything it measures.** Set
`MEASUREMENT_LOCATION` — the dashboard says so explicitly when it is unset.

---

## Health

`GET /health` checks database connectivity and **nothing else**. It deliberately
makes no gateway call: a probe running every 30 seconds against a sandbox would
burn API quota and fill the merchant dashboard with traffic that looks real.
Gateway reachability is checked on demand, by the Settings page's test-connection
action, which a human triggers.
