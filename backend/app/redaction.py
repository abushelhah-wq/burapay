"""
Masking and redaction (§0.7, §0.8).

Two different jobs, deliberately kept separate:

* **Masking** applies to *secrets we own* -- API keys, merchant IDs, webhook
  secrets, Authorization headers. They are shown as first-4/last-4 so a human
  can confirm *which* credential was used without the value being usable.
* **Redaction** applies to *cardholder data we must never retain* -- PAN, CVV,
  track data, PIN blocks. These are destroyed, not masked. The single exception
  is the last four digits of a PAN, which §0.8 explicitly permits and which is
  the only way to tie a log line back to a test card.

Everything written to ``api_call_logs.request_headers_json``,
``request_body_json``, ``response_headers_json`` and ``response_body_json``
passes through :func:`redact_headers` / :func:`redact_body` first. That is
enforced in one place -- ``app/gateways/http_client.py`` -- so no adapter can
forget to do it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------
# Key classification
# --------------------------------------------------------------------------

def _norm(key: str) -> str:
    """Normalise a key for matching: lowercase, strip separators."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


#: Keys whose value is a full card number. Matched on the normalised key.
_PAN_KEYS = frozenset({
    "cardnumber", "pan", "primaryaccountnumber", "accountnumber",
    "number", "cardno", "ccnumber", "creditcardnumber",
})

#: Keys whose value is a card verification code. Destroyed outright.
_CVV_KEYS = frozenset({
    "cvv", "cvc", "cvv2", "cvc2", "csc", "cid",
    "securitycode", "cardsecuritycode", "verificationcode",
})

#: Keys carrying magnetic-stripe / chip / PIN material. Destroyed outright.
_TRACK_KEYS = frozenset({
    "track1", "track2", "track2data", "trackdata", "emvdata",
    "pin", "pinblock", "encryptedpin",
})

#: Keys carrying a secret of ours. Masked to first-4/last-4.
_SECRET_KEY_PATTERNS = (
    "password", "secret", "apikey", "apipassword", "privatekey",
    "authorization", "accesstoken", "refreshtoken", "bearer",
    "clientsecret", "webhooksecret", "signaturekey", "merchantkey",
)

#: Token *references* are safe to store -- they are the whole point of §4g.
#: They are listed explicitly so the generic "*token*" secret rule below does
#: not destroy the identifier we need to charge a saved card later.
_TOKEN_REFERENCE_KEYS = frozenset({
    "tokenid", "tokenreference", "tokenref", "paymenttoken", "cardtoken",
    "storedpaymentmethodid", "registrationid", "agreementid", "customerkey",
    "initiatedby", "subscriptionid",
})

REDACTED_CVV = "[REDACTED:CVV]"
REDACTED_TRACK = "[REDACTED:TRACK-DATA]"


def classify_key(key: str) -> str:
    """
    Return one of ``pan``, ``cvv``, ``track``, ``secret``, ``safe``.

    Exposed so tests can assert the classification directly rather than
    inferring it from redacted output.
    """
    norm = _norm(key)
    if norm in _TOKEN_REFERENCE_KEYS:
        return "safe"
    if norm in _PAN_KEYS:
        return "pan"
    if norm in _CVV_KEYS:
        return "cvv"
    if norm in _TRACK_KEYS:
        return "track"
    if any(pattern in norm for pattern in _SECRET_KEY_PATTERNS):
        return "secret"
    return "safe"


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------

def mask_secret(value: Any, *, keep: int = 4) -> str:
    """
    Mask a secret to its first and last ``keep`` characters (§0.7).

    A value short enough that first-4/last-4 would reveal most of it is
    replaced wholesale -- masking an 8-character secret as ``abcd...efgh``
    would print the entire thing.
    """
    if value is None:
        return ""
    text = str(value)
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * 6}{text[-keep:]}"


# --------------------------------------------------------------------------
# PAN handling
# --------------------------------------------------------------------------

_DIGITS_RE = re.compile(r"\d")
#: A candidate PAN embedded in free text: 13-19 digits, optionally separated by
#: single spaces or hyphens in groups (so "4111 1111 1111 1111" is caught too).
_EMBEDDED_PAN_RE = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")


