"""
Background jobs.

RQ workers are synchronous processes, so each job opens its own event loop with
``asyncio.run``. That is safe here because a worker job owns its process for its
duration -- unlike the API, which has one long-lived loop and one engine.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from sqlalchemy import select

from app.db import dispose_engine, session_scope
from app.gateways.results import OrderContext, RawCard
from app.logging_setup import configure_logging, get_logger
from app.models import (
    BenchmarkRun, Gateway, IntegrationType, IntegrationTypeCode, Operation,
)
from app.services import execution
from app.services.benchmark import RunStatus, execute_run

logger = get_logger(__name__)


def run_benchmark(run_id: int, card: Optional[dict] = None) -> dict:
    """
    Execute a benchmark run. Entry point for the RQ worker.

    ``card`` is passed in memory from the enqueueing request and is never
    persisted by the queue beyond the job payload's lifetime. It exists only
    because a Direct API benchmark needs a card to charge; runs that do not
    need one pass ``None``.
    """
    configure_logging()
    try:
        return asyncio.run(_run_benchmark(run_id, card))
    finally:
        asyncio.run(dispose_engine())


async def _run_benchmark(run_id: int, card: Optional[dict]) -> dict:
    async with session_scope() as session:
        run = await session.get(BenchmarkRun, run_id)
        if run is None:
            logger.error("benchmark run not found", extra={"run_id": run_id})
            return {"run_id": run_id, "status": "missing"}
        if run.status == RunStatus.STOPPED:
            # Refused by a limit before it was ever enqueued. Nothing to do --
            # and specifically, do not "helpfully" run a shortened version.
            logger.info(
                "benchmark run was already stopped; not executing",
                extra={"run_id": run_id, "reason": run.stop_reason},
            )
            return {"run_id": run_id, "status": run.status, "reason": run.stop_reason}

        gateway = await session.get(Gateway, run.gateway_id)
        integration = (
            await session.get(IntegrationType, run.integration_type_id)
            if run.integration_type_id
            else None
        )
        if gateway is None or integration is None:
            run.status = RunStatus.FAILED
            run.stop_reason = "The gateway or integration type no longer exists."
            await session.flush()
            return {"run_id": run_id, "status": run.status}

        operation = Operation(run.operation)
        raw_card = (
            RawCard(
                number=card["number"], exp_month=int(card["exp_month"]),
                exp_year=int(card["exp_year"]), cvv=card["cvv"],
                holder_name=card.get("holder_name"),
            )
            if card
            else None
        )
        amount_minor = run.amount_minor or 1000
        currency = run.currency or "SAR"

        async def attempt(index: int):
            transaction, _ = await execution.get_or_create_transaction(
                session,
                gateway=gateway,
                integration_type=integration,
                operation=operation,
                # Derived from the run so a re-queued job cannot double-charge
                # for an index it already completed (§0.6).
                idempotency_key=f"bench-{run.id}-{index}-{uuid.uuid5(uuid.NAMESPACE_OID, str(run.id)).hex[:8]}",
                amount_minor=amount_minor,
                currency=currency,
            )

            async def runner(adapter):
                order = OrderContext(
                    merchant_reference=transaction.merchant_reference,
                    amount_minor=transaction.amount_minor,
                    currency=transaction.currency,
                    **dict(
                        zip(
                            ("return_url", "callback_url"),
                            execution.build_urls(gateway.code),
                        )
                    ),
                )
                if operation is Operation.SALE and integration.code == IntegrationTypeCode.HPP.value:
                    return await adapter.create_hpp_session(order)
                if raw_card is None:
                    raise ValueError(
                        "This benchmark operation needs a card and none was supplied."
                    )
                if operation is Operation.AUTH:
                    return await adapter.direct_api_auth(order, raw_card)
                return await adapter.direct_api_sale(order, raw_card)

            await execution.execute(
                session, gateway=gateway, transaction=transaction, runner=runner
            )
            return transaction

        await execute_run(session, run=run, attempt=attempt)
        logger.info(
            "benchmark run finished",
            extra={"run_id": run.id, "status": run.status,
                   "completed": run.completed_count},
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "completed": run.completed_count,
            "stop_reason": run.stop_reason,
        }


async def latest_runs(limit: int = 25) -> list[BenchmarkRun]:
    async with session_scope() as session:
        result = await session.execute(
            select(BenchmarkRun).order_by(BenchmarkRun.id.desc()).limit(limit)
        )
        return list(result.scalars().all())
