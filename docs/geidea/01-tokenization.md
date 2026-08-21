# Geidea — Tokenization
Source: https://docs.geidea.net/docs/tokenization
Fetched: 2026-08-21

## Overview

Tokenization replaces a customer's sensitive card data with a unique identifier
(a token) that the merchant can save and use later to perform transactions,
without needing PCI-DSS compliance.

Two implementations:
- **CIT — Customer Initiated Transactions**: customer is present, substitutes
  card number/expiry/name entry, customer still enters CVC + OTP to authorize.
- **MIT — Merchant Initiated Transactions**: recurring/unscheduled payments,
  no customer presence or authorization required per-charge. See separate
  merchant-initiated-mit.md doc for the full MIT flow — it layers additional
  required fields (cofAgreement, initiatedBy, agreementId/agreementType) on
  top of this base tokenization flow.

## Token Generation and Pay with Token

Recommended approach: "Card/Credentials on file" transaction. Cardholder
gives explicit authorization to store their account info; it's replaced with
a token ID.

3 steps to create a token and use it:
1. Successful Merchant Authentication
2. Successful Token Generation after a successful 3DS transaction
3. Use the generated TokenID for receiving further payments.

**Authentication**: username = merchant's public key, password = API
password (Basic Auth). Never expose the API password in frontend code —
server-side only, via a backend proxy.

## Token Creation

During Hosted Payment Page session creation, pass `cardOnFile: true`.

1. The initial payment MUST use 3DS (strong customer authentication) and
   must be Successful.
2. On success, you receive a `tokenId` in the order callback URL payload.
3. Once `tokenId` is generated, Geidea has saved that payment method for
   future use.

### Required fields for Session Creation API call (token creation)

| Parameter    | Description |
|---|---|
| `amount` | Total payment amount. Double, 2 decimal places. E.g. `19.99` |
| `currency` | 3-letter ISO 4217 code, e.g. `SAR`. Contact support to enable multicurrency. |
| `cardOnFile` | `true` |
| `signature` | See signature generation — refer to the HPP checkout doc's signature section (base signature scheme, distinct from the MIT-specific and callback-specific signature schemes documented elsewhere) |

### Environment base URLs (use the correct one per merchant account)
- KSA: `https://api.ksamerchant.geidea.net/`
- Egypt: `https://api.merchant.geidea.net/`
- UAE: `https://api.geidea.ae/`

### Sample request (token creation session)

```
POST https://api.merchant.geidea.net/payment-intent/api/v2/direct/session
Authorization: Basic <base64(merchant_public_key:api_password)>
Content-Type: application/json

{
  "amount": 10.00,
  "currency": "SAR",
  "callbackUrl": "https://www.example.com/callback",
  "signature": "4Vgy1C4JSLm8o8uxz4Ewj1pv6KbLQ6dj/hu0ExpTWyI=",
  "merchantReferenceId": "ABC-123",
  "language": "ar",
  "cardOnFile": true
}
```

### Sample successful session response

```json
{
    "session": {
        "id": "9c52785f-f092-4977-7816-08db602e2587",
        "amount": 10,
        "currency": "SAR",
        "callbackUrl": "https://webhook.site/...",
        "returnUrl": "https://someurl.com",
        "expiryDate": "2023-05-31T21:17:00.8733674Z",
        "status": "Initiated",
        "merchantId": "6876f6bc-f8eb-4253-f160-08d973705ffb",
        "merchantPublicKey": "6620c3e2-5088-41a8-8be6-98c003153932"
    },
    "responseMessage": "Success",
    "detailedResponseMessage": "The operation was successful",
    "language": "EN",
    "responseCode": "000",
    "detailedResponseCode": "000"
}
```

When approved with `cardOnFile: true`, the merchant receives a `tokenId` in
the callback payload once `detailedStatus` is `"Paid"`. Store the token
against the customer for future use — never store the full underlying token
(you only ever receive the `tokenId` reference, not raw card data).

### Sample callback payload after successful tokenizing payment (abridged — key fields)