def luhn_ok(digits: str) -> bool:
    """Standard Luhn check. Used to avoid redacting long non-card numbers."""
    if not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def looks_like_pan(value: Any) -> bool:
    """
    True when a value is plausibly a full card number.

    Length 13-19 and a valid Luhn checksum. Requiring Luhn is what keeps
    16-digit order references and gateway transaction IDs out of the redactor,
    which would otherwise make logs unreadable.
    """
    if isinstance(value, bool) or value is None:
        return False
    digits = "".join(_DIGITS_RE.findall(str(value)))
    if not 13 <= len(digits) <= 19:
        return False
    # Reject values carrying non-separator characters -- "ORDER-4111111111111111X"
    # is an identifier, not a PAN field.
    if not re.fullmatch(r"[\d \-]+", str(value)):
        return False
    return luhn_ok(digits)


def last4(value: Any) -> str:
    digits = "".join(_DIGITS_RE.findall(str(value or "")))
    return digits[-4:] if len(digits) >= 4 else ""


def redact_pan(value: Any) -> str:
    """Replace a PAN with a marker that keeps only the last four digits."""
    tail = last4(value)
    return f"[REDACTED:PAN last4={tail}]" if tail else "[REDACTED:PAN]"


def _scrub_text(text: str) -> str:
    """Redact any PAN-shaped substring inside free text (error strings, HTML)."""
    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digits = "".join(_DIGITS_RE.findall(candidate))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return f"[REDACTED:PAN last4={digits[-4:]}]"
        return candidate

    return _EMBEDDED_PAN_RE.sub(replace, text)


# --------------------------------------------------------------------------
# Recursive structure redaction
# --------------------------------------------------------------------------

_MAX_DEPTH = 12
_MAX_TEXT = 20_000


def redact_body(value: Any, *, _depth: int = 0) -> Any:
    """
    Recursively redact a decoded JSON body before it is written to the log store.

    Applies, in order:

    1. Key-based rules -- a key named ``cvv`` is destroyed whatever it holds.
    2. Value-based rules -- a PAN-shaped, Luhn-valid value is redacted even
       under a key we did not anticipate, because a gateway is free to nest
       card data anywhere and §0.8 does not have an "unless we did not expect
       it" clause.
    3. Free-text scanning -- PANs embedded in error messages or HTML bodies.
    """
    if _depth > _MAX_DEPTH:
        return "[REDACTED:MAX-DEPTH]"

    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            kind = classify_key(key)
            if kind == "cvv":
                out[str(key)] = REDACTED_CVV
            elif kind == "track":
                out[str(key)] = REDACTED_TRACK
            elif kind == "pan":
                out[str(key)] = redact_pan(item)
            elif kind == "secret":
                out[str(key)] = mask_secret(item)
            else:
                out[str(key)] = redact_body(item, _depth=_depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        return [redact_body(item, _depth=_depth + 1) for item in value]

    if isinstance(value, str):
        if looks_like_pan(value):
            return redact_pan(value)
        text = value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "...[truncated]"
        return _scrub_text(text)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if looks_like_pan(value):
            return redact_pan(value)
        return value

    return value


#: Header names masked wholesale regardless of the generic key rules. Cookies
#: are included because a session cookie is a bearer credential.
_SENSITIVE_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-apikey", "apikey", "api-key", "x-auth-token",
    "x-signature", "signature", "x-webhook-signature",
})


def redact_headers(headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None) -> dict[str, str]:
    """
    Mask sensitive headers (§0.7).

    Signature headers are masked rather than dropped: whether a signature was
    *present* is diagnostic information worth keeping, while its value is not
    something that needs to survive in a log table.
    """
    if headers is None:
        return {}
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, str] = {}
    for key, value in items:
        name = str(key)
        if name.lower() in _SENSITIVE_HEADERS or classify_key(name) == "secret":
            out[name] = mask_secret(value)
        else:
            out[name] = str(value)
    return out
