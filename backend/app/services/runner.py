"""
Benchmark runs and comparison tests (specification sections 16 and 17).

A run executes N transactions against one gateway with a configurable pause between
them. It runs as a background task so the request that started it returns
immediately, and it writes progress to the database as it goes, so the UI can follow
along and a restart never leaves a run wedged in RUNNING with no explanation.

Rate limiting is not optional
-----------------------------
Section 16 is explicit: automated benchmarking must not fire uncontrolled volume at a
payment provider. Three limits apply, all configurable from the Settings page and all
enforced here rather than in the UI:

* a minimum interval between transactions (default one every 2 seconds),
* a maximum number of transactions per run,
* serial execution by default — concurrency is opt-in.

HPP runs
--------
An automated HPP run can only measure what a machine can do alone: session creation.
Completing a hosted payment needs a human on the gateway's page. Those transactions
are left PENDING and the run says so, rather than reporting a hosted-checkout figure
that nobody actually completed.
"""

from __future__ import annotations

import asyncio
import platform
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_sessionmaker
from app.models import (BenchmarkRun, ComparisonTest, Gateway, IntegrationType,
                        RunStatus, Transaction)
from app.services import bootstrap as settings_service
from app.services.benchmark import (BenchmarkRefused, run_direct_transaction,
                                    start_hpp_transaction)

logger = get_logger(__name__)

#: Runs currently executing in this process, so they can be cancelled.
_active_tasks: Dict[str, asyncio.Task] = {}


