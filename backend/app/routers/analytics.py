"""
Benchmark dashboard (§5).

Computed live from stored transactions on every request. There is no cached
table, no nightly rollup and no seeded sample -- refresh after a new
transaction and the numbers move, and counting the matching rows by hand
reproduces them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.schemas.analytics import BenchmarkCell, BenchmarkSummary
from app.security.auth import require_user
from app.services.analytics import benchmark_summary, gateway_comparison
from app.timeutil import iso, utcnow

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/benchmark", response_model=BenchmarkSummary)
async def benchmark(
    gateway: Optional[list[str]] = Query(default=None),
    operation: Optional[list[str]] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> BenchmarkSummary:
    cells = await benchmark_summary(
        session,
        gateway_codes=gateway,
        operations=operation,
        since=since,
        until=until,
    )
    settings = get_settings()
    return BenchmarkSummary(
        cells=[BenchmarkCell(**cell) for cell in cells],
        measurement_location=settings.MEASUREMENT_LOCATION or None,
        generated_at=iso(utcnow()) or "",
    )


@router.get("/comparison")
async def comparison(
    operation: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
) -> dict[str, object]:
    """
    One row per gateway.

    Reports counts and success rates only. Latency percentiles are deliberately
    absent here: a p95 cannot be derived from per-cell p95s, and computing one
    that way would be wrong in a way nobody would notice.
    """
    return {
        "operation": operation or "ALL",
        "gateways": await gateway_comparison(session, operation=operation),
        "generated_at": iso(utcnow()),
    }
