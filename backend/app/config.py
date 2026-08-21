"""
Application settings.

Every value here comes from an environment variable documented in
``.env.example`` at the repository root. That file is the contract: this module
never invents a variable that is not documented there, and never reads a
gateway credential -- those live encrypted in ``gateway_credentials`` and are
resolved through :mod:`app.security.credentials` at call time (§7b).

Two rules this module exists to enforce:

* ``PUBLIC_BASE_URL`` is the single source of every return-URL and callback-URL
  sent to a gateway. Nothing in this codebase may hardcode a domain --
  ``tests/test_no_hardcoded_domain.py`` greps for it.
* The safety flags (``ALLOW_PRODUCTION_GATEWAYS``, ``ALLOW_DIRECT_CARD_ENTRY``,
  ``BENCHMARK_*``) are read here and enforced server-side. They are not UI
  hints; see :mod:`app.security.gates` and :mod:`app.services.benchmark`.
"""

from __future__ import annotations

import functools
import os
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Accepted spellings for a true boolean. The repository's existing
#: ``.env.example`` uses ``0``/``1`` (e.g. ``ALLOW_DIRECT_CARD_ENTRY=1``) while
#: §7b describes the same flags as ``true``/``false``; both must work, so a
#: deployment is never silently mis-read as "off".
_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off", ""}


def parse_bool(value: object, *, default: bool = False) -> bool:
    """Parse a boolean env var, accepting both ``1/0`` and ``true/false``."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"cannot parse {value!r} as a boolean")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,          # compose injects the environment; no file reads
        extra="ignore",
        case_sensitive=False,
    )

    # -- identity / crypto -------------------------------------------------
    # Signs session cookies. A rotation logs everyone out; it never touches
    # stored credentials, which are keyed on ENCRYPTION_KEY instead.
    APP_SECRET_KEY: str = ""
    # Fernet key encrypting gateway_credentials at rest. There is exactly one
    # such key and one consumer (CredentialStore). Never derive a second key
    # from it and never add a second variable for the same purpose (§7b).
    ENCRYPTION_KEY: str = ""

    # Used only to create the first admin when the users table is empty. Read
    # on every startup, but acted on only in that one case -- an existing admin
    # is never reset from the environment. See services/bootstrap.py.
    BOOTSTRAP_ADMIN_EMAIL: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""

    # -- database ----------------------------------------------------------
    POSTGRES_USER: str = "burapay"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = "burapay"
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432
    # Set explicitly only for non-Docker local runs; compose builds it from the
    # POSTGRES_* values above.
    DATABASE_URL: str = ""

    REDIS_URL: str = "redis://redis:6379/0"

    # -- public surface ----------------------------------------------------
    PUBLIC_BASE_URL: str = ""
    CORS_ORIGINS: str = ""
    DOMAIN: str = ""

    # -- safety gates (all enforced server-side) ---------------------------
    ALLOW_PRODUCTION_GATEWAYS: bool = False
    ALLOW_DIRECT_CARD_ENTRY: bool = False
    BENCHMARK_MIN_INTERVAL_SECONDS: int = 60
    BENCHMARK_MAX_TRANSACTIONS_PER_RUN: int = 50

    # -- gateway HTTP ------------------------------------------------------
    # The single default timeout for the shared client (§0.3). A per-gateway
    # override is allowed only where that gateway's documentation states a
    # different required value, and must be commented as a deviation.
    HTTP_TIMEOUT_SECONDS: float = 30.0

    # -- benchmark context -------------------------------------------------
    # Latency numbers are meaningless without knowing where they were measured.
    MEASUREMENT_LOCATION: str = ""
    TEST_AMOUNT: str = "10.00"
    TEST_CURRENCY: str = "SAR"

    LOG_LEVEL: str = "INFO"

    @field_validator(
        "ALLOW_PRODUCTION_GATEWAYS", "ALLOW_DIRECT_CARD_ENTRY", mode="before"
    )
    @classmethod
    def _coerce_bool(cls, value: object) -> bool:
        return parse_bool(value)

    @model_validator(mode="after")
    def _derive_database_url(self) -> "Settings":
        if not self.DATABASE_URL:
            self.DATABASE_URL = (
                f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        return self

    # -- derived -----------------------------------------------------------

    @property
    def async_database_url(self) -> str:
        """DATABASE_URL forced onto the asyncpg driver."""
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sync_database_url(self) -> str:
        """DATABASE_URL forced onto a sync driver, for Alembic."""
        url = self.DATABASE_URL
        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def public_base_url(self) -> str:
        """
        The base for every return/callback URL handed to a gateway.

        Trailing slashes are stripped so callers can join paths without
        producing a double slash that some gateways reject on exact-match
        return-URL validation.
        """
        return self.PUBLIC_BASE_URL.rstrip("/")

    @property
    def public_base_is_https(self) -> bool:
        return urlparse(self.public_base_url).scheme == "https"

    def callback_url(self, path: str) -> str:
        """
        Build an absolute URL under PUBLIC_BASE_URL.

        This is the ONLY way a gateway-facing URL is constructed anywhere in the
        codebase, so a staging deploy on another domain works unmodified.
        """
        if not self.public_base_url:
            raise MissingConfiguration(
                "PUBLIC_BASE_URL is not set, so no return or callback URL can be "
                "built. Set it to the https:// origin this deployment answers on."
            )
        return f"{self.public_base_url}/{path.lstrip('/')}"


class MissingConfiguration(RuntimeError):
    """Raised when a required setting is absent at the point it is needed."""


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache. Used by tests that patch the environment."""
    get_settings.cache_clear()
    return get_settings()
