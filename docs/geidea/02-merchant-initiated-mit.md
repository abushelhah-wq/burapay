# Geidea — Merchant Initiated (MIT)
Source: https://docs.geidea.net/docs/merchant-initiated-mit
Fetched: 2026-08-21

Two-step flow: (1) tokenize the card during an initial customer-present
payment, (2) later charge that token with no customer present.

### Environment base URLs
- KSA: `https://api.ksamerchant.geidea.net/`
- Egypt: `https://api.merchant.geidea.net/`
- UAE: `https://api.geidea.ae/`

## Step 1: Customer Card Tokenization

Session-create request MUST include:
- `initiatedBy`: `"Internet"`
- `cardOnFile`: `true`
- `cofAgreement`: object:
  - `id`: unique Agreement ID (merchant-generated, per customer/subscription)
  - `type`: `"Unscheduled"`

### Sample request

```
POST https://api.ksamerchant.geidea.net/payment-intent/api/v2/direct/session
accept: application/json
authorization: Basic <encoded_auth_header>
content-type: application/json

{
  "amount": "<amount>",
  "currency": "<currency>",
  "timestamp": "<timestamp>",
  "merchantReferenceId": "<merchant_reference_id>",
  "signature": "<signature>",
  "paymentOperation": "Pay",
  "cardOnFile": true,
  "callbackUrl": "https://webhook.site/...",
  "initiatedBy": "Internet",
  "cofAgreement": {
    "id": "<agreement_id>",
    "type": "unscheduled"
  }
}
```

On success, `tokenId` arrives at the callback URL. Store `tokenId` AND
`agreementId` together against the customer — both are required for step 2.
`tokenId` is unique per card.

This step's signature uses the base HPP signature scheme (see
geidea-checkout-v2#signature) — NOT the MIT-specific signature described
below, which only applies to step 2.2.

## Step 2: Initiating MIT Transactions

### 2.1 Session Generation for MIT

New session-create request, must include:
- `initiatedBy`: `"Merchant"`
- `agreementId`: same Agreement ID used at tokenization
- `agreementType`: `"Unscheduled"`
- `tokenId`: the token received from step 1's callback

```
POST https://api.ksamerchant.geidea.net/payment-intent/api/v2/direct/session
accept: application/json
authorization: Basic <encoded_auth_header>
content-type: application/json

{
  "amount": "<amount>",
  "currency": "<currency>",
  "timestamp": "<timestamp>",
  "merchantReferenceId": "<merchant_reference_id>",
  "signature": "<signature>",
  "paymentOperation": "Pay",
  "callbackUrl": "https://webhook.site/...",
  "initiatedBy": "Merchant",
  "agreementId": "<agreement_id>",
  "agreementType": "unscheduled",
  "tokenId": "<token_id>"
}
```

### 2.2 Initiating the MIT Transaction

Use the `sessionId` from 2.1. Requires a NEW signature, generated with a
DIFFERENT algorithm than the base HPP signature:

**MIT Signature Hashing Steps**
1. Concatenate the string: `{MerchantPublicKey}{SessionId}{TimeStamp}`
   (no separators — straight concatenation)
2. Hash (HMAC-SHA256) this concatenated string, keyed by `Merchant_API_Password`
3. Base64-encode the resulting hash

Reference implementation (PHP, from the docs):
```php
function generate_MIT_signature($merchant_public_key, $session_id, $timestamp, $api_password) {
    $data_string = $merchant_public_key . $session_id . $timestamp;
    $hash = hash_hmac('sha256', $data_string, $api_password, true);
    return base64_encode($hash);
}
```

Reference implementation (C#, from the docs):
```csharp
public static string GenerateMITSignature(string merchantPublicKey, string sessionId, string timeStamp, string apiPassword)
{
    string data = merchantPublicKey + sessionId + timeStamp;
    byte[] keyBytes = Encoding.UTF8.GetBytes(apiPassword);
    byte[] dataBytes = Encoding.UTF8.GetBytes(data);
    using (var hmac = new HMACSHA256(keyBytes))
    {
        byte[] hashBytes = hmac.ComputeHash(dataBytes);
        return Convert.ToBase64String(hashBytes);
    }
}
```

Python equivalent for BuraPay's implementation:
```python
import hmac
import hashlib
import base64

def generate_mit_signature(merchant_public_key: str, session_id: str, timestamp: str, api_password: str) -> str:
    data = f"{merchant_public_key}{session_id}{timestamp}"
    digest = hmac.new(
        api_password.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")
```

### Sample request for initiating MIT

```
POST https://api.ksamerchant.geidea.net/pgw/api/v2/direct/pay/token
accept: application/json
authorization: Basic <encoded_auth_header>
content-type: application/json

{
  "sessionid": "<session_id>",
  "callbackUrl": "https://webhook.site/...",
  "initiatedBy": "Merchant",
  "agreementId": "<agreement_id>",
  "agreementType": "unscheduled",
  "signature": "<signature>"
}
```

Note the endpoint path differs from Direct API sale
(`/pgw/api/v2/direct/pay/token`) — this is a distinct endpoint from the
generic session/pay flow, specific to token-based charges.

## Implementation notes for BuraPay

- MIT is a MINIMUM 2-call flow at the merchant-account level even before
  counting the original tokenization: (1) session generation for MIT
  (2.1), (2) the actual pay/token call (2.2). Both must be logged as
  separate `api_call_logs` rows with correct `sequence_number` and both
  count toward `request_count` for an MIT_CHARGE operation.
- The full lifecycle for "first MIT charge against a customer" is therefore:
  tokenization session-create (1 call) + HPP-hosted authentication/pay
  (server-to-server callback, not a call BuraPay makes) + MIT session
  generation (1 call) + MIT pay/token (1 call) = at least 3 logged BuraPay-
  initiated calls, distinct from the CIT flow's call count.
- Do NOT reuse the base HPP signature or the callback signature formula for
  step 2.2 — this is the one place Geidea's docs specify HMAC-SHA256 keyed
  by the API password over a specific 3-field concatenation. Getting this
  wrong produces a signature that Geidea will reject cleanly (not a security
  hole, but it will silently look like "MIT doesn't work" if implemented
  with the wrong formula).
- `agreementId` must be treated as a durable per-customer(or per-
  subscription) identifier BuraPay generates and stores — it is not
  returned by Geidea, the merchant invents it at tokenization time and must
  reuse the exact same value at every subsequent MIT charge.
