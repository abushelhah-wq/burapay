"""
First-start bootstrap: reference data, the first admin, and credential seeding.

Runs on every application start and is idempotent. Three jobs:

1. Ensure a ``gateways`` row exists for every adapter in the registry, and an
   ``integration_types`` row for every code in the enum. Reference data, not
   configuration -- an operator never has to create these by hand.
2. Create the first admin **only when the users table is empty** (§7b).
   ``BOOTSTRAP_ADMIN_EMAIL`` / ``BOOTSTRAP_ADMIN_PASSWORD`` are read on every
   start but acted on in that one case, so an admin who later changes their
   password is never silently reset back from the environment.
3. Import gateway credentials from the environment into
   ``gateway_credentials``, encrypted, so an existing ``.env`` from the
   previous release keeps working. Values already in the table are never
   overwritten -- once a human saves on the Settings page, that wins.
"""

from __future__ import annotations

import os
from typing import Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.gateways.registry import ADAPTERS
from app.logging_setup import get_logger
from app.models import Gateway, IntegrationType, IntegrationTypeCode, User
from app.security.auth import hash_password
from app.security.credentials import CredentialStore
from app.security.crypto import is_configured as encryption_configured

logger = get_logger(__name__)

#: Human-readable names for the integration types.
INTEGRATION_TYPE_NAMES: dict[str, str] = {
    IntegrationTypeCode.HPP.value: "Hosted Payment Page",
    IntegrationTypeCode.DIRECT_API.value: "Direct API (server-to-server)",
    IntegrationTypeCode.TOKEN_CIT.value: "Stored card - customer initiated",
    IntegrationTypeCode.TOKEN_MIT.value: "Stored card - merchant initiated",
    IntegrationTypeCode.TOKENIZE.value: "Tokenization (save card)",
}

#: Environment variable -> credential key name, per gateway code. This is the
#: migration path off the previous release's env-var-only credential model:
#: the names on the left are exactly what the existing ``.env.example``
#: documents, so no operator has to rename anything.
ENV_CREDENTIAL_MAP: dict[str, dict[str, str]] = {
    "geidea": {
        "GEIDEA_PUBLIC_KEY": "public_key",
        "GEIDEA_API_PASSWORD": "api_password",
    },
}

#: Environment variable -> ``gateways.config_json`` key. Non-secret adapter
#: configuration only; nothing here is ever a credential.
ENV_CONFIG_MAP: dict[str, dict[str, str]] = {
    "geidea": {
        "GEIDEA_REGION": "region",
        "GEIDEA_API_BASE": "api_base",
    },
}


async def ensure_reference_data(session: AsyncSession) -> None:
    """Create the gateway and integration-type rows every adapter needs."""
    for code, adapter_class in ADAPTERS.items():
        existing = await session.execute(select(Gateway).where(Gateway.code == code))
        gateway = existing.scalar_one_or_none()
        config = _config_from_environment(code)
        if gateway is None:
            gateway = Gateway(
                code=code,
                display_name=adapter_class.display_name or code.title(),
                enabled=True,
                config_json=config,
            )
            session.add(gateway)
            logger.info("registered gateway", extra={"gateway": code})
        else:
            # Merge newly-set environment config without discarding anything an
            # operator changed through the API.
            merged = {**config, **(gateway.config_json or {})}
            if merged != (gateway.config_json or {}):
                gateway.config_json = merged

    for code in IntegrationTypeCode:
        existing = await session.execute(
            select(IntegrationType).where(IntegrationType.code == code.value)
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                IntegrationType(
                    code=code.value,
                    display_name=INTEGRATION_TYPE_NAMES.get(code.value, code.value),
                )
            )
    await session.flush()


def _config_from_environment(gateway_code: str) -> dict[str, str]:
    mapping = ENV_CONFIG_MAP.get(gateway_code, {})
    out: dict[str, str] = {}
    for env_name, key in mapping.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            out[key] = value
    return out


async def ensure_bootstrap_admin(session: AsyncSession) -> None:
    """
    Create the first admin, and only the first (§7b).

    The guard is "the users table is empty", not "this email does not exist" --
    the difference matters. The latter would recreate a deleted admin on every
    restart, and would let anyone who can set an environment variable add an
    account to a running deployment.
    """
    settings = get_settings()
    email = settings.BOOTSTRAP_ADMIN_EMAIL.strip().lower()
    password = settings.BOOTSTRAP_ADMIN_PASSWORD

    count = await session.scalar(select(func.count()).select_from(User))
    if count:
        return
    if not email or not password:
        logger.warning(
            "no users exist and BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD "
            "are not both set, so no administrator was created"
        )
        return

    session.add(
        User(
            email=email,
            password_hash=hash_password(password),
            is_admin=True,
            is_active=True,
        )
    )
    await session.flush()
    logger.info("created bootstrap administrator", extra={"email": email})


async def seed_credentials_from_environment(session: AsyncSession) -> dict[str, list[str]]:
    """
    Import env-var gateway credentials into the encrypted store.

    Skipped entirely when ``ENCRYPTION_KEY`` is unusable -- writing plaintext
    as a fallback would violate §7b, and failing loudly at the Settings page is
    better than a store that silently holds unencrypted secrets.
    """
    if not encryption_configured():
        logger.error(
            "ENCRYPTION_KEY is not usable; skipping credential seeding. "
            "Gateway credentials cannot be stored until it is set."
        )
        return {}

    store = CredentialStore(session)
    seeded: dict[str, list[str]] = {}
    for code, env_keys in ENV_CREDENTIAL_MAP.items():
        result = await session.execute(select(Gateway).where(Gateway.code == code))
        gateway = result.scalar_one_or_none()
        if gateway is None:
            continue
        keys = await store.seed_from_environment(gateway, env_keys)
        if keys:
            seeded[code] = keys
    return seeded


async def run_bootstrap(session: AsyncSession) -> None:
    await ensure_reference_data(session)
    await ensure_bootstrap_admin(session)
    await seed_credentials_from_environment(session)


async def gateway_by_code(session: AsyncSession, code: str) -> Gateway | None:
    result = await session.execute(select(Gateway).where(Gateway.code == code))
    return result.scalar_one_or_none()


async def integration_type_by_code(
    session: AsyncSession, code: str
) -> IntegrationType | None:
    result = await session.execute(
        select(IntegrationType).where(IntegrationType.code == code)
    )
    return result.scalar_one_or_none()


def env_credential_map() -> Mapping[str, Mapping[str, str]]:
    return ENV_CREDENTIAL_MAP
