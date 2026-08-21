"""Settings-page and auth models (§7b)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiModel


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class UserOut(ApiModel):
    id: int
    email: str
    is_admin: bool
    is_active: bool
    last_login_at: Optional[datetime] = None


class CredentialIn(BaseModel):
    """
    One credential to store, encrypted.

    ``is_production`` is the flag ``ALLOW_PRODUCTION_GATEWAYS`` gates on. It is
    stored per credential rather than per gateway so a merchant can hold both
    sandbox and live keys without the live ones being reachable.
    """

    model_config = ConfigDict(extra="forbid")

    key_name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=4096)
    is_production: bool = False


class CredentialOut(ApiModel):
    """
    A stored credential, masked. The full value is never returned by any
    endpoint, to any caller, including an administrator (§7b).
    """

    key_name: str
    masked_value: str
    is_production: bool
    #: False when the stored ciphertext cannot be decrypted with the current
    #: ENCRYPTION_KEY -- which needs a different fix from "not configured".
    readable: bool = True
    updated_at: Optional[datetime] = None
    updated_by_user_id: Optional[int] = None


class GatewayCredentialsOut(ApiModel):
    gateway_code: str
    display_name: str
    required_credentials: list[str] = Field(default_factory=list)
    optional_credentials: list[str] = Field(default_factory=list)
    credentials: list[CredentialOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class GatewayConfigIn(BaseModel):
    """Non-secret adapter configuration. Never accepts a credential."""

    model_config = ConfigDict(extra="forbid")

    config: dict[str, str] = Field(default_factory=dict)
    enabled: Optional[bool] = None


class RuntimeFlags(ApiModel):
    """
    The server-side gates, reported so the UI can explain itself.

    These are a *reflection* of what the backend enforces, never the
    enforcement itself -- every one of them is checked again server-side on the
    request that matters.
    """

    allow_direct_card_entry: bool
    allow_production_gateways: bool
    benchmark_min_interval_seconds: int
    benchmark_max_transactions_per_run: int
    http_timeout_seconds: float
    public_base_url: str
    public_base_is_https: bool
    measurement_location: Optional[str] = None


class BenchmarkRunOut(ApiModel):
    id: int
    gateway_code: str
    operation: str
    integration_type: Optional[str] = None
    requested_count: int
    completed_count: int
    status: str
    #: Always populated on a run that did not finish -- §7b requires a stop to
    #: be visible rather than silent.
    stop_reason: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
