"""
Geidea request authentication and signing.

AUTHENTICATION -- doc-verified
    HTTP Basic: username = Merchant Public Key, password = API Password.
    Confirmed by worked samples in docs/geidea/01-tokenization.md and
    docs/geidea/02-merchant-initiated-mit.md.

FOUR DISTINCT SIGNATURE ALGORITHMS
----------------------------------
Geidea does not use one signature scheme. It uses four, with different field
lists, different concatenation orders, and -- in the base case -- a formula
that is still not published in the material available here. Reusing one flow's
formula for another is the single most likely way to get this wrong, so each
function below states its source and refuses to be generic:

  1. base / HPP session   -- INFERRED. Formula not in the fetched pages; the
                             pages reference geidea-checkout-v2#signature,
                             which could not be retrieved.
  2. MIT pay/token        -- VERIFIED. HMAC-SHA256 over
                             {MerchantPublicKey}{SessionId}{TimeStamp}.
                             docs/geidea/02, which ships PHP, C# and Python
                             reference implementations that agree.
  3. save-card session    -- VERIFIED (field list and order), from docs/geidea/03:
                             {timeStamp}{MerchantPublicKey}{OrderCurrency}.
  4. callback verification-- VERIFIED, from docs/geidea/04:
                             {MerchantPublicKey}{OrderAmount}{OrderCurrency}
                             {OrderId}{Status}{MerchantReferenceId}{timeStamp}.

On "hash, keyed by": pages 3 and 4 both describe the operation as "Hash
(SHA-256) the concatenated string, keyed by MerchantAPIPassword". A keyed
SHA-256 hash is HMAC-SHA256, and page 4's own reference implementation uses
``hmac.new(..., hashlib.sha256)``, which settles the reading for that page.
Page 3 uses identical wording but ships no reference code, so its construction
is inferred from that wording rather than from an example -- flagged in
:func:`save_card_signature`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.money import exponent
from app.timeutil import utcnow

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

#: The only worked timestamp example in the fetched documentation is
#: ``"10/20/2025 5:16:48 AM"`` (docs/geidea/03-standalone-save-card.md), which
#: is US-style month/day/year with a 12-hour clock and an unpadded hour.
#: Python's ``%I`` pads the hour, so :func:`timestamp` strips that zero to match
#: the example exactly.
#:
#: Padding of single-digit month and day cannot be determined from one example
#: whose month and day are both two digits. ``%m``/``%d`` (padded) is kept, and
#: the whole format is overridable from gateway config so a deployment can
#: correct it without a code change.
DEFAULT_TIMESTAMP_FORMAT = "%m/%d/%Y %I:%M:%S %p"

#: Alternate spellings seen in Geidea material. Kept so a signature rejection
#: can be worked through by configuration rather than by editing this file.
ALTERNATE_TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%m/%d/%Y %I:%M:%S %p",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

#: Field order for the base/HPP session signature. INFERRED -- see module
#: docstring. Overridable from gateway config.
DEFAULT_SIGNATURE_FIELDS: tuple[str, ...] = (
    "publicKey", "amount", "currency", "merchantReferenceId", "timestamp",
)


def timestamp(fmt: str | None = None) -> str:
    """
    Render 'now' in the format Geidea's worked example shows.

    The unpadded hour is deliberate: ``5:16:48 AM``, not ``05:16:48 AM``. The
    timestamp is part of three of the four signed strings, so a padding
    difference changes the signature.
    """
    value = utcnow().strftime(fmt or DEFAULT_TIMESTAMP_FORMAT)
    if (fmt or DEFAULT_TIMESTAMP_FORMAT) == DEFAULT_TIMESTAMP_FORMAT:
        # "10/20/2025 05:16:48 AM" -> "10/20/2025 5:16:48 AM"
        date_part, _, time_part = value.partition(" ")
        if time_part.startswith("0"):
            time_part = time_part[1:]
        value = f"{date_part} {time_part}"
    return value


def format_amount(amount_minor: int, currency: str) -> str:
    """
    Render minor units as the decimal string Geidea's JSON carries.

    Kept adapter-local rather than reusing :func:`app.money.format_minor`: that
    one is the presentation layer's, and a gateway's wire format must not be
    coupled to how the UI happens to display money.
    """
    places = exponent(currency)
    return str(
        Decimal(amount_minor).scaleb(-places).quantize(Decimal(1).scaleb(-places))
    )


def _hmac_b64(api_password: str, message: str) -> str:
    digest = hmac.new(
        api_password.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


# ---------------------------------------------------------------------------
# 1. Base / HPP session signature -- INFERRED
# ---------------------------------------------------------------------------

def build_signature(
    *,
    api_password: str,
    values: Mapping[str, str],
    fields: Sequence[str] = DEFAULT_SIGNATURE_FIELDS,
) -> str:
    """
    base64(HMAC-SHA256(apiPassword, concat(values[f] for f in fields))).

    INFERRED. The fetched pages all defer to ``geidea-checkout-v2#signature``
    for this formula and none of them reproduces it, so the field order here is
    not confirmed. It is shipped because a wrong signature produces a clean
    rejection that gets logged in full -- it cannot move money to the wrong
    place -- and because both the field order and the timestamp format are
    overridable from ``gateways.config_json`` without a redeploy.

    TODO(docs): confirm against https://docs.geidea.net/docs/geidea-checkout-v2.md
    (still unreachable from this environment; see docs/geidea/00-README.md for
    what is and is not covered by the fetched set).
    """
    message = "".join(str(values.get(name, "")) for name in fields)
    return _hmac_b64(api_password, message)


# ---------------------------------------------------------------------------
# 2. MIT signature -- VERIFIED
# ---------------------------------------------------------------------------

def mit_signature(
    *, public_key: str, session_id: str, timestamp_value: str, api_password: str
) -> str:
    """
    base64(HMAC-SHA256(apiPassword, publicKey + sessionId + timestamp)).

    Source: docs/geidea/02-merchant-initiated-mit.md, "MIT Signature Hashing
    Steps", corroborated by three reference implementations (PHP, C#, Python)
    that agree on the field order and the straight concatenation with no
    separators.

    Applies ONLY to the pay/token call in MIT step 2.2. The session-create call
    that precedes it uses the base signature above -- the doc is explicit that
    these are different, and mixing them yields a rejection that reads like
    "MIT does not work".
    """
    return _hmac_b64(api_password, f"{public_key}{session_id}{timestamp_value}")


# ---------------------------------------------------------------------------
# 3. Save-card signature -- VERIFIED field list, inferred construction
# ---------------------------------------------------------------------------

def save_card_signature(
    *, timestamp_value: str, public_key: str, currency: str, api_password: str
) -> str:
    """
    base64(HMAC-SHA256(apiPassword, timeStamp + publicKey + currency)).

    Source: docs/geidea/03-standalone-save-card.md, "Signature Hashing Steps".
    Note the order: timestamp FIRST, then public key, then currency -- the
    reverse of where the public key sits in the MIT and callback formulas.

    The field list and order are taken verbatim from the doc. The HMAC
    construction is read from "Hash (SHA-256) ... keyed by MerchantAPIPassword",
    the same wording the callback page uses and demonstrates as HMAC-SHA256 in
    its reference code. This page ships no reference code of its own, so that
    one step is inference from consistent wording rather than from an example.
    """
    return _hmac_b64(api_password, f"{timestamp_value}{public_key}{currency}")


# ---------------------------------------------------------------------------
# 4. Callback verification signature -- VERIFIED
# ---------------------------------------------------------------------------

def callback_signature(
    *,
    public_key: str,
    amount: str,
    currency: str,
    order_id: str,
    status: str,
    merchant_reference_id: str,
    timestamp_value: str,
    api_password: str,
) -> str:
    """
    base64(HMAC-SHA256(apiPassword, publicKey + amount + currency + orderId
                                    + status + merchantReferenceId + timeStamp)).

    Source: docs/geidea/04-webhook-callback-notifications.md, which ships a
    Python reference implementation.

    That page notes it gives no fully worked numeric example, and asks for the
    result to be checked against one real sandbox callback before being relied
    on. Until that check happens, a verification failure is reported as
    "invalid" and the callback is stored but not acted on -- which is the safe
    direction to be wrong in, and is visible in the callbacks view.
    """
    message = (
        f"{public_key}{amount}{currency}{order_id}{status}"
        f"{merchant_reference_id}{timestamp_value}"
    )
    return _hmac_b64(api_password, message)


def verify_callback_signature(expected: str, received: str) -> bool:
    """
    Constant-time comparison.

    ``compare_digest`` rather than ``==``: a byte-by-byte comparison leaks how
    much of a forged signature was correct through its timing. The Geidea docs
    do not specify this; it is a requirement of this codebase.
    """
    if not expected or not received:
        return False
    return hmac.compare_digest(expected, received)


class BasicAuth:
    """
    HTTP Basic authentication, applied by the shared client (§0.3).

    Implemented here rather than handed to httpx so authentication and signing
    are one strategy object the shared client applies uniformly, and so the
    credential never reaches a code path that might log it -- the client masks
    the ``Authorization`` header before writing the log row.
    """

    def __init__(self, public_key: str, api_password: str) -> None:
        self._public_key = public_key
        self._api_password = api_password

    def apply(
        self, *, method: str, url: str, headers: dict[str, str], body: Any
    ) -> tuple[dict[str, str], Any]:
        token = base64.b64encode(
            f"{self._public_key}:{self._api_password}".encode()
        ).decode()
        headers = {
            "Content-Type": "application/json",
            **headers,
            "Authorization": f"Basic {token}",
        }
        return headers, body

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let a traceback print the API password.
        return "<GeideaBasicAuth public_key=***>"
