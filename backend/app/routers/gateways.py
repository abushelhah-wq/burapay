"""
Gateway discovery (§5).

Drives the gateway picker and the integration-type picker. The response tells
the frontend exactly what it may offer: which operations the adapter
implements, which credentials are missing, whether the production gate blocks
this gateway, and which endpoints rest on unverified documentation. A control
the API says is unavailable is never rendered as clickable.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.gateways.registry import adapter_class_or_none
from app.models import Gateway, IntegrationType
from app.schemas.gateways import (
    DocGap, GatewayOut, IntegrationTypeOut, TestCardsOut,
)
from app.security.credentials import CredentialStore

router = APIRouter(prefix="/api/gateways", tags=["gateways"])


async def _gateway_out(session: AsyncSession, gateway: Gateway) -> GatewayOut:
    adapter_class = adapter_class_or_none(gateway.code)
    store = CredentialStore(session)
    credentials = await store.resolve(gateway.id)
    is_production = await store.is_production(gateway.id)
    settings = get_settings()

    required = list(adapter_class.required_credentials) if adapter_class else []
    missing = [key for key in required if not credentials.get(key)]

    gaps: list[DocGap] = []
    if adapter_class is not None and hasattr(adapter_class, "doc_gaps"):
        gaps = [
            DocGap(
                step_name=gap["step_name"],
                doc_url=gap["doc_url"],
                provenance=gap["provenance"],
                missing=gap["missing"],
                blocks_operation=gap["blocks_operation"] == "True",
            )
            for gap in adapter_class.doc_gaps()
        ]

    return GatewayOut(
        code=gateway.code,
        display_name=gateway.display_name,
        enabled=gateway.enabled,
        configured=not missing and adapter_class is not None,
        missing_credentials=missing,
        is_production=is_production,
        production_blocked=is_production and not settings.ALLOW_PRODUCTION_GATEWAYS,
        capabilities=(
            sorted(capability.value for capability in adapter_class.capabilities)
            if adapter_class
            else []
        ),
        supported_integration_types=(
            [code.value for code in adapter_class.supported_integration_types]
            if adapter_class
            else []
        ),
        doc_urls=dict(adapter_class.doc_urls) if adapter_class else {},
        doc_gaps=gaps,
    )


@router.get("", response_model=list[GatewayOut])
async def list_gateways(
    session: AsyncSession = Depends(get_session),
) -> list[GatewayOut]:
    rows = (await session.execute(select(Gateway).order_by(Gateway.code))).scalars()
    return [await _gateway_out(session, gateway) for gateway in rows]


@router.get("/integration-types", response_model=list[IntegrationTypeOut])
async def list_integration_types(
    session: AsyncSession = Depends(get_session),
) -> list[IntegrationType]:
    result = await session.execute(
        select(IntegrationType).order_by(IntegrationType.id)
    )
    return list(result.scalars().all())


@router.get("/{code}", response_model=GatewayOut)
async def get_gateway(
    code: str, session: AsyncSession = Depends(get_session)
) -> GatewayOut:
    result = await session.execute(select(Gateway).where(Gateway.code == code))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown gateway {code!r}."
        )
    return await _gateway_out(session, gateway)


@router.get("/{code}/test-cards", response_model=TestCardsOut)
async def test_cards(code: str, session: AsyncSession = Depends(get_session)) -> TestCardsOut:
    """
    Sandbox test cards for the card-entry helper (§5).

    §5 requires this list to be populated from the gateway's test-card
    documentation rather than hardcoded from memory. Where that page has not
    been read, the list is empty and ``unavailable_reason`` says so -- the UI
    then explains the gap instead of offering a remembered card number that may
    not be valid for this merchant's sandbox.
    """
    result = await session.execute(select(Gateway).where(Gateway.code == code))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown gateway {code!r}."
        )

    adapter_class = adapter_class_or_none(code)
    doc_url = (adapter_class.doc_urls.get("test_cards", "") if adapter_class else "")

    cards: list[dict[str, object]] = []
    reason: Optional[str] = None
    if code == "geidea":
        from app.gateways.geidea import endpoints as geidea_endpoints

        cards = [dict(card) for card in geidea_endpoints.TEST_CARDS]
        if not cards:
            reason = (
                "Geidea's test-card list has not been read from "
                f"{doc_url}. That page was unreachable when this build was made "
                "(the deployment's egress proxy refuses docs.geidea.net), and "
                "§5 requires these to come from the documentation rather than "
                "from memory. Enter a card from your Geidea sandbox pack, or "
                "populate TEST_CARDS in app/gateways/geidea/endpoints.py."
            )
    else:
        reason = f"No test-card list is defined for {code!r}."

    return TestCardsOut(
        gateway_code=code, doc_url=doc_url, cards=cards, unavailable_reason=reason
    )
