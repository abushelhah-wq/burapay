"""
Benchmark runs (§5, §7b).

A run of N transactions takes minutes, so it is queued rather than executed
inside the request. What the caller needs immediately -- whether the run was
accepted, and why not if it was refused -- is decided synchronously before
anything is enqueued, so the response is complete on its own.

Both limits are applied here, by :mod:`app.services.benchmark`, before the job
exists. A run that would exceed BENCHMARK_MAX_TRANSACTIONS_PER_RUN is refused
and recorded as STOPPED with its reason; it is never enqueued and never
silently shortened.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.logging_setup import get_logger
from app.models import BenchmarkRun, Gateway, IntegrationType, Operation, User
from app.schemas.payments import BenchmarkRunRequest
from app.schemas.settings import BenchmarkRunOut
from app.security.auth import require_user
from app.security.gates import assert_direct_card_entry_allowed
from app.services.benchmark import RunStatus, create_run
from app.workers.queue import enqueue, queue_available, seal
from app.workers.tasks import run_benchmark

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])
logger = get_logger(__name__)


def _out(run: BenchmarkRun, gateway_code: str, integration_code: str | None) -> BenchmarkRunOut:
    return BenchmarkRunOut(
        id=run.id,
        gateway_code=gateway_code,
        operation=run.operation,
        integration_type=integration_code,
        requested_count=run.requested_count,
        completed_count=run.completed_count,
        status=run.status,
        stop_reason=run.stop_reason,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/{code}/runs", response_model=BenchmarkRunOut)
async def start_run(
    code: str,
    payload: BenchmarkRunRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> BenchmarkRunOut:
    gateway = (
        await session.execute(select(Gateway).where(Gateway.code == code))
    ).scalar_one_or_none()
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown gateway {code!r}.")

    integration = (
        await session.execute(
            select(IntegrationType).where(IntegrationType.code == payload.integration_type)
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown integration type {payload.integration_type!r}.",
        )

    try:
        operation = Operation(payload.operation)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown operation {payload.operation!r}."
        ) from exc

    if payload.card is not None:
        # The same server-side gate as the single-transaction endpoints. A
        # benchmark is not a way around it.
        assert_direct_card_entry_allowed()

    run = await create_run(
        session,
        gateway=gateway,
        integration_type=integration,
        operation=operation,
        requested_count=payload.count,
        user_id=user.id,
    )
    run.amount_minor = payload.amount_minor
    run.currency = payload.currency
    await session.commit()

    if run.status == RunStatus.STOPPED:
        # Refused by a limit. Recorded, reported, not queued.
        return _out(run, gateway.code, integration.code)

    if not queue_available():
        run.status = RunStatus.STOPPED
        run.stop_reason = (
            "The job queue (Redis) is unreachable, so this run was not started. "
            "Nothing was charged."
        )
        await session.commit()
        logger.error("benchmark run not queued: redis unreachable", extra={"run_id": run.id})
        return _out(run, gateway.code, integration.code)

    # Sealed before it reaches Redis: a queued job is persisted, so a plaintext
    # card here would be card data on disk (§0.8). The description keeps RQ from
    # writing a repr of the arguments into the worker log.
    sealed_card = seal(payload.card.model_dump()) if payload.card else None
    enqueue(
        run_benchmark, run.id, sealed_card,
        description=f"benchmark run {run.id} ({gateway.code} {operation.value} x{payload.count})",
    )
    logger.info(
        "benchmark run queued",
        extra={"run_id": run.id, "gateway": gateway.code, "count": payload.count},
    )
    return _out(run, gateway.code, integration.code)


@router.get("/runs", response_model=list[BenchmarkRunOut])
async def list_runs(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
) -> list[BenchmarkRunOut]:
    gateways = {
        row.id: row for row in (await session.execute(select(Gateway))).scalars()
    }
    integrations = {
        row.id: row for row in (await session.execute(select(IntegrationType))).scalars()
    }
    runs = (
        await session.execute(
            select(BenchmarkRun).order_by(BenchmarkRun.id.desc()).limit(50)
        )
    ).scalars()
    return [
        _out(
            run,
            gateways[run.gateway_id].code if run.gateway_id in gateways else "?",
            integrations[run.integration_type_id].code
            if run.integration_type_id in integrations
            else None,
        )
        for run in runs
    ]
