# Geidea — Standalone Save Card
Source: https://docs.geidea.net/docs/save-card
Fetched: 2026-08-21

Lets a customer save a card as a payment method WITHOUT going through a
checkout/purchase flow. Distinct from the tokenization-during-payment flow
in 01-tokenization.md.

**Pre-requisite**: merchant's MID must have tokenization enabled.

## How it works

1. Merchant POSTs to `/savecard/create-session` to get a `sessionId`.
2. Customer is directed to the HPP (modal/iframe by default via
   `GeideaCheckout`), enters card number/expiry/CVV.
3. Transaction goes to the processor for approval (may require 3DS/OTP).
4. On approval, a token is generated and returned via callback.
5. **Automatic Transaction Void**: after token generation, Geidea
   automatically voids the initial transaction so the customer is NOT
   actually charged for the save-card action. Merchant keeps the token.

This auto-void behavior is specific to save-card and is NOT how the
tokenization-during-payment flow in 01-tokenization.md works (that flow's
initial payment amount is real and captured, e.g. 10.00 SAR actually
charged as part of tokenizing).

## Signature Hashing Steps (save-card specific — a THIRD distinct algorithm)

1. Concatenate the string: `{timeStamp}{MerchantPublicKey}{OrderCurrency}`
2. Hash (SHA-256) the concatenated string, keyed by `MerchantAPIPassword`
3. Base64-encode the result

Note the field order and inputs differ from both the MIT signature
(01/02 docs) and the base HPP signature — three separate signature
formulas exist across these flows. Do not reuse one for another.

## Create Session endpoint

```
POST https://api.merchant.geidea.net/payment-intent/api/v2/direct/session/saveCard
Content-Type: application/json
Authorization: Basic <base64(merchant_public_key:api_password)>

{
    "currency": "EGP",
    "callbackUrl": "https://webhook.site/...",
    "returnUrl": "https://www.geidea.net/",
    "language": "en",
    "appearance": {
        "receiptPage": true,
        "styles": { "hideGeideaLogo": true },
        "uiMode": "modal"
    },
    "cofAgreement": {
        "id": "MH",
        "type": "Unscheduled"
    },
    "merchantReferenceId": "MohammedHamdy",
    "signature": "ENif6Ew2pXCCP1ToOs1VN9xBW7xZeV88ee+Mrd2rrXs=",
    "timeStamp": "10/20/2025 5:16:48 AM"
}
```

Note the endpoint path is `/session/saveCard`, distinct from the standard
`/session` endpoint used for payments and tokenization-during-payment.

### Supported currencies (from this page)
SAR, EGP, AED, QAR, OMR, BHD, KWD, USD, GBP, EUR (multicurrency requires
contacting support to enable on the account).

### Sample response (abridged — key fields)

```json
{
    "session": {
        "id": "0665813c-0047-4df0-c0e6-08de0fb6b989",
        "amount": 1,
        "currency": "EGP",
        "status": "Initiated",
        "paymentOperation": "SaveCard",
        "cardOnFile": true,
        "cofAgreement": { "id": "MH", "type": "Unscheduled" },
        "tokenId": null
    },
    "responseMessage": "Success",
    "responseCode": "000",
    "detailedResponseCode": "000",
    "signature": "HBllKtD3+hf0ptRmm5dZOxWTC6eJl9x0t4mgZpNG2Jg="
}
```

Note `paymentOperation: "SaveCard"` — this is a distinct operation value
from `"Pay"`, useful for classifying the api_call_logs / transaction row.

## Start the payment

```javascript
const payment = new GeideaCheckout(onSuccess, onError, onCancel);
payment.startPayment(sessionId);
```

## Implementation notes for BuraPay

- If BuraPay wants a standalone "Tokenize" action (independent of a real
  purchase), THIS is the correct endpoint (`/session/saveCard`), not the
  cardOnFile-during-payment flow in 01-tokenization.md. The two differ in:
  endpoint path, signature formula, and whether Geidea auto-voids the
  underlying transaction.
- Because Geidea auto-voids the save-card transaction server-side, BuraPay
  should NOT also issue its own void/reversal call against a save-card
  session — that would either double-void (harmless no-op, but wastes a
  logged call and confuses the request_count metric) or fail because
  Geidea already voided it. Treat the resulting transaction as VOID/
  no-charge automatically once the callback confirms token issuance, don't
  add a manual void step to this specific flow.
- The `amount` field in the sample response shows `1` even for a save-card
  session — this may be a placeholder/minimum charge amount used internally
  for the auth-then-auto-void mechanic, not a real customer-facing charge.
  Confirm actual behavior against sandbox before assuming `amount` is
  meaningful for this operation type in BuraPay's UI (likely display as
  "Card saved — no charge" rather than showing a dollar amount).
