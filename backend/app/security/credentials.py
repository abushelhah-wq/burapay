"""
CredentialStore -- the single path to a gateway credential (§7b).

Rules this module exists to enforce:

* Credentials live in ``gateway_credentials`` as Fernet ciphertext. Plaintext
  is never written to any table, log, seed script or migration.
* Adapters resolve credentials **through this store, at call time**. No adapter
  reads ``os.environ``, and no plaintext copy outlives the request that needed
  it -- :meth:`resolve` returns a plain dict that the request drops on
  completion.
* Display is masked to the last four characters. The full value is never
  returned to any HTTP response, including to an admin.

Seeding: on first start, any gateway credential present in the environment
(``GEIDEA_PUBLIC_KEY`` and friends, the shape the previous release used) is
imported into the table encrypted, so an existing ``.env`` keeps working
unchanged. Seeding never overwrites a value already in the table -- the
Settings page is authoritative once a human has touched it.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import get_logger
from app.models import Gateway, GatewayCredential
from app.security.crypto import CredentialDecryptionError, decrypt, encrypt
from app.timeutil import utcnow

logger = get_logger(__name__)


def mask_for_display(value: str, keep: int = 4) -> str:
    """
    Render a credential for the Settings page: last ``keep`` characters only.

    §7b asks for a masked display showing the last 4. Short values are fully
    masked rather than partially revealed.
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * max(4, len(value) - keep)}{value[-keep:]}"


class CredentialStore:
    """Reads and writes encrypted gateway credentials."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- read -------------------------------------------------------------

    async def resolve(self, gateway_id: int) -> dict[str, str]:
        """
        Decrypt every credential for a gateway.

        Called once per operation, immediately before the adapter runs. The
        returned dict is the plaintext copy §7b permits: scoped to this
        request, not cached, not stored on any long-lived object.
        """
        rows = await self._rows(gateway_id)
        out: dict[str, str] = {}
        for row in rows:
            try:
                out[row.key_name] = decrypt(row.encrypted_value)
            except CredentialDecryptionError:
                # One unreadable credential must not make the others
                # unavailable; the adapter will report the specific key as
                # missing, which is the actionable message.
                logger.error(
                    "credential could not be decrypted",
                    extra={"gateway_id": gateway_id, "key_name": row.key_name},
                )
        return out

    async def is_production(self, gateway_id: int) -> bool:
        """True when *any* stored credential for this gateway is live-flagged."""
        rows = await self._rows(gateway_id)
        return any(row.is_production for row in rows)

    async def describe(self, gateway_id: int) -> list[dict[str, object]]:
        """Masked view for the Settings page. Never returns a full value."""
        rows = await self._rows(gateway_id)
        described: list[dict[str, object]] = []
        for row in rows:
            try:
                masked = mask_for_display(decrypt(row.encrypted_value))
                readable = True
            except CredentialDecryptionError:
                masked = "(undecryptable - ENCRYPTION_KEY changed)"
                readable = False
            described.append({
                "key_name": row.key_name,
                "masked_value": masked,
                "is_production": row.is_production,
                "readable": readable,
                "updated_at": row.updated_at,
                "updated_by_user_id": row.updated_by_user_id,
            })
        return sorted(described, key=lambda item: str(item["key_name"]))

    async def _rows(self, gateway_id: int) -> list[GatewayCredential]:
        result = await self.session.execute(
            select(GatewayCredential).where(GatewayCredential.gateway_id == gateway_id)
        )
        return list(result.scalars().all())

    # -- write ------------------------------------------------------------

    async def put(
        self,
        *,
        gateway_id: int,
        key_name: str,
        value: str,
        is_production: bool = False,
        user_id: Optional[int] = None,
    ) -> GatewayCredential:
        """Insert or update one credential, encrypting on the way in."""
        existing = await self.session.execute(
            select(GatewayCredential).where(
                GatewayCredential.gateway_id == gateway_id,
                GatewayCredential.key_name == key_name,
            )
        )
        row = existing.scalar_one_or_none()
        ciphertext = encrypt(value)
        if row is None:
            row = GatewayCredential(
                gateway_id=gateway_id,
                key_name=key_name,
                encrypted_value=ciphertext,
                is_production=is_production,
                updated_by_user_id=user_id,
            )
            self.session.add(row)
        else:
            row.encrypted_value = ciphertext
            row.is_production = is_production
            row.updated_by_user_id = user_id
            row.updated_at = utcnow()
        await self.session.flush()
        # Logged without the value, and without the masked value either -- the
        # audit fact is "this key changed", not what it changed to.
        logger.info(
            "gateway credential updated",
            extra={"gateway_id": gateway_id, "key_name": key_name,
                   "is_production": is_production, "user_id": user_id},
        )
        return row

    async def delete(self, *, gateway_id: int, key_name: str) -> bool:
        existing = await self.session.execute(
            select(GatewayCredential).where(
                GatewayCredential.gateway_id == gateway_id,
                GatewayCredential.key_name == key_name,
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        logger.info(
            "gateway credential deleted",
            extra={"gateway_id": gateway_id, "key_name": key_name},
        )
        return True

    # -- seeding ----------------------------------------------------------

    async def seed_from_environment(
        self, gateway: Gateway, env_keys: Mapping[str, str]
    ) -> list[str]:
        """
        Import credentials from the environment on first start.

        ``env_keys`` maps environment variable name -> credential key name.
        Existing rows are left alone: once a human has saved a value on the
        Settings page, the environment no longer overrides it. Returns the
        credential key names that were seeded.
        """
        present = {row.key_name for row in await self._rows(gateway.id)}
        seeded: list[str] = []
        for env_name, key_name in env_keys.items():
            if key_name in present:
                continue
            value = os.environ.get(env_name, "").strip()
            if not value:
                continue
            await self.put(
                gateway_id=gateway.id, key_name=key_name, value=value,
                is_production=False, user_id=None,
            )
            seeded.append(key_name)
        if seeded:
            logger.info(
                "seeded gateway credentials from environment",
                extra={"gateway": gateway.code, "keys": seeded},
            )
        return seeded


def missing_keys(credentials: Mapping[str, str], required: Iterable[str]) -> list[str]:
    return [key for key in required if not credentials.get(key)]
