"""
Credential encryption.

Gateway secrets are stored as one encrypted blob per (gateway, environment) row.
The blob is Fernet (AES-128-CBC + HMAC-SHA256) over the JSON credential map, keyed
from ``ENCRYPTION_KEY``. Consequences that matter:

* The database on its own is useless to an attacker — decryption needs the env var.
* Losing ``ENCRYPTION_KEY`` means losing every stored credential. There is no
  recovery path, by design; re-enter the credentials.
* Nothing is ever decrypted on the way *out* to a client. The API returns masked
  values only (:func:`mask`), and the plaintext exists in memory just long enough to
  sign a gateway request.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class DecryptionError(RuntimeError):
    """The stored blob could not be decrypted with the configured key."""


def _fernet() -> Fernet:
    """Build the Fernet from ENCRYPTION_KEY.

    A proper 32-byte url-safe base64 key is used as-is. Anything else is hashed to
    that shape, so an operator who sets a passphrase gets a working (if weaker) key
    rather than a stack trace at boot.
    """
    raw = settings.encryption_key.encode("utf-8")
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return Fernet(raw)
    except Exception:  # noqa: BLE001 - any decode failure means "not a Fernet key"
        pass
    derived = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(derived)


def encrypt_dict(values: Dict[str, Any]) -> str:
    """Encrypt a credential map into a storable string."""
    payload = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _fernet().encrypt(payload).decode("utf-8")


def decrypt_dict(blob: str) -> Dict[str, Any]:
    """Decrypt a stored blob back into a credential map."""
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode("utf-8")).decode("utf-8"))
    except InvalidToken as exc:
        raise DecryptionError(
            "Stored credentials could not be decrypted. This normally means "
            "ENCRYPTION_KEY changed since they were saved; re-enter them.") from exc


def mask(value: Any, keep: int = 4) -> str:
    """Render a secret for display: ``sk_test_************X92D``.

    Short values are masked completely rather than partly, because revealing four of
    six characters reveals most of the secret.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    prefix_end = text.find("_")
    prefix = text[: prefix_end + 1] if 0 < prefix_end <= 12 else ""
    body = text[len(prefix):]
    return f"{prefix}{'*' * max(4, len(body) - keep)}{body[-keep:]}"


def mask_dict(values: Dict[str, Any], secret_fields: set[str]) -> Dict[str, Any]:
    """Mask the secret fields of a credential map, pass the rest through."""
    return {key: (mask(value) if key in secret_fields else value)
            for key, value in values.items()}
