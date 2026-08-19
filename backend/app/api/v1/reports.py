"""Reports and exports (specification sections 27 and 28)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_session
from app.models import Transaction, User
from app.services import analytics, export

router = APIRouter(prefix="/reports", tags=["reports"])

MEDIA_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _filters(gateway_code, integration_type, currency, environment, status_filter,
             methodology, date_from, date_to) -> analytics.TransactionFilters:
    return analytics.TransactionFilters(
        gateway_codes=gateway_code, integration_type=integration_type, currency=currency,
        environment=environment, status=status_filter, methodology=methodology,
        date_from=date_from, date_to=date_to)


@router.get("/gateway-summary")
async def gateway_summary(gateway_code: Optional[List[str]] = Query(None),
                          integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
                          currency: Optional[str] = Query(None),
                          environment: Optional[str] = Query(None),
                          date_from: Optional[datetime] = Query(None),
                          date_to: Optional[datetime] = Query(None),
                          _: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    filters = _filters(gateway_code, integration_type, currency, environment, None,
                       None, date_from, date_to)
    return {"rows": await analytics.gateway_summary(session, filters)}


@router.get("/api-breakdown")
async def api_breakdown(gateway_code: Optional[List[str]] = Query(None),
                        integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
                        currency: Optional[str] = Query(None),
                        date_from: Optional[datetime] = Query(None),
                        date_to: Optional[datetime] = Query(None),
                        _: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Every gateway operation measured separately, with its normalized category."""
    filters = _filters(gateway_code, integration_type, currency, None, None, None,
                       date_from, date_to)
    return {"rows": await analytics.api_breakdown(session, filters)}


@router.get("/hpp-performance")
async def hpp_performance(gateway_code: Optional[List[str]] = Query(None),
                          currency: Optional[str] = Query(None),
                          date_from: Optional[datetime] = Query(None),
                          date_to: Optional[datetime] = Query(None),
                          _: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    filters = _filters(gateway_code, "hpp", currency, None, None, None, date_from, date_to)
    return {"rows": await analytics.hpp_comparison(session, filters)}


@router.get("/export")
async def export_results(
        format: str = Query("csv", pattern="^(csv|json|excel)$"),
        dataset: str = Query("transactions", pattern="^(transactions|summary|api_breakdown|all)$"),
        gateway_code: Optional[List[str]] = Query(None),
        integration_type: Optional[str] = Query(None, pattern="^(hpp|direct)$"),
        currency: Optional[str] = Query(None),
        environment: Optional[str] = Query(None),
        status_filter: Optional[str] = Query(None, alias="status"),
        methodology: Optional[str] = Query(None),
        date_from: Optional[datetime] = Query(None),
        date_to: Optional[datetime] = Query(None),
        limit: int = Query(10000, ge=1, le=100000),
        _: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)) -> Response:
    """Export the current view as CSV, Excel or JSON.

    The filter set travels with the file — in the JSON envelope and on the workbook's
    About sheet — because a table of latencies with no record of what produced it is
    not evidence of anything.
    """
    filters = _filters(gateway_code, integration_type, currency, environment,
                       status_filter, methodology, date_from, date_to)

    transactions = list((await session.execute(
        filters.apply(select(Transaction).order_by(Transaction.started_at.desc()))
        .limit(limit))).scalars())
    context = {
        "gateway_code": gateway_code, "integration_type": integration_type,
        "currency": currency, "environment": environment, "status": status_filter,
        "methodology": methodology,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "row_limit": limit, "rows_returned": len(transactions),
    }

    transaction_rows = export.transactions_to_rows(transactions)
    summary_rows = export.flatten_summary(await analytics.comparison_table(session, filters))
    breakdown_rows = await analytics.api_breakdown(session, filters)

    datasets = {
        "transactions": transaction_rows,
        "summary": summary_rows,
        "api_breakdown": breakdown_rows,
    }
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    if format == "excel":
        chosen = datasets if dataset == "all" else {dataset: datasets[dataset]}
        payload = export.to_excel(chosen, context=context)
        filename = f"burapay-{dataset}-{stamp}.xlsx"
    elif format == "json":
        chosen = datasets if dataset == "all" else {dataset: datasets[dataset]}
        payload = export.to_json({"generated_at": datetime.utcnow().isoformat(),
                                  "filters": context, "data": chosen})
        filename = f"burapay-{dataset}-{stamp}.json"
    else:
        if dataset == "all":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CSV holds one dataset. Choose transactions, summary or "
                       "api_breakdown, or export Excel/JSON for all three.")
        payload = export.to_csv(datasets[dataset])
        filename = f"burapay-{dataset}-{stamp}.csv"

    return Response(content=payload, media_type=MEDIA_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
