# Geidea docs — fetched for BuraPay build

These four files are direct fetches of the Geidea documentation pages the
AI coding tool reported it could NOT reach from its own sandboxed network
(docs.geidea.net was blocked at that tool's egress proxy — the domain
itself is live; this is a network policy on that specific tool, not a
Geidea outage). Fetched externally and saved here so implementation can
proceed with zero dependency on that tool's network access.

Files:
- `01-tokenization.md` — base tokenization/CIT flow, create-token and
  pay-with-token request/response shapes, the HPP-based signature scheme
  reference.
- `02-merchant-initiated-mit.md` — full MIT flow, including the exact
  HMAC-SHA256 signature algorithm (with Python/PHP/C# implementations) —
  this was the field-name risk the original build flagged as too dangerous
  to guess, now resolved from source.
- `03-standalone-save-card.md` — the separate /savecard/create-session
  endpoint for tokenizing a card with no real purchase, including its own
  (third) distinct signature formula and the auto-void behavior.
- `04-webhook-callback-notifications.md` — the (fourth) callback signature
  formula, verification checklist, and — importantly — documented edge
  cases around InProgress orders, retried payment attempts, and multi-
  transaction callback payloads that directly affect BuraPay's
  reconciliation/timeout-handling logic.

## Important cross-cutting finding

Geidea documents FOUR textually distinct signature algorithms across these
flows (base HPP, MIT, save-card, callback verification) — different field
concatenation order and in one case a different hash construction (plain
SHA-256 vs HMAC-SHA256). Each file calls out its own formula explicitly and
warns against reusing another flow's formula. This is exactly the kind of
detail an AI would get wrong by pattern-matching one signature scheme onto
another if forced to guess — hand these files over as-is rather than
summarizing them further, so the exact field lists survive verbatim.

## How to use

Hand these files directly to the coding tool as context/attachments when
implementing the Geidea adapter's tokenization, CIT, MIT, save-card, and
webhook-verification code paths. The tool does not need network access to
docs.geidea.net for this subset of the work.

Still outstanding, not covered by this package (fetch separately if/when
needed):
- `geidea-checkout-v2` (HPP Checkout) — referenced repeatedly above as the
  source of the "base" signature formula; needed to actually implement it
  since these four files describe it by reference, not in full.
- `initiate-authentication-v-2`, `authenticate-payer-v2`, `pay-v2` (Direct
  API's three-step flow) — needed if Direct API sale/auth wasn't already
  verified against source in the earlier build pass.
- `refund-2`, `void-1`, `cancel-order-1`, `fetch-1` (order/transaction
  management) — same caveat.
- `test-cards.md`, `api-response-codes-and-messages.md` — needed for
  integration tests and for mapping Geidea's response codes to BuraPay's
  internal status enum.

If the coding tool's network access gets unblocked (or the org allow-lists
docs.geidea.net for it), it can fetch the remaining pages itself following
the same .md-suffix pattern used throughout this build
(`https://docs.geidea.net/docs/<page>.md`). Otherwise, ask and these can be
fetched externally the same way this batch was.
