# Geidea — Webhook/Callback Notifications
Source: https://docs.geidea.net/docs/sample-callback-responses
Fetched: 2026-08-21

## Purpose

Server-to-server notifications sent to the merchant's `callbackUrl` as a
transaction's status changes (successful, failed, pending, etc.), so the
merchant doesn't rely solely on the browser redirect return (which can fail
to arrive due to the customer's connection, browser issues, etc.).

## Callback Signature (a FOURTH distinct signature formula — do not reuse
## the base HPP, MIT, or save-card formulas for this)

Introduced as a `signature` parameter in the Pay API response payload, to
let the merchant verify the callback is authentic.

**Signature Hashing Steps**
1. Concatenate the string:
   `{MerchantPublicKey}{OrderAmount}{OrderCurrency}{OrderId}{Status}{MerchantReferenceId}{timeStamp}`
2. Hash (SHA-256) this concatenated string, keyed by `MerchantAPIPassword`
3. Base64-encode the result

The merchant must independently compute this signature from the fields in
the callback payload and compare against the `signature` field in the
payload to confirm authenticity before trusting the callback.

Python implementation for BuraPay:
```python
import hmac
import hashlib
import base64

def verify_callback_signature(
    merchant_public_key: str,
    order_amount: str,
    order_currency: str,
    order_id: str,
    status: str,
    merchant_reference_id: str,
    timestamp: str,
    api_password: str,
    received_signature: str,
) -> bool:
    data = (
        f"{merchant_public_key}{order_amount}{order_currency}"
        f"{order_id}{status}{merchant_reference_id}{timestamp}"
    )
    digest = hmac.new(
        api_password.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, received_signature)
```
Note: use `hmac.compare_digest` for the comparison, not `==`, to avoid a
timing side-channel — this is a BuraPay implementation requirement, not
something the Geidea docs specify, but it's standard practice for signature
verification and should be treated as non-negotiable in the adapter code.

**IMPORTANT — confirm exact field ordering and format against the actual
`geidea-checkout-v2#signature` base signature page too before finalizing**:
this callback page states the field list and hash algorithm but does not
show a fully worked numeric example the way the MIT page does. Treat this
as high-confidence but verify against one real sandbox callback payload
before shipping — compute the signature server-side from a captured real
callback and confirm it matches the `signature` field Geidea actually sent,
rather than trusting the doc prose alone.

## Verification checklist before acting on a callback (from the docs)

Beyond signature verification, before treating a callback as authoritative:
- `amount` in the callback matches BuraPay's own recorded order amount.
- `responseCode` == `"000"`
- `responseMessage` == `"Success"`
- `detailedResponseCode` == `"000"`
- `detailedResponseMessage` == `"The operation was successful"`

Only if ALL of the above hold should BuraPay mark the transaction as
successfully completed from a callback. A signature that verifies but a
response code that doesn't match "000" means the callback is authentic but
reports a failure/decline — record it as such, don't treat "signature
valid" as "payment succeeded."

## Callback timing / order lifecycle edge cases (from the docs — important
## for BuraPay's status-reconciliation logic)

- When a customer enters a card and an Order ID is generated, the order
  status becomes `"InProgress"`. **No callback is sent yet** — the payment
  journey isn't complete.
- If the customer reaches the 3DS/OTP screen and cancels or retries with a
  different payment method, the order STAYS `"InProgress"` — still no
  callback.
- If the customer abandons the HPP entirely (closes it), a callback IS sent
  with `detailedResponseMessage: "Transaction Cancelled By User."`
- **Authentication failures do NOT produce a separate callback** — the
  order remains `"InProgress"` through failed auth attempts.
- Customers can retry paying the same order multiple times. Once the order
  is EVENTUALLY paid successfully, the single callback you receive
  consolidates the FULL history of all attempts (failed + successful) under
  one order ID, in a `transactions` array.

## Implications for BuraPay's order-query / reconciliation logic

This has direct consequences for §4.j and §6 of the build spec (timeout
handling / order-query reconciliation):

1. A BuraPay transaction sitting in `PENDING` with no callback yet is NOT
   necessarily stuck or failed — it may genuinely be `InProgress` at
   Geidea's end (customer still on the 3DS page, or retried and hasn't
   succeeded yet). Do not treat "no callback within N seconds" as
   equivalent to "failed" — poll via Order Query instead of assuming
   failure, and expect `InProgress` as a valid, non-terminal status.
2. When a callback finally arrives for an order that had multiple payment
   attempts, its `transactions` array contains MULTIPLE transaction records
   (failed attempts + the final successful one) under a single `orderId`.
   BuraPay's `api_call_logs`/`transactions` schema must be able to
   represent this: one BuraPay `transactions` row per logical
   customer-facing operation, but the gateway-side `transactions[]` array
   inside a single callback may report more sub-events than BuraPay itself
   initiated calls for — log the full array content into the stored
   callback payload (webhooks_received.body_json) even though only the
   FINAL outcome updates BuraPay's own transaction status.
3. Do not mark a BuraPay transaction as `FAILED` purely because an
   individual attempt within the `transactions[]` array shows a failed
   sub-transaction — check the outer `order.status`/`order.detailedStatus`
   for the authoritative final outcome, since Geidea may report several
   failed sub-attempts followed by an eventual success, all in one payload.

## Implementation notes for BuraPay

- `verify_webhook()` in the `GatewayAdapter` interface (see build spec §3)
  must implement this exact signature check for the Geidea adapter.
- A callback with a valid signature but `responseCode != "000"` still gets
  logged to `webhooks_received` with `signature_valid = true` — it's a
  legitimate failure notification, not a security failure.
- A callback with an INVALID signature must be logged with
  `signature_valid = false` and must NOT be used to update any transaction
  status — treat as a data point for the audit log only, and consider
  surfacing it in the UI/logs as a potential integrity issue worth
  investigating, since it could indicate misconfiguration (wrong API
  password / merchant public key used for verification) rather than an
  actual attack, especially early in an integration.
