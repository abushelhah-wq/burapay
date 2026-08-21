"""
Symmetric encryption for stored credentials (§7b).

One key (``ENCRYPTION_KEY``), one consumer (:mod:`app.security.credentials`),
one algorithm (Fernet: AES-128-CBC with an HMAC-SHA256 authentication tag).

No key is ever *derived* from ``ENCRYPTION_KEY`` and no second variable is
introduced for the same purpose. If the key is rotated, existing ciphertext
becomes unreadable and the affected credentials must be re-entered -- which is
the correct behaviour, and why :func:`decrypt` reports a decryption failure
distinctly rather than returning an empty string that would look like "not
configured".
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class EncryptionNotConfigured(RuntimeError):
    """ENCRYPTION_KEY is absent or unusable."""


class CredentialDecryptionError(RuntimeError):
    """
    Stored ciphertext could not be decrypted with the current key.

    Almost always means ENCRYPTION_KEY changed. Deliberately distinct from
    "no credential stored", because the remedies differ: restore the old key,
    versus enter the credential for the first time.
    """


def _normalise_key(raw: str) -> bytes:
    """
    Accept a proper Fernet key, or derive one deterministically from a
    passphrase.

    A urlsafe-base64 32-byte value is used as-is -- that is what
    ``Fernet.generate_key()`` produces and what ``.env.example`` tells the
    operator to generate. Anything else is hashed to 32 bytes so a deployment
    that was handed a plain passphrase still encrypts correctly rather than
    refusing to boot. The hash is over the passphrase only; it is not a second
    key with an independent lifetime.
    """
    candidate = raw.strip()
    if not candidate:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        decoded = base64.urlsafe_b64decode(candidate.encode())
        if len(decoded) == 32:
            return candidate.encode()
    except Exception:
        pass
    digest = hashlib.sha256(candidate.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_normalise_key(get_settings().ENCRYPTION_KEY))


def encrypt(plaintext: str) -> str:
    """Encrypt a credential value for storage in ``gateway_credentials``."""
    if plaintext is None:
        raise ValueError("refusing to encrypt None")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a stored credential. Raises rather than returning a blank."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Stored credential could not be decrypted with the current "
            "ENCRYPTION_KEY. If the key was rotated, restore the previous "
            "value or re-enter the credential on the Settings page."
        ) from exc


def is_configured() -> bool:
    try:
        _fernet()
        return True
    except Exception:
        return False
