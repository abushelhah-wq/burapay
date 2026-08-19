"""
Sanitization — the last thing that runs before anything is logged or persisted.

Two separate jobs, both mandatory (specification sections 24, 25, 43):

* **Secrets** never reach storage. Authorization headers, API keys, webhook secrets
  and bearer tokens are replaced with ``[redacted]`` by key name, at any depth.
* **Cardholder data** never reaches storage. PANs are truncated to their last four
  digits, CVVs are dropped entirely, and any digit run that looks like a card number
  is masked even when it appears in free text such as an error message.

The rule is deny-by-key plus a value-level PAN sweep, so a gateway that returns a
secret under a field name nobody anticipated still gets caught by the sweep, and a
field named ``cvv`` is dropped no matter what it contains.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

REDACTED = "[redacted]"

#: Field names whose values are always removed. Matched case-insensitively against
#: the key with separators stripped, so ``api-key``/``api_key``/``APIKey`` all hit.
SECRET_KEY_PARTS = frozenset({
    "authorization", "proxyauthorization", "apikey", "xapikey", "apipassword",
    "secret", "secretkey", "clientsecret", "webhooksecret", "hmackey", "signature",
    "password", "passwd", "accesstoken", "token", "refreshtoken", "idtoken",
    "bearer", "privatekey", "publishablekey", "secrettoken", "credential",
    "credentials", "cookie", "setcookie", "entityid",
})

#: Deliberately NOT treated as secrets: values a gateway mints specifically to be
#: handed to its own browser SDK. Adyen's ``sessionData`` is meaningless without the
#: session it belongs to and is designed to reach the browser, so redacting it would
#: break the Drop-in flow while protecting nothing.
CLIENT_SIDE_FIELDS = frozenset({"sessiondata", "clientkey"})

#: Card fields that must never be stored in any form.
CARD_DROP_PARTS = frozenset({
    "cvv", "cvc", "cvv2", "cid", "securitycode", "cardsecuritycode", "track",
    "track1", "track2", "trackdata", "pin", "pinblock",
})

#: Card number fields, kept only as a last-four.
PAN_PARTS = frozenset({
    "cardnumber", "number", "pan", "accountnumber", "primaryaccountnumber",
    "sourcenumber", "cardnum",
})

#: 13–19 digits, optionally separated by spaces or dashes — a PAN in free text.
PAN_PATTERN = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _normalize(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_secret(key: str) -> bool:
    return _normalize(key) in SECRET_KEY_PARTS


def _is_card_drop(key: str) -> bool:
    return _normalize(key) in CARD_DROP_PARTS


def _is_pan_field(key: str) -> bool:
    return _normalize(key) in PAN_PARTS


def last_four(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"


def _luhn_ok(digits: str) -> bool:
    """Luhn check, so ordinary long numbers (timestamps, references) survive intact."""
    total, alternate = 0, False
    for char in reversed(digits):
        digit = ord(char) - 48
        if alternate:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        alternate = not alternate
    return total % 10 == 0


def scrub_text(text: str) -> str:
    """Mask anything in free text that passes as a card number."""
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return last_four(digits)
        return match.group(0)

    return PAN_PATTERN.sub(replace, text)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Recursively sanitize a JSON-ish structure. Safe to call on anything."""
    if _depth > 12:
        return "[truncated]"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_card_drop(name):
                continue
            if _is_secret(name):
                out[name] = REDACTED
            elif _is_pan_field(name) and isinstance(item, (str, int)):
                out[name] = last_four(str(item))
            else:
                out[name] = sanitize(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def sanitize_headers(headers: Iterable[tuple[str, str]] | Dict[str, str]) -> Dict[str, str]:
    """Header maps get the same treatment, with every auth header redacted."""
    items = headers.items() if isinstance(headers, dict) else headers
    return {key: (REDACTED if _is_secret(key) else scrub_text(str(value)))
            for key, value in items}


def sanitize_snippet(text: str, limit: int = 2000) -> str:
    """A response excerpt kept for debugging: scrubbed, then truncated."""
    if not text:
        return ""
    cleaned = scrub_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…[truncated]"


def redact_secret_values(text: str, secrets: List[str]) -> str:
    """Remove known secret literals that a gateway echoed back into a message."""
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, REDACTED)
    return text
