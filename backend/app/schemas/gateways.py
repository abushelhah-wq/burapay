"""Gateway, capability and documentation-gap responses."""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.schemas.common import ApiModel


class DocGap(ApiModel):
    """
    An endpoint whose shape is not verified against live documentation.

    Surfaced through the API so the UI can show, per operation, what evidence
    stands behind it -- and grey out the ones that rest on nothing (§0.2, §3).
    """

    step_name: str
    doc_url: str
    provenance: str
    missing: str
    blocks_operation: bool


class IntegrationTypeOut(ApiModel):
    code: str
    display_name: str


class GatewayOut(ApiModel):
    code: str
    display_name: str
    enabled: bool
    #: True once every required credential is present. The UI shows a gateway
    #: with missing credentials as "not configured" rather than letting a user
    #: press Pay and receive an authentication failure.
    configured: bool
    missing_credentials: list[str] = Field(default_factory=list)
    #: True when any stored credential is flagged live. Combined with
    #: ALLOW_PRODUCTION_GATEWAYS this tells the UI whether transacting is
    #: possible at all.
    is_production: bool = False
    production_blocked: bool = False
    #: Capability names the adapter implements (§3). Anything absent is hidden
    #: or disabled in the UI instead of failing on click.
    capabilities: list[str] = Field(default_factory=list)
    supported_integration_types: list[str] = Field(default_factory=list)
    doc_urls: dict[str, str] = Field(default_factory=dict)
    doc_gaps: list[DocGap] = Field(default_factory=list)


class TestCard(ApiModel):
    """
    A sandbox test card, served from the adapter rather than hardcoded in the
    frontend (§5).

    The list is empty when the gateway's test-card documentation has not been
    read; the UI then explains that instead of offering a remembered number.
    """

    brand: Optional[str] = None
    number: str
    exp_month: int
    exp_year: int
    cvv: str
    description: Optional[str] = None


class TestCardsOut(ApiModel):
    gateway_code: str
    doc_url: str
    cards: list[TestCard] = Field(default_factory=list)
    #: Set when the list is empty and explains why, so the UI shows a reason
    #: rather than an unexplained blank.
    unavailable_reason: Optional[str] = None


class TestConnectionOut(ApiModel):
    """Result of the Settings page's harmless read-only probe (§7b)."""

    ok: bool
    message: str
    checked: str
    duration_ms: Optional[int] = None
