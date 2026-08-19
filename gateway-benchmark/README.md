# Payment Gateway API Response-Time Benchmark

A real, measurement-based benchmark comparing **Geidea** against **Adyen, Stripe, Checkout.com, HyperPay and Moyasar** across three API surfaces:

1. **Hosted Checkout** — redirect/hosted payment page flow
2. **Direct API (server-to-server)** — merchant collects/tokenizes card data and calls the payment API directly
3. **Stand-alone APIs** — Refund, Capture, Void, and MIT (merchant-initiated/recurring transactions)

For each flow it measures two things: **how many API calls** the merchant server must make end to end, and **how long each call takes** (min/mean/median/p95/max over repeated runs, so the numbers are statistically meaningful rather than anecdotal).

---

## Status: ready to run, waiting on credentials

The documentation research, the measurement harness, all six gateway modules, and the reporting pipeline are complete and tested. What is missing is the one thing that cannot be produced from a documentation review: **live authenticated calls against each gateway's sandbox**. API keys cannot be self-provisioned on your behalf.

Everything that can be done ahead of that is done:

- Documentation-based mapping of every gateway's equivalent API calls — [`docs/02_api_flow_comparison.md`](docs/02_api_flow_comparison.md)
- A deep dive on the gateway that diverges most from Geidea — [`docs/03_stripe_flow_mapping.md`](docs/03_stripe_flow_mapping.md)
- A sandbox signup guide for all six — [`docs/01_sandbox_signup_guide.md`](docs/01_sandbox_signup_guide.md)
- Ready-to-run timing scripts for each gateway — [`scripts/`](scripts/)
- A test suite that drives all six modules end to end against a local mock, so you know the toolchain works before spending a sandbox credential on it
- An Excel workbook pre-populated with the documentation-based comparison, with timing tabs ready to receive real data — [`results/gateway_benchmark_workbook.xlsx`](results/gateway_benchmark_workbook.xlsx)

## How to finish it

1. **Get sandbox credentials.** Read [`docs/01_sandbox_signup_guide.md`](docs/01_sandbox_signup_guide.md). Start with **Geidea** (re-confirm sandbox access) and **HyperPay** (contact sales/merchant support) — neither is self-serve and both take human turnaround time. Stripe and Moyasar are the fastest path to first real numbers.

2. **Install and verify the toolchain** — this needs no credentials at all:

   ```bash
   cd scripts
   python3 -m pip install -r requirements.txt --break-system-packages
   python demo.py
   ```

   `demo.py` runs every gateway module against a local mock and builds a workbook from the results. If it finishes cleanly, everything except the credentials is working.

3. **Fill in credentials.**

   ```bash
   cp .env.example .env      # then edit; .env is gitignored
   python run_all.py --list  # shows which gateways are configured
   ```

4. **Run it from your own MENA-region infrastructure** — not from a laptop on another continent, and not from a cloud session in an arbitrary region. Network latency dominates every other difference this benchmark measures. Set `MEASUREMENT_LOCATION` in `.env` so the output records where the numbers came from.

   ```bash
   python run_all.py
   python build_workbook.py
   ```

5. **Share `results/*.csv` and `results/latest.json`** (or the console output) and the real numbers can be folded into the written comparison.

You do not need all six gateways to start. Anything without credentials is reported as "not configured" and skipped — it never fails the run. Start with two and add the rest as they arrive.

## Structure

```
gateway-benchmark/
├── README.md                              (this file)
├── docs/
│   ├── 01_sandbox_signup_guide.md          how to get test credentials for each gateway
│   ├── 02_api_flow_comparison.md           documentation-based call-sequence mapping
│   └── 03_stripe_flow_mapping.md           Stripe deep dive (largest divergence from Geidea)
├── scripts/
│   ├── harness.py                          timing/measurement framework
│   ├── geidea.py                           baseline; 4-call Direct API sequence
│   ├── adyen.py, stripe_gw.py, checkout_com.py, moyasar.py, hyperpay.py
│   ├── reference_data.py                   the documentation research, as data
│   ├── run_all.py                          runs every configured gateway, prints comparisons
│   ├── build_workbook.py                   writes results/gateway_benchmark_workbook.xlsx
│   ├── demo.py                             full pipeline against a local mock, no credentials
│   ├── .env.example                        credential template
│   ├── requirements.txt
│   └── tests/
│       ├── mock_gateway.py                 local stand-in for all six gateways
│       └── test_benchmark.py               32 tests: stats, accounting, every gateway flow
└── results/
    └── gateway_benchmark_workbook.xlsx     documentation comparison + tabs for real data
```

## Command reference

```bash
cd scripts

python run_all.py                            # every configured gateway, every flow
python run_all.py --gateway geidea stripe    # a subset
python run_all.py --flow direct_api          # a subset of flows
python run_all.py --runs 5 --warmups 1       # quick shakedown before a full batch
python run_all.py --verbose                  # per-call timings as they happen
python run_all.py --list                     # configuration status, then exit

python build_workbook.py                     # folds results/latest.json into the workbook
python build_workbook.py --docs-only         # documentation-only workbook

python demo.py                               # whole pipeline against the mock
python -m unittest discover -s tests -v      # the test suite
```

Flow names: `hosted_checkout`, `direct_api`, `capture`, `refund`, `void`, `mit`.
Gateway names: `geidea`, `adyen`, `stripe`, `checkout_com`, `hyperpay`, `moyasar`.

## Methodology

**What counts as an API call.** Only outbound merchant-server-to-gateway calls. Inbound webhooks are noted but not counted — the merchant does not initiate them, so they are not a round trip it can time or retry. The customer's browser round trip to a hosted page is likewise excluded: the merchant's backend does not control it, and it is not what "response time" complaints are usually about.

