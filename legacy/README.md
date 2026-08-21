# Previous implementation (v1)

This directory holds the release that BuraPay v2 replaced. It is kept in the
tree, rather than only in git history, because it contains work that has not
been redone yet and that the port will need.

**Nothing here runs.** It is not built by any Dockerfile, not imported by the
backend, and not collected by the test suite.

## What is still valuable in here

`app/adapters/` holds working synchronous adapters for five gateways besides
Geidea — Stripe, Adyen, Checkout.com, HyperPay and Moyasar — each with its call
sequence, request construction and response parsing already worked out against
those gateways' documentation. Porting them to the async `GatewayAdapter`
interface is the natural next step, and this is the reference for doing it. See
"Adding a gateway adapter" in the root README.

`tests/mock_gateway.py` stands in for all six gateways and asserts the exact
timed-call count per gateway and flow.

`scripts/bench.py` is the batch runner: N repetitions per flow with
min/mean/median/p95/max. v2 replaces it with queued benchmark runs
(`POST /api/benchmark/{gateway}/runs`), but the CLI's reporting is a useful
model.

## Why v2 is a rewrite rather than an evolution

The v1 architecture could not meet the current requirements without being
replaced wholesale:

- **Synchronous `requests`**, with each adapter making its own calls. The
  requirement is that every outbound gateway call passes through one shared
  async client that logs the request before sending and the response after.
- **SQLite, with one row per attempt.** The requirement is Postgres with a full
  `api_call_logs` audit trail, JSONB payloads, and real concurrency.
- **Jinja templates served by FastAPI.** The requirement is a separate frontend
  service talking to a documented REST API.
- **Credentials in environment variables only.** The requirement is encrypted
  storage with a Settings page and a production-credential gate.

## What carried forward

The documentation research in `docs/` and `scripts/reference_data.py` is still
current and still at the repository root — it is the provenance behind
`backend/app/gateways/geidea/endpoints.py`, which records for every endpoint
whether its shape was verified, derived, inferred, or is entirely unknown.
