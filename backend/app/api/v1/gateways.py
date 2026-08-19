"""Gateway catalogue, credentials and health (specification sections 4, 5, 36)."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.crypto import DecryptionError
from app.db.session import get_session
from app.gateways.registry import (SIMULATED_CODES, credential_schema, documented_calls)
from app.models import Gateway, User
from app.schemas import (CredentialUpdate, CredentialsOut, GatewayDetail,
                         GatewayHealthOut, GatewayOut, GatewayUpdate, Message)
from app.services import credentials as credential_service
from app.services import health as health_service

router = APIRouter(prefix="/gateways", tags=["gateways"])


async def _detail(session: AsyncSession, gateway: Gateway,
                  environment: str) -> GatewayDetail:
    status_info = await credential_service.configuration_status(
        session, gateway.code, environment)
    return GatewayDetail(
        **GatewayOut.model_validate(gateway).model_dump(),
        configured=status_info["configured"],
        missing_fields=status_info["missing_fields"],
        status=status_info["status"],
        credential_fields=credential_schema(gateway.code),
        documented_calls=documented_calls(gateway.code),
        is_simulated=gateway.code in SIMULATED_CODES)


@router.get("", response_model=List[GatewayDetail])
async def list_gateways(environment: str = Query("sandbox", pattern="^(sandbox|production)$"),
                        enabled_only: bool = Query(False),
                        _: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> List[GatewayDetail]:
    """Every gateway, with whether it is configured and what it supports.

    This is what drives step 1 and step 2 of the user journey: an unconfigured gateway
    is listed and labelled, not hidden, so it is obvious what needs credentials.
    """
    statement = select(Gateway).order_by(Gateway.display_order, Gateway.name)
    if enabled_only:
        statement = statement.where(Gateway.enabled.is_(True))
    rows = list((await session.execute(statement)).scalars())
    return [await _detail(session, row, environment) for row in rows]


@router.get("/{code}", response_model=GatewayDetail)
async def get_gateway(code: str,
                      environment: str = Query("sandbox", pattern="^(sandbox|production)$"),
                      _: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)) -> GatewayDetail:
    gateway = await credential_service.get_gateway(session, code)
    if gateway is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Unknown gateway {code!r}.")
    return await _detail(session, gateway, environment)


@router.patch("/{code}", response_model=GatewayDetail)
async def update_gateway(code: str, payload: GatewayUpdate,
                         _: User = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)) -> GatewayDetail:
    gateway = await credential_service.get_gateway(session, code)
    if gateway is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Unknown gateway {code!r}.")
    if payload.enabled is not None:
        gateway.enabled = payload.enabled
    if payload.supported_currencies is not None:
        gateway.supported_currencies = [c.upper() for c in payload.supported_currencies]
    if payload.display_order is not None:
        gateway.display_order = payload.display_order
    await session.commit()
    await session.refresh(gateway)
    return await _detail(session, gateway, "sandbox")


@router.get("/{code}/credentials", response_model=CredentialsOut)
async def get_credentials(code: str,
                          environment: str = Query("sandbox", pattern="^(sandbox|production)$"),
                          _: User = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)) -> CredentialsOut:
    """Masked credentials only.

    There is no endpoint anywhere in this API that returns a gateway secret in full —
    not for admins, not for debugging. The plaintext leaves the database only inside
    the benchmark engine (specification section 5).
    """
    try:
        values = await credential_service.masked_credentials(session, code, environment)
    except DecryptionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    status_info = await credential_service.configuration_status(session, code, environment)
    return CredentialsOut(gateway_code=code, environment=environment, values=values,
                          configured=status_info["configured"],
                          missing_fields=status_info["missing_fields"])


@router.put("/{code}/credentials", response_model=CredentialsOut)
async def put_credentials(code: str, payload: CredentialUpdate,
                          admin: User = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)) -> CredentialsOut:
    try:
        values = await credential_service.save_credentials(
            session, code, payload.environment, payload.values, updated_by=admin.email)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    status_info = await credential_service.configuration_status(
        session, code, payload.environment)
    return CredentialsOut(gateway_code=code, environment=payload.environment,
                          values=values, configured=status_info["configured"],
                          missing_fields=status_info["missing_fields"])


@router.delete("/{code}/credentials", response_model=Message)
async def delete_credentials(code: str,
                             environment: str = Query("sandbox", pattern="^(sandbox|production)$"),
                             _: User = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)) -> Message:
    removed = await credential_service.delete_credentials(session, code, environment)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No stored credentials for that gateway and environment.")
    return Message(message=f"Credentials for {code} ({environment}) deleted.")


@router.get("/health/status", response_model=List[GatewayHealthOut])
async def health_status(hours: int = Query(24, ge=1, le=168),
                        _: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    """The most recent probe result per gateway, with a short history."""
    return await health_service.recent_health(session, hours=hours)


@router.post("/health/check", response_model=List[GatewayHealthOut])
async def run_health_check(environment: str = Query("sandbox", pattern="^(sandbox|production)$"),
                           _: User = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    """Probe every enabled gateway now, using read-only endpoints only."""
    return await health_service.probe_all(session, environment)


@router.post("/{code}/health/check", response_model=GatewayHealthOut)
async def run_single_health_check(
        code: str, environment: str = Query("sandbox", pattern="^(sandbox|production)$"),
        _: User = Depends(require_admin),
        session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    return await health_service.probe_gateway(session, code, environment)