```json
{
  "order": {
    "orderId": "9e238617-1ae9-469f-02f7-08db602e5619",
    "amount": 101,
    "totalAmount": 101,
    "currency": "SAR",
    "detailedStatus": "Paid",
    "status": "Success",
    "merchantReferenceId": "test-site-...",
    "cardOnFile": true,
    "tokenId": "5c430263-0fff-4a1a-f0f7-08db59cbdec9",
    "initiatedBy": "Internet",
    "paymentOperation": "Pay",
    "transactions": [
      {
        "transactionId": "...",
        "type": "Authentication",
        "status": "Success",
        "amount": 101,
        "source": "HPP",
        "paymentMethod": {
          "type": "Card",
          "brand": "visa",
          "maskedCardNumber": "444000******0010",
          "expiryDate": { "month": 1, "year": 39 }
        },
        "codes": {
          "responseCode": "000",
          "responseMessage": "Success",
          "detailedResponseCode": "000",
          "detailedResponseMessage": "The operation was successful"
        },
        "authenticationDetails": { "...": "3DS ACS/DS fields" }
      },
      {
        "transactionId": "...",
        "type": "Pay",
        "status": "Success",
        "amount": 101,
        "authorizationCode": "100086",
        "rrn": "315105100086",
        "codes": {
          "acquirerCode": "00",
          "acquirerMessage": "Approved",
          "responseCode": "000",
          "responseMessage": "Success",
          "detailedResponseCode": "000",
          "detailedResponseMessage": "The operation was successful"
        }
      }
    ],
    "totalAuthorizedAmount": 101,
    "totalCapturedAmount": 101,
    "totalRefundedAmount": 0,
    "isTest": true
  },
  "signature": "C3sjR0O+mcrORORMU4s/MgrvxgJW/2wmROWdoVAQosI="
}
```

Note the order object contains a `transactions` array with distinct
`Authentication` and `Pay` transaction types — each with its own
`transactionId`, `codes`, and status. This maps directly to the
`api_call_logs` sequencing requirement: a tokenizing HPP payment is at
minimum 2 gateway-side transaction records even though it's 1 merchant
session-create call plus the HPP-hosted flow.

## Tokenized Transaction (pay with an existing token)

1. Merchant retrieves the stored `tokenId` for the customer/card.
2. Merchant does NOT receive the full token (encrypted card details) —
   only ever the `tokenId`.
3. Merchant includes `tokenId`, `amount`, `signature`, `currency` in a new
   session-create request.

### Sample request (pay with token)

```
POST https://api.merchant.geidea.net/payment-intent/api/v2/direct/session
Authorization: Basic <base64(merchant_public_key:api_password)>
Content-Type: application/json

{
  "amount": 10.00,
  "currency": "SAR",
  "callbackUrl": "https://www.example.com/callback",
  "merchantReferenceId": "ABC-123",
  "signature": "4Vgy1C4JSLm8o8uxz4Ewj1pv6KbLQ6dj/hu0ExpTWyI=",
  "language": "ar",
  "tokenId": "5c430263-0fff-4a1a-f0f7-08db59cbdec9"
}
```

Then pass the returned `session.id` to the checkout.html / GeideaCheckout
flow to complete. Callback payload shape mirrors the tokenizing-payment
example above (`cardOnFile: false`, `tokenId: null` in the *order* object
since this order didn't create a new token — the token was consumed, not
generated).

## Implementation notes for BuraPay

- CIT token creation is fundamentally an HPP-session-create call with
  `cardOnFile: true`, then the standard HPP redirect/modal flow, then a
  callback carrying `tokenId`. It is NOT a separate "tokenize" REST
  endpoint on its own — it's the Session Creation API with a flag.
- CIT charge-with-token is the same Session Creation API, but with `tokenId`
  instead of raw card fields, still followed by the HPP flow (customer
  still present to confirm/CVC/OTP per the CIT definition above).
- Do not confuse this base tokenization flow with MIT — MIT requires
  additional fields documented separately (cofAgreement, initiatedBy=
  "Merchant", agreementId, agreementType) and uses a DIFFERENT signature
  algorithm (HMAC-SHA256 keyed by API password over
  {MerchantPublicKey, SessionId, TimeStamp} — see merchant-initiated-mit.md).
- The base/HPP signature algorithm referenced here is defined on the HPP
  Checkout doc page (docs/geidea-checkout-v2#signature) — fetch that page
  separately before implementing signature generation for this flow; do not
  reuse the MIT or callback signature formulas here.
