# busrapay — payment gateway response-time benchmark

A web application that measures how many API calls each payment gateway needs and
how long each one takes. Pick a gateway and a flow, pay with a sandbox test card,
and every merchant-server call is timed and recorded.

Compares **Geidea** (the baseline) against **Adyen, Stripe, Checkout.com, HyperPay
and Moyasar** across six flows: hosted checkout, direct server-to-server API,
capture, refund, void, and MIT/recurring.

---

## Quickstart

```bash
python3 -m pip install -r requirements.txt --break-system-packages
cp .env.example .env                  # fill in at least one gateway's sandbox keys

./scripts/make_cert.sh localhost      # self-signed cert for local HTTPS
SSL_CERTFILE=certs/cert.pem SSL_KEYFILE=certs/key.pem \
  PUBLIC_BASE_URL=https://localhost:8443 python serve.py
```

Then open <https://localhost:8443/checkout> and click through the browser's
self-signed-certificate warning.

For a real deployment with a real certificate, see [Deploying](#deploying).

## Why HTTPS is not optional here

This is not a security checkbox — three things break without it:

1. **Hosted checkout hands the gateway a `return_url` and a `callback_url`.**
   Sandboxes reject non-HTTPS values for both. The app refuses to start a hosted
   flow when `PUBLIC_BASE_URL` is not `https://`, and says so, rather than letting
   the gateway return an opaque rejection you have to decode.
2. **Webhooks need a publicly reachable TLS endpoint.** A gateway cannot deliver to
   a host whose certificate it cannot verify — which rules out self-signed certs for
   anything beyond local UI work.
3. **Browsers will not carry a secure-context redirect back to a plaintext origin**,
   so the return leg silently fails.

The server-side flows (direct API, capture, refund, void, MIT) never redirect a
browser, so they work regardless. Only hosted checkout is gated.

A self-signed certificate is enough to click around the UI locally. It is **not**
enough for hosted checkout against a real sandbox — for that you need either a real
certificate or a tunnel (ngrok, cloudflared) that terminates TLS with a trusted one.

## What gets measured

**Only outbound merchant-server calls are counted.** Webhooks are acknowledged at
`/webhook/{gateway}` but never counted — the merchant neither initiates them nor
controls their delivery, so they are not a round trip it can time or retry.

**Timed vs. setup calls.** Refunding something requires a payment to refund;
charging a stored card requires a stored card. Those setup calls are issued but
marked *setup* and excluded from both the call count and the total, because a real
merchant would already have made them. The result table shows them greyed out, so
you can see exactly what was and was not counted.

**Hosted checkout is measured across both legs.** The clock covers the session
create and the server-side confirmation. It deliberately excludes the customer's
time on the gateway's payment page — the merchant does not control how fast someone
types a card number.

**Percentiles are nearest-rank, never interpolated.** Every figure on the results
page is a latency that actually occurred. With a handful of runs a p95 is not yet
meaningful; use `scripts/bench.py` for a real distribution.

**Where you run it matters more than anything else it measures.** Run it from the
same infrastructure your merchants transact from. Set `MEASUREMENT_LOCATION` in
`.env` so results record where they came from.

## The question this exists to answer

From the documentation research in [`docs/02_api_flow_comparison.md`](docs/02_api_flow_comparison.md):

> **Geidea's Direct API is the only one of the six with a fixed 4-call sequence**
> (session → initiate authentication → authenticate payer → pay), regardless of
> whether a 3DS challenge actually occurs. Every competitor documents a 1–2 call
> frictionless minimum, with the extra call conditional on a real challenge.

Whether that fixed sequence survives contact with a sandbox is the point of the
exercise. `app/adapters/geidea_adapter.py` deliberately does **not** hard-code four
calls: it reads the initiate response for a frictionless marker and completes in
three when it finds one. `GEIDEA_FORCE_FULL_3DS=1` forces the documented four.

The result panel shows the measured count next to the documented one, so a
divergence is visible immediately rather than needing to be dug for.

## Layout

```
app/
├── main.py                    FastAPI routes, HTTPS enforcement, HSTS
├── config.py                  settings; validates PUBLIC_BASE_URL is https
├── timing.py                  MeasuredSession — times every call, splits timed/prep
├── storage.py                 SQLite: one row per attempt, one per HTTP call
├── adapters/
│   ├── base.py                the adapter contract
│   ├── geidea_adapter.py      baseline — both flows, HMAC signing, region hosts
│   ├── stripe_adapter.py      the reference pattern
│   ├── adyen_adapter.py, checkout_adapter.py, hyperpay_adapter.py, moyasar_adapter.py
│   └── __init__.py            registry
├── templates/                 checkout, results, gateway handoff
└── static/style.css
scripts/
├── bench.py                   batch runner — N repetitions, real distributions
├── build_workbook.py          Excel export
├── reference_data.py          the documentation research, as data
└── make_cert.sh               self-signed cert for local HTTPS
deploy/                        docker-compose + Caddyfile (automatic real certs)
docs/                          sandbox signup guide, API flow comparison, Stripe deep dive
tests/                         33 tests: mock gateway, all six adapters, HTTP layer
```

`app/adapters/` is the single implementation of every gateway's call sequence. The
web UI and `scripts/bench.py` both drive it, so the two can never disagree about
what a flow is.

## Two ways to run a measurement

**The web UI** (`/checkout`) runs one attempt per click. That is the right shape for
"does this work, and how long did it take" — and the only way to measure hosted
checkout, whose second leg needs a human on the gateway's page.

**`scripts/bench.py`** repeats each flow N times and reports min/mean/median/p95/max.
One sample is an anecdote; a p95 needs a few dozen.

```bash
python scripts/bench.py --list                    # configuration status
python scripts/bench.py --gateway geidea stripe   # a subset
python scripts/bench.py --flow direct_api         # a subset of flows
python scripts/bench.py --runs 5 --warmups 1      # quick shakedown
python scripts/bench.py                           # 30 runs, every configured gateway
```

It excludes hosted checkout for the reason above. Results land in `results/` as JSON
and CSV; `python scripts/build_workbook.py` folds them into the Excel workbook.

## Deploying

`deploy/docker-compose.yml` runs the app behind Caddy, which obtains and renews a
real Let's Encrypt certificate automatically — no cert paths to configure, no
renewal cron to forget.

```bash
# 1. Point your domain's A/AAAA record at the host
# 2. Set DOMAIN, ACME_EMAIL and gateway credentials in .env
docker compose -f deploy/docker-compose.yml up -d
```

Ports 80 and 443 must be reachable from the internet: Caddy needs 80 for the ACME
challenge, and the gateways need 443 to deliver webhooks and return redirects.

The app container publishes no ports of its own — it is reachable only through
Caddy, so there is no plaintext path to it. It runs unprivileged and owns only its
data volume.

Behind any other reverse proxy, set `BEHIND_PROXY=1` so the app trusts
`X-Forwarded-Proto`. That header is client-settable, so it is ignored unless you
opt in — otherwise a caller could spoof the scheme past the HTTPS check.

## Credentials

Every gateway is optional. One with no credentials shows as *not configured* in the
UI and cannot be selected, rather than failing when you press Pay.

[`docs/01_sandbox_signup_guide.md`](docs/01_sandbox_signup_guide.md) covers getting
sandbox keys for each. Stripe and Moyasar are self-serve with keys visible
immediately; Geidea and HyperPay are not self-serve and need a human at the vendor,
so start those first.

`.env` is gitignored. Use sandbox keys only. No request body is ever written to the
database — only response excerpts on failures — so card data submitted to a gateway
never lands in storage or logs.

## Known unknowns

Carried from the documentation research, and worth knowing before you debug:

- **Geidea's signature format is inferred.** The documented concatenation is
  implemented (`publicKey + amount + currency + reference + timestamp`, HMAC-SHA256
  keyed on the API password, base64), but it could not be verified against a worked
  example. If the first call is rejected for a bad signature, that field order — or
  `GEIDEA_TIMESTAMP_FORMAT` — is the thing to change, not the adapter.
- **Geidea's hosted redirect field name varies.** The adapter checks several known
  spellings and, failing all of them, reports which fields the session response
  actually carried rather than throwing a `KeyError`.
- **HyperPay's back-office URL shape is unverified.** Both dedicated tutorial pages
  404'd during research. The default posts to `/v1/payments/{id}`; set
  `HYPERPAY_BACKOFFICE_PATH=nested` for Peach Payments' shape on the same engine.
- **Adyen's Sessions flow has no documented status endpoint.** Confirmation is
  webhook-only, so `confirm_hosted` makes no call and says so rather than inventing
  a GET that would misrepresent the flow.
- **Checkout.com's capture/refund/void return `202 Accepted`.** Those timings are
  time-to-ack, not time-to-settled.

The full list of 22 open questions is in
[`docs/02_api_flow_comparison.md`](docs/02_api_flow_comparison.md) and in the
workbook's Data Gaps tab.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt --break-system-packages
python3 -m unittest discover -s tests -v
```

33 tests. `tests/mock_gateway.py` stands in for all six gateways, so every adapter
flow — both legs of hosted checkout included — runs end to end with no credentials.
The tests assert the exact timed-call count per gateway and flow, so a changed
sequence fails loudly. They verify request construction, response parsing, call
sequencing, the timed/prep split, and the HTTPS policy. They do **not** verify that
a real gateway accepts these requests; only a live run does that.
