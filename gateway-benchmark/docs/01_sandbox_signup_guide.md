# Sandbox / Test Account Signup Guide

Before the timing scripts in `/scripts` can produce real numbers, you need live sandbox credentials for each gateway. This guide covers how to get them.

Self-serve gateways (Stripe, Adyen, Checkout.com, Moyasar) can be done in minutes. Relationship-gated gateways (Geidea, HyperPay) require contacting the provider directly — **start those first**, since they involve human turnaround time.

You do not need all six to start. Every gateway is independent: run what you have, add the rest as they arrive.

---

## 1. Geidea (baseline — you likely already have this)

- **Process:** No public self-serve signup. Credentials (Public Key + API Password) are issued by Geidea's enablement team by email during onboarding, or found in the merchant portal under **Payment Gateway → Gateway Settings**.
- **Action:** Contact your existing Geidea account/integration contact, or support, to confirm/obtain **sandbox (test)** credentials specifically — not production.
- **Docs:** https://docs.geidea.net/docs/pre-requisites
- **Set:** `GEIDEA_PUBLIC_KEY`, `GEIDEA_API_PASSWORD`, `GEIDEA_REGION` (`eg` | `ksa` | `uae`)

Two things to ask for while you have their attention, because both are unresolved in the public docs and both affect the benchmark:

1. A **test card that is not enrolled in 3DS**, so the Direct API flow can be run without a challenge. This is what settles whether the documented 4-call sequence can drop to 3 — the benchmark's headline question.
2. Confirmation of the **signature payload field order and timestamp format**. `scripts/geidea.py` implements the documented concatenation (`publicKey + amount + currency + merchantReferenceId + timestamp`, HMAC-SHA256 keyed on the API password, base64), but this could not be confirmed against a worked example. If the sandbox rejects your first call, this is the thing to check — override the timestamp format with `GEIDEA_TIMESTAMP_FORMAT` rather than editing the module.

For the MIT flow you also need a `tokenId` from an earlier session that carried `cardOnFile: true`, plus the `agreementId` you generated for it. Without `GEIDEA_TOKEN_ID` the MIT flow is skipped rather than guessed at.

## 2. Adyen

- **Process:** Self-serve. Sign up at https://www.adyen.com/get-started (or the "create a free test account" link on https://docs.adyen.com/get-started-with-adyen/) with a business email.
- **What you get:** Access to the **test Customer Area**. Generate an **API key** under Developers → API credentials, and note your **merchant account** name.
- **Docs:** https://docs.adyen.com/get-started-with-adyen/
- **Set:** `ADYEN_API_KEY`, `ADYEN_MERCHANT_ACCOUNT`, and confirm `ADYEN_API_VERSION` (the endpoints are versioned — `/v71/payments`, `/v72/...`; the dashboard shows which version your credential targets).

The MIT flow needs `ADYEN_STORED_PAYMENT_METHOD_ID`. Adyen delivers the card-on-file token asynchronously on the `recurring.token.created` webhook rather than in the store-card response, so it cannot be minted inline — make one store-card payment, capture the token from the webhook or the Customer Area, and paste it in. Until then the MIT flow is skipped.

### Troubleshooting: no confirmation/access email received

1. **Check spam/promotions/quarantine first** — Adyen's verification emails are commonly filtered by corporate mail servers. Search for sender domain `adyen.com`.
2. **Wait a little longer before resending** — test account approval is not always instant; some signups queue for a light manual review.
3. **Try a business email domain** if you used a personal address (Gmail etc.) — business signups process faster and more reliably.
4. **If anyone on your team already has Customer Area access**, an existing admin can resend the verification link directly. Check before re-submitting — duplicate signups can conflict.
5. **Re-check the form for a typo in the email field** — a bounced confirmation is indistinguishable from "never sent" from your side.
6. **After ~24–48 hours with none of the above working**, contact Adyen support via https://www.adyen.com/contact, referencing the test account signup, the email used, and the approximate signup time.

Reference: https://help.adyen.com/knowledge/account/access-your-customer-area/what-can-i-do-if-i-have-trouble-accessing-the-customer-area

## 3. Stripe

- **Process:** Self-serve. Sign up at https://dashboard.stripe.com/register. Every account starts in **sandbox/test mode** automatically — no separate request.
- **What you get:** A test **secret key** (`sk_test_...`) and **publishable key** (`pk_test_...`), visible immediately under Developers → API keys.
- **Docs:** https://docs.stripe.com/get-started/api-request
- **Set:** `STRIPE_SECRET_KEY`

Consider a **Restricted API Key** (`rk_...`) scoped to only Payments, Checkout Sessions and Refunds, per Stripe's current best-practice guidance. The benchmark needs nothing beyond those.

`scripts/stripe_gw.py` charges Stripe's test token (`tok_visa`) rather than a raw PAN, because Stripe blocks raw card numbers on `/v1/payment_methods` for accounts without PCI enablement. That matches how the comparison scopes Adyen and Checkout.com — tokenization is a browser-side precondition, not a merchant-server call.

## 4. Checkout.com

- **Process:** Self-serve. Sign up at https://www.checkout.com/get-test-account.
- **What you get:** A sandbox account with immediate card-payment testing. Generate test **API keys** (secret + public) from the Dashboard.
- **Docs:** https://support.checkout.com/hc/en-us/articles/14327083327890-Create-a-sandbox-account and https://www.checkout.com/docs/get-started
- **Set:** `CHECKOUT_SECRET_KEY`, `CHECKOUT_PUBLIC_KEY` (the public key mints card tokens — without it the Direct API and MIT flows are skipped), and `CHECKOUT_PROCESSING_CHANNEL_ID` if your account is on NAS.

