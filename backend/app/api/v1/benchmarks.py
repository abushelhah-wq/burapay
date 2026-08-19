"""Benchmark runs and gateway comparison tests (specification sections 16 and 17)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.db.session import get_session
from app.models import BenchmarkRun, ComparisonTest, Gateway, User
from app.schemas import (BenchmarkRunCreate, BenchmarkRunDetail, BenchmarkRunOut,
                         ComparisonTestCreate, ComparisonTestOut, Message, Page)
from app.services import analytics, runner
from app.services.benchmark import BenchmarkRefused

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get("/limits")
async def get_limits(_: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """The rate limits a run will be clamped to, so the UI can show them up front."""
    limits = await runner.apply_limits(session, transaction_count=1, interval_seconds=0)
    return {
        "min_interval_seconds": limits["min_interval_seconds"],
        "max_transactions_per_run": limits["max_transactions_per_run"],
        "note": ("Automated benchmarking is rate limited so it never fires uncontrolled "
                 "volume at a payment provider. These limits are configurable by an "
                 "administrator on the Settings page."),
    }


@router.post("/runs", response_model=BenchmarkRunOut, status_code=status.HTTP_201_CREATED)
async def create_run(payload: BenchmarkRunCreate, admin: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)) -> BenchmarkRunOut:
    """Queue a run and start it in the background.

    The requested transaction count and interval are clamped to the configured limits;
    what was requested and what was applied both land in the run's metadata, so a run
    is always reproducible from its own record.
    """
    try:
        run = await runner.create_run(
            session, name=payload.name, gateway_code=payload.gateway_code,
            integration_type=payload.integration_type, currency=payload.currency,
            amount=payload.amount, transaction_count=payload.transaction_count,
            interval_seconds=payload.interval_seconds, environment=payload.environment,
            methodology=payload.methodology, created_by=admin.email)
    except BenchmarkRefused as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    runner.start_run(run.id)
    return BenchmarkRunOut.model_validate(run)


@router.get("/runs", response_model=Page[BenchmarkRunOut])
async def list_runs(gateway_code: Optional[str] = Query(None),
                    status_filter: Optional[str] = Query(None, alias="status"),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    _: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)) -> Page[BenchmarkRunOut]:
    statement = select(BenchmarkRun).order_by(BenchmarkRun.created_at.desc())
    count_statement = select(func.count()).select_from(BenchmarkRun)
    if gateway_code:
        gateway = (await session.execute(
            select(Gateway).where(Gateway.code == gateway_code))).scalar_one_or_none()
        gateway_id = gateway.id if gateway else "unknown"
        statement = statement.where(BenchmarkRun.gateway_id == gateway_id)
        count_statement = count_statement.where(BenchmarkRun.gateway_id == gateway_id)
    if status_filter:
        statement = statement.where(BenchmarkRun.status == status_filter)
        count_statement = count_statement.where(BenchmarkRun.status == status_filter)

    total = (await session.execute(count_statement)).scalar_one()
    rows = (await session.execute(statement.limit(limit).offset(offset))).scalars()
    return Page[BenchmarkRunOut](items=[BenchmarkRunOut.model_validate(r) for r in rows],
                                 total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=BenchmarkRunDetail)
async def get_run(run_id: str, _: User = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)) -> BenchmarkRunDetail:
    run = await session.get(BenchmarkRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    gateway = await session.get(Gateway, run.gateway_id) if run.gateway_id else None
    statistics = await analytics.comparison_table(
        session, analytics.TransactionFilters(benchmark_run_id=run.id))
    return BenchmarkRunDetail(**BenchmarkRunOut.model_validate(run).model_dump(),
                              gateway_code=gateway.code if gateway else None,
                              progress=await runner.run_progress(session, run),
                              statistics=statistics[0] if statistics else {})


@router.post("/runs/{run_id}/cancel", response_model=Message)
async def cancel_run(run_id: str, _: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)) -> Message:
    run = await session.get(BenchmarkRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    if not runner.cancel_run(run_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="That run is not currently executing in this process.")
    return Message(message="Cancellation requested.")


@router.delete("/runs/{run_id}", response_model=Message)
async def delete_run(run_id: str, _: User = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)) -> Message:
    run = await session.get(BenchmarkRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    await session.delete(run)          # cascades to its transactions
    await session.commit()
    return Message(message="Benchmark run and its transactions deleted.")


# --------------------------------------------------------------------------- #
# Comparison tests
# --------------------------------------------------------------------------- #

@router.post("/comparison-tests", response_model=ComparisonTestOut,
             status_code=status.HTTP_201_CREATED)
async def create_comparison_test(payload: ComparisonTestCreate,
                                 admin: User = Depends(require_admin),
                                 session: AsyncSession = Depends(get_session)
                                 ) -> ComparisonTestOut:
    """Run the same test against several gateways and produce one comparable set.

    The legs run in sequence, not in parallel: six gateways measured simultaneously
    from one host would be competing for the same CPU and network interface, and that
    contention would show up as gateway latency.
    """
    try:
        test, runs = await runner.create_comparison_test(
            session, name=payload.name, gateway_codes=payload.gateway_codes,
            integration_type=payload.integration_type, currency=payload.currency,
            amount=payload.amount,
            transactions_per_gateway=payload.transactions_per_gateway,
            interval_seconds=payload.interval_seconds, environment=payload.environment,
            methodology=payload.methodology, created_by=admin.email)
    except BenchmarkRefused as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    runner.start_comparison(test.id, [run.id for run in runs])
    return ComparisonTestOut.model_validate(test)


@router.get("/comparison-tests", response_model=Page[ComparisonTestOut])
async def list_comparison_tests(limit: int = Query(50, ge=1, le=200),
                                offset: int = Query(0, ge=0),
                                _: User = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)
                                ) -> Page[ComparisonTestOut]:
    total = (await session.execute(
        select(func.count()).select_from(ComparisonTest))).scalar_one()
    rows = (await session.execute(
        select(ComparisonTest).order_by(ComparisonTest.created_at.desc())
        .limit(limit).offset(offset))).scalars()
    return Page[ComparisonTestOut](items=[ComparisonTestOut.model_validate(r) for r in rows],
                                   total=total, limit=limit, offset=offset)


@router.get("/comparison-tests/{test_id}")
async def get_comparison_test(test_id: str, _: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """The test, its runs, the per-gateway statistics and the resulting ranking."""
    test = await session.get(ComparisonTest, test_id)
    if test is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such test.")
    runs = list((await session.execute(
        select(BenchmarkRun).where(BenchmarkRun.comparison_test_id == test_id)
        .order_by(BenchmarkRun.created_at))).scalars())

    rows: List[Dict[str, Any]] = []
    for run in runs:
        stats = await analytics.comparison_table(
            session, analytics.TransactionFilters(benchmark_run_id=run.id))
        rows.extend(stats)

    from app.benchmarks.scoring import ScoreInput, score_all
    from app.services import bootstrap as settings_service
    weights = await settings_service.get_setting(session, "scoring_weights")
    ranking = score_all([
        ScoreInput(gateway_code=row["gateway_code"], gateway_name=row["gateway_name"],
                   integration_type=row["integration_type"], sample_size=row["completed"],
                   mean_api_ms=row["api"]["mean"], p95_api_ms=row["api"]["p95"],
                   mean_total_ms=row["total"]["mean"], success_rate=row["success_rate"])
        for row in rows if not row["is_simulated"]], weights)

    return {
        "test": ComparisonTestOut.model_validate(test).model_dump(),
        "runs": [BenchmarkRunOut.model_validate(run).model_dump() for run in runs],
        "results": rows,
        "ranking": ranking,
        "progress": {run.id: await runner.run_progress(session, run) for run in runs},
    }


@router.delete("/comparison-tests/{test_id}", response_model=Message)
async def delete_comparison_test(test_id: str, _: User = Depends(require_admin),
                                 session: AsyncSession = Depends(get_session)) -> Message:
    test = await session.get(ComparisonTest, test_id)
    if test is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such test.")
    await session.delete(test)
    await session.commit()
    return Message(message="Comparison test deleted.")
