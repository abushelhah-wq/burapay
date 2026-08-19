"""
Application settings.

Everything here comes from the environment. Gateway credentials do *not* live here:
they are configured at runtime through the Settings page and stored encrypted in the
database (see ``app.core.crypto`` and ``app.services.credentials``), so that rotating
a sandbox key never means editing a file or restarting a container.

The three variables that must be set in any real deployment are ``APP_SECRET_KEY``,
``ENCRYPTION_KEY`` and ``DATABASE_URL``. The first two have development fallbacks so
``pytest`` and ``docker compose up`` work out of the box; both are refused when
``APP_ENV=production``, because a shared default key is the same as no key at all.
"""

from __future__ import annotations

import functools
from typing import List, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated

#: Only these are allowed as a gateway environment until production is explicitly
#: unlocked. Section 26 of the specification: sandbox/test only by default.
Environment = Literal["sandbox", "production"]

DEV_SECRET_KEY = "dev-only-insecure-secret-key-change-me"
#: A valid Fernet key (32 url-safe base64 bytes) used only when ENCRYPTION_KEY is unset.
DEV_ENCRYPTION_KEY = "ZGV2LW9ubHktaW5zZWN1cmUtZmVybmV0LWtleS0zMmJ5dGU="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # -- identity --------------------------------------------------------- #
    app_name: str = "BuraPay Gateway Benchmark"
    app_version: str = "1.0.0"
    app_env: str = Field(default="development")

    # -- security --------------------------------------------------------- #
    app_secret_key: str = Field(default=DEV_SECRET_KEY)
    encryption_key: str = Field(default=DEV_ENCRYPTION_KEY)
    access_token_expire_minutes: int = 720
    jwt_algorithm: str = "HS256"

    #: Created on first boot when no user exists at all. Changing these after the
    #: first boot does nothing — the account already exists.
    bootstrap_admin_email: str = "admin@busrapay.com"
    bootstrap_admin_password: str = "ChangeMe!123"

    # -- database --------------------------------------------------------- #
    database_url: str = Field(
        default="postgresql+asyncpg://burapay:burapay@localhost:5432/burapay")

    # -- deployment ------------------------------------------------------- #
    #: Public HTTPS origin. Gateways redirect the browser back to it and POST
    #: webhooks to it, and every sandbox rejects non-HTTPS values for both.
    public_base_url: str = "https://busrapay.com"
    #: NoDecode keeps pydantic-settings from trying to JSON-parse this. Without it,
    #: the documented ``CORS_ORIGINS=https://busrapay.com`` fails at start-up because
    #: a bare URL is not valid JSON.
    cors_origins: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"])

    # -- benchmarking ----------------------------------------------------- #
    #: Safe default rate limit for automated runs (specification section 16).
    benchmark_min_interval_seconds: float = 2.0
    benchmark_max_transactions_per_run: int = 500
    benchmark_max_concurrency: int = 1
    http_timeout_seconds: float = 30.0

    #: Guard rail for section 26. Nothing may target a production gateway
    #: environment until this is explicitly turned on.
    allow_production_gateways: bool = False

    #: Direct-API flows that accept a PAN typed into the browser put this app in PCI
    #: scope. Off by default; only ever appropriate with sandbox test cards.
    allow_direct_card_entry: bool = False

    log_level: str = "INFO"
    server_region: Optional[str] = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("production", "prod")

    @property
    def sync_database_url(self) -> str:
        """The same database, for Alembic, which runs synchronously."""
        return self.database_url.replace("+asyncpg", "").replace("+aiosqlite", "")

    def insecure_defaults(self) -> List[str]:
        """Which secrets are still at their development values."""
        problems = []
        if self.app_secret_key == DEV_SECRET_KEY:
            problems.append("APP_SECRET_KEY")
        if self.encryption_key == DEV_ENCRYPTION_KEY:
            problems.append("ENCRYPTION_KEY")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.insecure_defaults():
        raise RuntimeError(
            "Refusing to start in production with development secrets still in place: "
            + ", ".join(settings.insecure_defaults())
            + ". Generate real values (see .env.example) before deploying.")
    return settings


settings = get_settings()
