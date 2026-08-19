"""Comparison dashboard, HPP comparison and charts (specification sections 13-15)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_session
from app.models import User
from app.services import analytics

router = APIRouter(prefix="/comparison", tags=["comparison"])


def _filters(gateway_code: Optional[List[str]], integration_type: Optional[str],
             currency: Optional[str], environment: Optional[str],
             status_filter: Optional[str], methodology: Optional[str],
             date_from: Optional[datetime], date_to: Optional[datetime],
             include_simulated: bool = True) -> analytics.TransactionFilters:
    return analytics.TransactionFilters(
        gateway_codes=gateway_code, integration_type=integration_type,
        currency=currency, environment=environment, status=status_filter,
        methodology=methodology, date_from=date_from, date_to=date_to,
        include_simulated=include_simulated)


@router.get("")
async def comparison(gateway_code: Optional[List[str]] = Query(None),
                     integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
                     currency: Optional[str] = Query(None),
                     environment: Optional[str] = Query(None),
                     status_filter: Optional[str] = Query(None, alias="status"),
                     methodology: Optional[str] = Query(None),
                     date_from: Optional[datetime] = Query(None),
                     date_to: Optional[datetime] = Query(None),
                     include_simulated: bool = Query(True),
                     _: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """The comparison table from section 13.

    Every row carries its own sample size and a ``reliable`` flag alongside the
    percentiles, because a p99 over eight transactions is the maximum in disguise.
    """
    filters = _filters(gateway_code, integration_type, currency, environment,
                       status_filter, methodology, date_from, date_to, include_simulated)
    rows = await analytics.comparison_table(session, filters)
    return {
        "rows": rows,
        "note": ("Latency figures cover completed transactions only. Gateway API time "
                 "excludes customer interaction and redirect time; total transaction "
                 "time includes them. The two are not interchangeable."),
    }


@router.get("/hpp")
async def hpp_comparison(gateway_code: Optional[List[str]] = Query(None),
                         currency: Optional[str] = Query(None),
                         environment: Optional[str] = Query(None),
                         date_from: Optional[datetime] = Query(None),
                         date_to: Optional[datetime] = Query(None),
                         _: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Hosted checkout breakdown (section 14)."""
    filters = _filters(gateway_code, "hpp", currency, environment, None, None,
                       date_from, date_to)
    rows = await analytics.hpp_comparison(session, filters)
    return {
        "rows": rows,
        "note": ("Page load and redirect timings come from the browser's Performance "
                 "API and are only available where the gateway's hosted page permits "
                 "cross-origin timing. Blank means not measurable, not zero."),
    }


@router.get("/integrations")
async def integration_comparison(gateway_code: Optional[List[str]] = Query(None),
                                 currency: Optional[str] = Query(None),
                                 date_from: Optional[datetime] = Query(None),
                                 date_to: Optional[datetime] = Query(None),
                                 _: User = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)
                                 ) -> Dict[str, Any]:
    """HPP against Direct API for each gateway."""
    filters = _filters(gateway_code, None, currency, None, None, None, date_from, date_to)
    return {"rows": await analytics.integration_comparison(session, filters)}


@router.get("/timeseries")
async def timeseries(gateway_code: Optional[List[str]] = Query(None),
                     integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
                     currency: Optional[str] = Query(None),
                     environment: Optional[str] = Query(None),
                     bucket: str = Query("hour", pattern="^(minute|hour|day)$"),
                     date_from: Optional[datetime] = Query(None),
                     date_to: Optional[datetime] = Query(None),
                     _: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Response time over time (section 15)."""
    filters = _filters(gateway_code, integration_type, currency, environment, None,
                       None, date_from, date_to)
    return {"bucket": bucket,
            "series": await analytics.response_time_series(session, filters, bucket=bucket)}


@router.get("/ranking")
async def ranking(gateway_code: Optional[List[str]] = Query(None),
                  integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
                  currency: Optional[str] = Query(None),
                  date_from: Optional[datetime] = Query(None),
                  date_to: Optional[datetime] = Query(None),
                  _: User = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """The Internal Benchmark Score (section 18).

    Gateways without enough comparable transactions are returned in ``excluded`` with
    the reason, rather than ranked on thin data.
    """
    filters = _filters(gateway_code, integration_type, currency, None, None, None,
                       date_from, date_to, include_simulated=False)
    return await analytics.ranking(session, filters)


@router.get("/dashboard")
async def dashboard(days: int = Query(30, ge=1, le=365),
                    _: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Everything the home page needs (section 47)."""
    return await analytics.dashboard(session, days=days)
