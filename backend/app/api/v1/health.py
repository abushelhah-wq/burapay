"""Health check (specification section 35)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Liveness plus a real database round trip.

    Returns 503 when the database is unreachable, so a container orchestrator and an
    uptime check both see the failure rather than a cheerful 200 from a process that
    cannot serve anything.
    """
    database = "connected"
    healthy = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:                              # noqa: BLE001
        database = f"unavailable: {type(exc).__name__}"
        healthy = False

    payload = HealthResponse(
        status="healthy" if healthy else "degraded",
        database=database,
        version=settings.app_version,
        environment=settings.app_env,
        test_mode=not settings.allow_production_gateways,
        checked_at=datetime.now(timezone.utc))
    return JSONResponse(payload.model_dump(mode="json"), status_code=200 if healthy else 503)