If the dashboard shows a subdomain prefix on your API host, put the full host in `CHECKOUT_API_BASE`.

For alternative payment methods or raw card-data (SAQ D) testing you need to contact a Checkout.com solutions engineer — not needed for this benchmark's core flows.

### Troubleshooting: no confirmation/access email received

1. **Check spam/promotions/quarantine first** — same as Adyen, the most common cause by far.
2. **Confirm the signup actually submitted** — revisit https://www.checkout.com/get-test-account and see whether it reports "account pending" or "already registered" for your email, rather than re-submitting blindly.
3. **Try a business email domain** rather than a free consumer address.
4. **Try logging in anyway** at https://dashboard.sandbox.checkout.com — some flows grant dashboard access before email verification completes.
5. **After ~24 hours**, contact support@checkout.com or https://support.checkout.com/hc/en-us, referencing the "Create a sandbox account" flow and the email used.

Reference: https://support.checkout.com/hc/en-us/articles/14327357069074-Create-a-Checkout-com-account

## 5. HyperPay

- **Process:** **No public self-serve sandbox signup.** HyperPay is a MENA-focused acquirer/PSP and appears to provision Test System credentials only after commercial onboarding.
- **Action:** Request test system access via https://www.hyperpay.com/get-started, https://www.hyperpay.com/merchant-support, or https://www.hyperpay.com/contact. Expect a sales/onboarding conversation before you receive an **Entity ID** and **access token** for their Copy&Pay / Server-to-Server Test System.
- **Docs (integration mechanics once you have access):** https://www.hyperpay.com/integration-guide/
- **Set:** `HYPERPAY_ACCESS_TOKEN`, `HYPERPAY_ENTITY_ID`, and `HYPERPAY_API_BASE` if they issue a HyperPay-branded host rather than `eu-test.oppwa.com`.

**Budget lead time for this one** — it will not be instant like the others. It is also the gateway with the largest documentation gap, so it benefits most from a live run. Two things worth raising directly with their integration contact while you wait:

- The exact URL shape for **capture/refund/reversal** (`paymentType` `CP`/`RF`/`RV`). Both dedicated tutorial pages 404'd during research, so `scripts/hyperpay.py` defaults to `POST /v1/payments/{id}` and offers `HYPERPAY_BACKOFFICE_PATH=nested` for Peach Payments' `/v1/payments/{id}/payments` shape. One of the two is right; a five-minute answer from them saves a guessing round.
- Whether **webhooks are mandatory** for Copy&Pay — the nav lists a section whose content was unreachable.

## 6. Moyasar

- **Process:** Self-serve. Sign up at https://moyasar.com and create a dashboard account.
- **What you get:** Test keys prefixed `pk_test_` (publishable) and `sk_test_` (secret).
- **Auth:** HTTP Basic — username = your secret key, password = empty string.
- **Docs:** https://docs.moyasar.com/api/authentication, test cards at https://docs.moyasar.com/guides/card-payments/test-cards
- **Set:** `MOYASAR_SECRET_KEY`, `MOYASAR_PUBLISHABLE_KEY`, and keep `MOYASAR_CURRENCY=SAR` — Moyasar sandboxes are SAR-first, so a USD-wide run fails only here.

`MOYASAR_DIRECT_MODE` selects which of Moyasar's two contradictory documented paths gets measured: `token` (tokenize first — 3 calls, what their Custom UI guide recommends) or `card` (raw card fields — 2 calls, what their API reference schema allows). Run both and you have resolved data gap #19 empirically, which is worth doing since it is a full call's difference.

---

## Summary

| Gateway | Self-serve? | Where | Turnaround |
|---|---|---|---|
| Geidea | No | Enablement team / merchant portal | Depends on your existing relationship |
| Adyen | Yes | adyen.com/get-started | Minutes |
| Stripe | Yes | dashboard.stripe.com/register | Minutes |
| Checkout.com | Yes | checkout.com/get-test-account | Minutes |
| HyperPay | No | Sales / merchant support contact | Days (commercial process) |
| Moyasar | Yes | moyasar.com | Minutes |

**Recommended order:** fire off the Geidea and HyperPay requests first since they involve human turnaround, then do Stripe and Moyasar — both are self-serve with keys visible in the dashboard immediately and no email-confirmation dependency, so they are the fastest path to real numbers. Adyen and Checkout.com sit in between; if their confirmation emails stall, the troubleshooting sections above cover it, and there is no reason to wait on them before starting.

## Next step

```bash
cd scripts
cp .env.example .env          # then fill in what you have
python3 -m pip install -r requirements.txt --break-system-packages
python demo.py                # confirms the toolchain works, no credentials needed
python run_all.py --list      # shows which gateways your .env has configured
python run_all.py             # the real thing
```

Run `run_all.py` **from your own MENA-region infrastructure**, not from a laptop on another continent or a cloud session in a random region. Network latency dominates every other difference this benchmark measures, so where you run it from is not a detail — it is the single biggest factor in whether the numbers mean anything. Set `MEASUREMENT_LOCATION` in `.env` so the results say where they came from.
