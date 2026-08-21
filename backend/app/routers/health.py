"""
Health endpoint (§6).

Checks database connectivity and **nothing else**. It deliberately makes no
gateway call: a health probe running every 30 seconds against a sandbox would
burn API quota and fill the merchant dashboard with noise that looks like real
traffic. Gateway reachability is checked on demand, by the Settings page's
"test connection" action, which a human triggers.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db import session_scope
from app.logging_setup import get_logger
from app.timeutil import iso, utcnow

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health")
async def health(response: Response) -> dict[str, object]:
    checks: dict[str, object] = {"database": "unknown"}
    healthy = True

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        healthy = False
        checks["database"] = "error"
        # The message is included because a health endpoint that says only
        # "unhealthy" makes an operator open a shell to find out why.
        checks["database_error"] = str(exc)[:500]
        logger.exception("health check: database unreachable")

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "unhealthy",
        "checks": checks,
        "time": iso(utcnow()),
        # Stated in the payload so nobody has to read this file to confirm it.
        "note": "This check never calls a payment gateway.",
    }


@router.get("/healthz", include_in_schema=False)
async def healthz(response: Response) -> dict[str, object]:
    """Alias kept for the container HEALTHCHECK used by the previous release."""
    return await health(response)