**Timed vs. prep calls.** Setup a real merchant would already have done — creating a payment so there is something to refund, minting a token so there is something to charge — is issued as an untimed *prep* call and excluded from both the call count and the timing statistics. Only the operation being measured is timed. This is why the Refund flow reports 1 call even though three HTTP requests go out.

**Repetition.** Each flow runs 30 times by default (`RUNS_PER_CALL`) with 2 warmup runs discarded (`WARMUP_RUNS`). Warmups absorb TCP/TLS setup; connection reuse is left on afterwards, because a real merchant server keeps keep-alive connections to its gateway.

**Percentiles are nearest-rank, never interpolated.** With 30 samples, an interpolated p95 invents a number sitting between two real observations. Every figure reported is a latency that was actually measured.

**3DS.** Where a challenge would add a round trip in real (non-test) card scenarios, it is noted per gateway but not forced in the default flow, since sandbox test cards typically bypass challenges. Gateways whose call count varies with the challenge outcome record the actual number of calls each run made, so the variation shows up in the data rather than being flattened.

**Async acknowledgements.** Checkout.com returns `202 Accepted` on capture/refund/void and Adyen returns `received`. Those timings are *time-to-ack*, not *time-to-settled* — the authoritative outcome arrives on a webhook. Comparing them against a gateway that answers synchronously undercounts them, and the report flags this per gateway.

**Interpretation.** Numbers vary by time of day, region and gateway-side load. Treat single runs skeptically and prefer the median and p95 from a full batch. Run all gateways at consistent times of day for a fair comparison.

## What the benchmark is trying to settle

The documentation research produced one dominant hypothesis, stated in full in [`docs/02_api_flow_comparison.md`](docs/02_api_flow_comparison.md):

> **Geidea's Direct API is the only one of the six with a fixed 4-call sequence** (Session → Initiate Authentication → Authenticate Payer → Pay) regardless of whether a 3DS challenge actually occurs. Every competitor documents a 1–2 call frictionless minimum, with the extra call conditional on a real challenge.

Whether that fixed sequence survives contact with a sandbox is exactly what a live run answers. `scripts/geidea.py` deliberately does not hard-code 4 calls: it reads the initiate response for a frictionless marker and records a 3-call run if it finds one. The workbook's **Documented vs Measured** tab is where the vendor's claim and the sandbox's behaviour meet.

The second hypothesis is about MIT: Geidea is the only gateway requiring a fresh session object on *every* recurring charge. Moyasar's documented "2 calls" turns out to be a one-time token mint plus a per-charge call — so its real per-charge cost is 1, which the scripts measure rather than inherit from the docs.

## Troubleshooting

### `ModuleNotFoundError: No module named 'dotenv'`

Step 2's `pip install` either was not run, or ran in a different Python environment than the one executing `run_all.py` — common with multiple Python installs, virtualenvs, or `python` and `python3` pointing at different interpreters.

```bash
cd scripts
python3 -m pip install -r requirements.txt --break-system-packages
# or, on a system that blocks --break-system-packages or manages Python strictly:
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_all.py
```

Confirm with `python3 -c "import dotenv"` — no output means it is installed. If the error persists, run `which python3` and `which pip` and make sure they point at the same environment.

### `SSLCertVerificationError` / `certificate verify failed` on `pip install`

`pip` cannot build a trusted certificate chain to PyPI — almost always because you are on a corporate network (very plausibly Geidea's own) whose firewall/proxy intercepts and re-signs HTTPS traffic with an internal root certificate. Your browser trusts that root because IT pushed it via Group Policy/MDM; Python's own certificate store does not know about it.

Try these in order and stop at the first that works:

1. **Ask IT/security for the corporate root CA certificate** (a `.crt`/`.pem`) and point pip at it. This is the correct long-term fix — it makes pip trust your network the same way your browser already does, without weakening anything:

   ```bash
   pip install -r requirements.txt --cert /path/to/corporate-root-ca.pem
   ```

2. **Switch networks** (off VPN, or a different WiFi) and retry. If it works elsewhere, that confirms it is your current network's interception rather than your machine.

3. **Point Python at your OS trust store**, which often already includes the corporate root if IT installed it system-wide:

   ```bash
   pip install --upgrade certifi
   export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   # Linux path
   pip install -r requirements.txt
   ```

4. **Last resort, and do not leave it in place** — this removes a real security check:

   ```bash
   pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
   ```

   Acceptable for a one-off install of well-known packages (`requests`, `python-dotenv`, `tabulate`, `openpyxl`), but not a habit: a proxy that re-signs your traffic can in principle see and modify everything passing through it, including which package versions you actually receive.

If none work, raise a ticket with IT/security — they will know whether your network expects a specific CA bundle or an internal PyPI mirror.

### A gateway fails every run

Run it alone with full detail:

```bash
python run_all.py --gateway <name> --runs 1 --verbose
```

The runner aborts a flow after 3 consecutive failures rather than burning 30 attempts, and the last error is printed and written to the summary CSV. Common causes: wrong API version (`ADYEN_API_VERSION`), a currency the sandbox does not support (`MOYASAR_CURRENCY` must be `SAR`), a missing NAS processing channel (`CHECKOUT_PROCESSING_CHANNEL_ID`), or — for Geidea — the signature format described in `scripts/geidea.py`'s module docstring.

### Credentials safety

`.env` is gitignored and no request body is ever written to the result files — only response excerpts on failures. Use sandbox keys only, and prefer a restricted key where the gateway offers one.
