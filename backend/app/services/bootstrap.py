"""
First-boot setup: seed the gateway catalogue, the default settings and the admin user.

Runs on every startup and is idempotent. Gateway rows are refreshed from the adapter
registry each time, so a capability that changes in code (a gateway gaining HPP
support, say) shows up without a manual database edit — while ``enabled`` and any
stored credentials are left exactly as the operator set them.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.scoring import DEFAULT_WEIGHTS
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.gateways.registry import all_descriptors
from app.db.base import utcnow
from app.models import AppSetting, Gateway, User, UserRole, UserStatus

logger = get_logger(__name__)

DEFAULT_SETTINGS: Dict[str, Dict[str, Any]] = {
    "scoring_weights": {
        "value": dict(DEFAULT_WEIGHTS),
        "description": "Internal Benchmark Score weights. Must be non-negative; normalised to 1.",
    },
    "benchmark_limits": {
        "value": {
            "min_interval_seconds": settings.benchmark_min_interval_seconds,
            "max_transactions_per_run": settings.benchmark_max_transactions_per_run,
            "max_concurrency": settings.benchmark_max_concurrency,
        },
        "description": "Rate limits applied to automated benchmark runs.",
    },
    "test_defaults": {
        "value": {"amount": 1.00, "currency": "SAR",
                  "description": "BuraPay benchmark transaction"},
        "description": "Defaults pre-filled on the Run Benchmark form.",
    },
    "test_card": {
        "value": {"number": "4111111111111111", "month": "12", "year": "2030",
                  "cvc": "123", "holder": "BuraPay Benchmark"},
        "description": ("Sandbox test card used by Direct API flows. Never a real card; "
                        "never stored against a transaction."),
    },
}


async def seed_gateways(session: AsyncSession) -> None:
    existing = {row.code: row for row in (await session.execute(select(Gateway))).scalars()}
    for descriptor in all_descriptors():
        row = existing.get(descriptor["code"])
        if row is None:
            session.add(Gateway(enabled=True, **descriptor))
            continue
        # Refresh what the code owns; leave what the operator owns.
        row.name = descriptor["name"]
        row.supports_hpp = descriptor["supports_hpp"]
        row.supports_direct = descriptor["supports_direct"]
        row.supported_currencies = descriptor["supported_currencies"]
        row.logo_slug = descriptor["logo_slug"]
        row.display_order = descriptor["display_order"]
        row.docs_url = descriptor["docs_url"]
        row.notes = descriptor["notes"]
    await session.commit()


async def seed_settings(session: AsyncSession) -> None:
    existing = {row.key for row in (await session.execute(select(AppSetting))).scalars()}
    for key, payload in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(AppSetting(key=key, value=payload["value"],
                                   description=payload["description"]))
    await session.commit()


async def seed_admin(session: AsyncSession) -> None:
    """Create the first administrator, but only when the platform has no users at all.

    Specification section 10: the initial administrator comes from the environment or
    the CLI, never from a hard-coded account in source. Three properties make this
    safe to run on every start-up:

    * It only ever *creates*. Changing ``BOOTSTRAP_ADMIN_PASSWORD`` after the first
      boot does nothing — silently resetting the administrator's password on every
      restart would be a back door with an environment variable for a key.
    * The password is hashed before it is stored, like every other password.
    * Nothing here logs the password. The warning names the account and says to change
      it; it does not say what it is.
    """
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if count:
        return
    session.add(User(username=settings.bootstrap_admin_username.strip().lower(),
                     email=settings.bootstrap_admin_email.strip().lower(),
                     full_name="BuraPay Administrator",
                     hashed_password=hash_password(settings.bootstrap_admin_password),
                     role=UserRole.ADMIN.value, status=UserStatus.ACTIVE.value,
                     password_changed_at=utcnow()))
    await session.commit()
    logger.warning(
        "initial administrator created from the environment — sign in and change this "
        "password now",
        extra={"operation": "bootstrap", "status": "created",
               "username": settings.bootstrap_admin_username,
               "email": settings.bootstrap_admin_email})


async def bootstrap(session: AsyncSession) -> None:
    await seed_gateways(session)
    await seed_settings(session)
    await seed_admin(session)


async def get_setting(session: AsyncSession, key: str) -> Dict[str, Any]:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))
           ).scalar_one_or_none()
    if row is not None:
        return dict(row.value or {})
    return dict(DEFAULT_SETTINGS.get(key, {}).get("value", {}))


async def set_setting(session: AsyncSession, key: str, value: Dict[str, Any],
                      updated_by: str | None = None) -> Dict[str, Any]:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))
           ).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=key, value=value,
                         description=DEFAULT_SETTINGS.get(key, {}).get("description"),
                         updated_by=updated_by)
        session.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
    await session.commit()
    return value