def environment_metadata(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Section 42 metadata: enough to tell whether two runs are comparable.

    Deliberately excludes anything sensitive about the host — no IP addresses, no
    environment variables, no paths.
    """
    metadata = {
        "app_version": settings.app_version,
        "hostname": socket.gethostname(),
        "server_region": settings.server_region,
        "app_env": settings.app_env,
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        metadata.update(extra)
    return metadata


async def apply_limits(session: AsyncSession, *, transaction_count: int,
                       interval_seconds: float) -> Dict[str, float]:
    """Clamp a requested run to the configured safety limits."""
    limits = await settings_service.get_setting(session, "benchmark_limits")
    min_interval = float(limits.get("min_interval_seconds",
                                    settings.benchmark_min_interval_seconds))
    max_count = int(limits.get("max_transactions_per_run",
                               settings.benchmark_max_transactions_per_run))
    return {
        "transaction_count": max(1, min(int(transaction_count), max_count)),
        "interval_seconds": max(float(interval_seconds or 0.0), min_interval),
        "min_interval_seconds": min_interval,
        "max_transactions_per_run": max_count,
    }


async def create_run(session: AsyncSession, *, name: str, gateway_code: str,
                     integration_type: str, currency: str, amount: float,
                     transaction_count: int, interval_seconds: Optional[float] = None,
                     environment: str = "sandbox", methodology: str = "mixed",
                     created_by: Optional[str] = None,
                     comparison_test_id: Optional[str] = None) -> BenchmarkRun:
    gateway = (await session.execute(
        select(Gateway).where(Gateway.code == gateway_code))).scalar_one_or_none()
    if gateway is None:
        raise BenchmarkRefused(f"Unknown gateway {gateway_code!r}.")

    limits = await apply_limits(
        session, transaction_count=transaction_count,
        interval_seconds=(interval_seconds if interval_seconds is not None
                          else settings.benchmark_min_interval_seconds))

    run = BenchmarkRun(
        name=name, gateway_id=gateway.id, comparison_test_id=comparison_test_id,
        integration_type=integration_type, environment=environment, currency=currency.upper(),
        amount=amount, transaction_count=int(limits["transaction_count"]),
        interval_seconds=limits["interval_seconds"], methodology=methodology,
        status=RunStatus.QUEUED.value, created_by=created_by,
        run_metadata=environment_metadata({
            "requested_transaction_count": transaction_count,
            "applied_limits": limits,
            "gateway_code": gateway_code,
        }))
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _execute_run(run_id: str) -> None:
    """Run the transactions. Owns its own session — the request that started it is gone."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await session.get(BenchmarkRun, run_id)
        if run is None:
            return
        gateway = await session.get(Gateway, run.gateway_id) if run.gateway_id else None
        if gateway is None:
            run.status = RunStatus.FAILED.value
            run.error_message = "The gateway for this run no longer exists."
            await session.commit()
            return

        run.status = RunStatus.RUNNING.value
        run.started_at = datetime.now(timezone.utc)
        await session.commit()

        integration = (IntegrationType.HPP if run.integration_type == IntegrationType.HPP.value
                       else IntegrationType.DIRECT)
        failures = 0
        try:
            for index in range(run.transaction_count):
                if index:
                    await asyncio.sleep(run.interval_seconds)
                try:
                    if integration is IntegrationType.DIRECT:
                        await run_direct_transaction(
                            session, gateway_code=gateway.code, amount=run.amount,
                            currency=run.currency, environment=run.environment,
                            benchmark_run_id=run.id, methodology=run.methodology,
                            description=f"{run.name} #{index + 1}")
                    else:
                        # Session creation only: nobody is on the hosted page.
                        await start_hpp_transaction(
                            session, gateway_code=gateway.code, amount=run.amount,
                            currency=run.currency, environment=run.environment,
                            benchmark_run_id=run.id, methodology=run.methodology,
                            description=f"{run.name} #{index + 1}")
                except BenchmarkRefused:
                    raise
                except Exception as exc:                  # noqa: BLE001
                    # A single failed transaction is data, not a reason to stop: the
                    # failure is already recorded against its own transaction row.
                    failures += 1
                    logger.warning("benchmark transaction failed",
                                   extra={"gateway": gateway.code, "run_id": run.id,
                                          "operation": "benchmark_run",
                                          "status": "error", "error": str(exc)[:300]})
            run.status = RunStatus.COMPLETED.value
            if failures:
                run.error_message = (f"{failures} of {run.transaction_count} transactions "
                                     f"failed; see the transactions for details.")
        except asyncio.CancelledError:
            run.status = RunStatus.CANCELLED.value
            run.error_message = "Cancelled before completion."
            await session.commit()
            raise
        except Exception as exc:                          # noqa: BLE001
            run.status = RunStatus.FAILED.value
            run.error_message = str(exc)[:2000]
        finally:
            run.completed_at = datetime.now(timezone.utc)
            try:
                await session.commit()
            except Exception as exc:                      # noqa: BLE001
                logger.warning("could not record run status",
                               extra={"run_id": run_id, "operation": "benchmark_run",
                                      "status": "error", "error": str(exc)[:300]})
            _active_tasks.pop(run_id, None)

        logger.info("benchmark run finished",
                    extra={"gateway": gateway.code, "run_id": run.id,
                           "operation": "benchmark_run", "status": run.status})


def start_run(run_id: str) -> None:
    """Launch a run in the background."""
    if run_id in _active_tasks:
        return
    task = asyncio.create_task(_execute_run(run_id))
    _active_tasks[run_id] = task
    task.add_done_callback(lambda _t: _active_tasks.pop(run_id, None))


def cancel_run(run_id: str) -> bool:
    task = _active_tasks.get(run_id)
    if task is None:
        return False
    task.cancel()
    return True


def is_running(run_id: str) -> bool:
    return run_id in _active_tasks


# --------------------------------------------------------------------------- #
# Comparison tests
# --------------------------------------------------------------------------- #

async def create_comparison_test(session: AsyncSession, *, name: str,
                                 gateway_codes: List[str], integration_type: str,
                                 currency: str, amount: float,
                                 transactions_per_gateway: int,
                                 interval_seconds: Optional[float] = None,
                                 environment: str = "sandbox",
                                 methodology: str = "mixed",
                                 created_by: Optional[str] = None
                                 ) -> tuple[ComparisonTest, List[BenchmarkRun]]:
    """Create one comparison test and an identical run per gateway.

    Identical is the point: same amount, same currency, same count, same interval,
    same server, executed one after another. Anything that differs between the runs
    would show up in the comparison as a gateway difference.
    """
    if len(gateway_codes) < 2:
        raise BenchmarkRefused("A comparison test needs at least two gateways.")

    test = ComparisonTest(
        name=name, integration_type=integration_type, environment=environment,
        currency=currency.upper(), amount=amount,
        transactions_per_gateway=transactions_per_gateway,
        gateway_codes=list(gateway_codes), status=RunStatus.QUEUED.value,
        created_by=created_by, run_metadata=environment_metadata())
    session.add(test)
    await session.flush()

    runs: List[BenchmarkRun] = []
    for code in gateway_codes:
        runs.append(await create_run(
            session, name=f"{name} — {code}", gateway_code=code,
            integration_type=integration_type, currency=currency, amount=amount,
            transaction_count=transactions_per_gateway, interval_seconds=interval_seconds,
            environment=environment, methodology=methodology, created_by=created_by,
            comparison_test_id=test.id))
    await session.commit()
    await session.refresh(test)
    return test, runs


async def _execute_comparison(test_id: str, run_ids: List[str]) -> None:
    """Run each gateway's leg in sequence.

    Sequential rather than parallel on purpose: running six gateways at once means
    they share this server's CPU, network interface and connection pool, and the
    slowest of them would be measured while the others compete with it. Serial costs
    wall-clock time and buys comparability, which is the whole point.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        test = await session.get(ComparisonTest, test_id)
        if test is None:
            return
        test.status = RunStatus.RUNNING.value
        test.started_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        for run_id in run_ids:
            await _execute_run(run_id)
        status = RunStatus.COMPLETED.value
    except asyncio.CancelledError:
        status = RunStatus.CANCELLED.value
        raise
    except Exception:                                     # noqa: BLE001
        status = RunStatus.FAILED.value
    finally:
        # Best effort: on cancellation the process is usually shutting down, and
        # failing to write a status must not turn a clean stop into a crash.
        try:
            async with sessionmaker() as session:
                test = await session.get(ComparisonTest, test_id)
                if test is not None:
                    test.status = status
                    test.completed_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as exc:                          # noqa: BLE001
            logger.warning("could not record comparison test status",
                           extra={"operation": "comparison_test", "status": "error",
                                  "error": str(exc)[:300]})
        _active_tasks.pop(test_id, None)


def start_comparison(test_id: str, run_ids: List[str]) -> None:
    if test_id in _active_tasks:
        return
    task = asyncio.create_task(_execute_comparison(test_id, run_ids))
    _active_tasks[test_id] = task
    task.add_done_callback(lambda _t: _active_tasks.pop(test_id, None))


async def run_progress(session: AsyncSession, run: BenchmarkRun) -> Dict[str, Any]:
    """How far a run has got, counted from the transactions it has actually written."""
    result = await session.execute(
        select(Transaction.status).where(Transaction.benchmark_run_id == run.id))
    statuses = [row[0] for row in result]
    completed = len(statuses)
    return {
        "completed": completed,
        "total": run.transaction_count,
        "percent": round(completed / run.transaction_count * 100, 1) if run.transaction_count else 0.0,
        "succeeded": sum(1 for s in statuses if s == "SUCCESS"),
        "pending": sum(1 for s in statuses if s in ("PENDING", "IN_PROGRESS")),
        "is_active": is_running(run.id),
    }
