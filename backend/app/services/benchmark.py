"""
Benchmark run executor and its limits (§7b).

``BENCHMARK_MIN_INTERVAL_SECONDS`` and ``BENCHMARK_MAX_TRANSACTIONS_PER_RUN``
are enforced here, in the executor. They are not advisory and they are not
frontend hints -- calling the API directly hits the same checks.

The important subtlety, spelled out in §7b: **a run that would exceed the
maximum must stop, not truncate silently.** Quietly running 50 of a requested
200 would produce a result set that looks complete and is not, which is exactly
the kind of thing that makes a benchmark untrustworthy. So an over-large
request is refused outright, and the reason is recorded on the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging_setup import get_logger
from app.models import BenchmarkRun, Gateway, IntegrationType, Operation, Transaction
from app.timeutil import utcnow

logger = get_logger(__name__)


class RunStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(slots=True)
class LimitDecision:
    """Whether a run may start, and why not when it may not."""

    allowed: bool
    reason: Optional[str] = None
    #: Seconds the caller must wait, when the interval limit is what blocked it.
    retry_after_seconds: Optional[int] = None


async def check_limits(
    session: AsyncSession, *, gateway: Gateway, requested_count: int
) -> LimitDecision:
    """
    Apply both benchmark limits before a run starts.

    Checked in this order so the more actionable message wins: being told "you
    asked for too many" is more useful than "wait 43 seconds, then you will be
    told you asked for too many".
    """
    settings = get_settings()

    if requested_count < 1:
        return LimitDecision(False, "A benchmark run must request at least one transaction.")

    maximum = settings.BENCHMARK_MAX_TRANSACTIONS_PER_RUN
    if requested_count > maximum:
        return LimitDecision(
            False,
            (
                f"Requested {requested_count} transactions, which exceeds "
                f"BENCHMARK_MAX_TRANSACTIONS_PER_RUN ({maximum}). The run was "
                f"not started. Lower the count rather than expecting it to be "
                f"truncated -- a partial run reported as a whole one would "
                f"misrepresent the results."
            ),
        )

    interval = settings.BENCHMARK_MIN_INTERVAL_SECONDS
    if interval > 0:
        result = await session.execute(
            select(BenchmarkRun)
            .where(BenchmarkRun.gateway_id == gateway.id)
            .order_by(BenchmarkRun.started_at.desc())
            .limit(1)
        )
        previous = result.scalar_one_or_none()
        if previous is not None:
            elapsed = (utcnow() - _aware(previous.started_at)).total_seconds()
            if elapsed < interval:
                wait = int(round(interval - elapsed))
                return LimitDecision(
                    False,
                    (
                        f"The previous benchmark run against {gateway.display_name} "
                        f"started {int(elapsed)}s ago; "
                        f"BENCHMARK_MIN_INTERVAL_SECONDS is {interval}. "
                        f"Wait {wait}s."
                    ),
                    retry_after_seconds=wait,
                )

    return LimitDecision(True)


def _aware(value):
    """Treat a naive timestamp from the driver as UTC (see timeutil.iso)."""
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def create_run(
    session: AsyncSession,
    *,
    gateway: Gateway,
    integration_type: Optional[IntegrationType],
    operation: Operation,
    requested_count: int,
    user_id: Optional[int] = None,
) -> BenchmarkRun:
    """
    Record a run, refusing it up front when a limit says so.

    A refused run is still persisted -- with ``status=STOPPED`` and a populated
    ``stop_reason`` -- so "why did nothing happen when I pressed the button" is
    answerable from the data rather than only from a transient error toast.
    """
    decision = await check_limits(
        session, gateway=gateway, requested_count=requested_count
    )
    run = BenchmarkRun(
        gateway_id=gateway.id,
        integration_type_id=integration_type.id if integration_type else None,
        operation=operation.value,
        requested_count=requested_count,
        completed_count=0,
        status=RunStatus.PENDING if decision.allowed else RunStatus.STOPPED,
        stop_reason=decision.reason,
        started_at=utcnow(),
        completed_at=None if decision.allowed else utcnow(),
        created_by_user_id=user_id,
    )
    session.add(run)
    await session.flush()
    if not decision.allowed:
        logger.info(
            "benchmark run refused by a configured limit",
            extra={"gateway": gateway.code, "requested": requested_count,
                   "reason": decision.reason},
        )
    return run


async def execute_run(
    session: AsyncSession,
    *,
    run: BenchmarkRun,
    attempt: Callable[[int], Awaitable[Optional[Transaction]]],
) -> BenchmarkRun:
    """
    Run ``attempt`` once per requested transaction, recording progress.

    ``attempt`` receives the zero-based index and returns the transaction it
    created, or ``None`` if it could not. The executor never exceeds
    ``requested_count``, which :func:`check_limits` has already bounded by
    ``BENCHMARK_MAX_TRANSACTIONS_PER_RUN``.

    A failing attempt does not abort the run -- one declined sandbox
    transaction among fifty is data, not a reason to stop. An attempt that
    raises does stop it, because a raising attempt means the harness itself is
    broken and continuing would produce fifty identical failures.
    """
    if run.status == RunStatus.STOPPED:
        return run

    run.status = RunStatus.RUNNING
    await session.flush()

    try:
        for index in range(run.requested_count):
            await attempt(index)
            run.completed_count = index + 1
            await session.flush()
    except Exception as exc:  # noqa: BLE001
        run.status = RunStatus.FAILED
        run.stop_reason = (
            f"Run stopped after {run.completed_count} of {run.requested_count} "
            f"transactions: {exc}"
        )
        run.completed_at = utcnow()
        await session.flush()
        logger.exception(
            "benchmark run failed", extra={"run_id": run.id, "gateway_id": run.gateway_id}
        )
        return run

    run.status = RunStatus.COMPLETED
    run.completed_at = utcnow()
    await session.flush()
    return run
