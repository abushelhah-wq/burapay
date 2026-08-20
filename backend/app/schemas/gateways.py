"""Gateway catalogue and credential schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class CredentialFieldOut(BaseModel):
    key: str
    label: str
    required: bool
    secret: bool
    placeholder: str = ""
    help_text: str = ""
    default: Optional[str] = None
    choices: List[str] = Field(default_factory=list)


class GatewayOut(ORMModel):
    id: str
    code: str
    name: str
    enabled: bool
    supports_hpp: bool
    supports_direct: bool
    supported_currencies: List[str] = Field(default_factory=list)
    logo_slug: Optional[str] = None
    display_order: int = 100
    docs_url: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


class GatewayDetail(GatewayOut):
    configured: bool = False
    #: Which required credential fields are still missing, so the UI can say exactly
    #: what to fill in rather than just "Not Configured".
    missing_fields: List[str] = Field(default_factory=list)
    #: Which credential keys are stored. Key names only — no values, masked or
    #: otherwise. Lets the UI say whether an optional capability such as tokenisation
    #: is ready without exposing anything.
    configured_fields: List[str] = Field(default_factory=list)
    status: str = "Not Configured"
    credential_fields: List[CredentialFieldOut] = Field(default_factory=list)
    documented_calls: Dict[str, str] = Field(default_factory=dict)
    #: Direct API payment modes this gateway implements. A gateway offering more than
    #: one gets a mode selector on the Run Benchmark page.
    supported_payment_modes: List[str] = Field(default_factory=lambda: ["standard"])
    is_simulated: bool = False


class GatewayUpdate(BaseModel):
    enabled: Optional[bool] = None
    supported_currencies: Optional[List[str]] = None
    display_order: Optional[int] = None


class CredentialUpdate(BaseModel):
    """Credential values to store.

    Fields left out are untouched; a masked value echoed back is untouched; ``null``
    clears a field. That is what lets the Settings page render masks safely.
    """

    environment: str = Field(default="sandbox", pattern="^(sandbox|production)$")
    values: Dict[str, Any]


class CredentialsOut(BaseModel):
    gateway_code: str
    environment: str
    #: Always masked. The API has no endpoint that returns a secret in full.
    values: Dict[str, Any] = Field(default_factory=dict)
    configured: bool = False
    missing_fields: List[str] = Field(default_factory=list)


class GatewayHealthOut(BaseModel):
    gateway_code: str
    available: Optional[bool] = None
    response_time_ms: Optional[float] = None
    http_status: Optional[int] = None
    checked_at: str
    detail: Optional[str] = None
    status: str = "Unknown"
    history: List[Dict[str, Any]] = Field(default_factory=list)
