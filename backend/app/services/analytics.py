"""
Benchmark analytics (§5).

Every number here is computed from rows in ``transactions`` at request time.
There is no sampling, no cache and no seeded data -- if the dashboard shows a
p95, some transaction actually took that long, and counting the matching rows
by hand reproduces it.

Percentiles are **nearest-rank, never interpolated**. An interpolated p95 is a
latency that never occurred, which is misleading in a tool whose entire purpose
is comparing real latencies. With a handful of runs a p95 is not yet
meaningful; the response reports ``sample_size`` next to every figure so a
reader can see that for themselves.

Aggregation happens in Python rather than in SQL. Postgres could do it with
``percentile_disc``, but the mock-gateway test suite runs on SQLite, and having
the dashboard's arithmetic differ between the tested path and the deployed path
would defeat the purpose of testing it. The row counts involved are small --
this is a benchmarking tool, not an analytics warehouse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Gateway, IntegrationType, Transaction, TransactionStatus


def nearest_rank_percentile(values: Sequence[int], percentile: float) -> Optional[int]:
    """
    Nearest-rank percentile: always a value that actually occurred.

    ``percentile`` is 0-100. Returns ``None`` for an empty sample rather than
    0, because "no data" and "instant" must not render identically.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def median(values: Sequence[int]) -> Optional[int]:
    """
    Median, taking the lower of the two middle values on an even sample.

    Averaging them would invent a latency that never happened, which is the
    same objection as interpolated percentiles.
    """
    return nearest_rank_percentile(values, 50)


def mean(values: Sequence[int]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


@dataclass(slots=True)
class Bucket:
    """One gateway x integration type x operation cell of the dashboard."""

    gateway_code: str
    gateway_name: str
    integration_type_code: Optional[str]
    operation: str

    durations: list[int] = field(default_factory=list)
    request_counts: list[int] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    errored: int = 0
    timed_out: int = 0
    pending: int = 0

    def add(self, row: Transaction) -> None:
        self.total += 1
        status = row.status
        if status == TransactionStatus.SUCCESS.value:
            self.succeeded += 1
        elif status == TransactionStatus.FAILED.value:
            self.failed += 1
        elif status == TransactionStatus.ERROR.value:
            self.errored += 1
        elif status == TransactionStatus.TIMEOUT.value:
            self.timed_out += 1
        else:
            self.pending += 1

        # Latency is only meaningful for a completed round trip. A PENDING row
        # has no duration yet, and including a half-measured one would drag
        # every average toward zero.
        if row.duration_ms is not None and status != TransactionStatus.PENDING.value:
            self.durations.append(row.duration_ms)
        if row.request_count:
            self.request_counts.append(row.request_count)

    def to_dict(self) -> dict[str, object]:
        completed = self.total - self.pending
        return {
            "gateway_code": self.gateway_code,
            "gateway_name": self.gateway_name,
            "integration_type_code": self.integration_type_code,
            "operation": self.operation,
            "sample_size": self.total,
            "completed": completed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "errored": self.errored,
            "timed_out": self.timed_out,
            "pending": self.pending,
            # Denominator is completed attempts, not all rows: a PENDING
            # transaction has not yet succeeded or failed, and counting it as a
            # failure would understate every gateway equally but wrongly.
            "success_rate": (
                round(self.succeeded / completed, 4) if completed else None
            ),
            "avg_duration_ms": mean(self.durations),
            "median_duration_ms": median(self.durations),
            "p95_duration_ms": nearest_rank_percentile(self.durations, 95),
            "min_duration_ms": min(self.durations) if self.durations else None,
            "max_duration_ms": max(self.durations) if self.durations else None,
            "avg_request_count": mean(self.request_counts),
            "median_request_count": median(self.request_counts),
            "max_request_count": max(self.request_counts) if self.request_counts else None,
            "latency_sample_size": len(self.durations),
        }


async def benchmark_summary(
    session: AsyncSession,
    *,
    gateway_codes: Optional[Iterable[str]] = None,
    operations: Optional[Iterable[str]] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[dict[str, object]]:
    """
    Per gateway x integration type x operation statistics, computed live.

    Returns one entry per non-empty cell, sorted so the display order is stable
    between refreshes.
    """
    gateways = {
        row.id: row for row in (await session.execute(select(Gateway))).scalars()
    }
    integration_types = {
        row.id: row for row in (await session.execute(select(IntegrationType))).scalars()
    }

    statement = select(Transaction)
    if gateway_codes:
        wanted = {code for code in gateway_codes}
        ids = [gid for gid, row in gateways.items() if row.code in wanted]
        if not ids:
            return []
        statement = statement.where(Transaction.gateway_id.in_(ids))
    if operations:
        statement = statement.where(Transaction.operation.in_(list(operations)))
    if since is not None:
        statement = statement.where(Transaction.created_at >= since)
    if until is not None:
        statement = statement.where(Transaction.created_at <= until)

    buckets: dict[tuple[int, Optional[int], str], Bucket] = {}
    for row in (await session.execute(statement)).scalars():
        key = (row.gateway_id, row.integration_type_id, row.operation)
        bucket = buckets.get(key)
        if bucket is None:
            gateway = gateways.get(row.gateway_id)
            integration = integration_types.get(row.integration_type_id)
            bucket = Bucket(
                gateway_code=gateway.code if gateway else str(row.gateway_id),
                gateway_name=gateway.display_name if gateway else str(row.gateway_id),
                integration_type_code=integration.code if integration else None,
                operation=row.operation,
            )
            buckets[key] = bucket
        bucket.add(row)

    return sorted(
        (bucket.to_dict() for bucket in buckets.values()),
        key=lambda item: (
            str(item["gateway_code"]),
            str(item["integration_type_code"] or ""),
            str(item["operation"]),
        ),
    )


async def gateway_comparison(
    session: AsyncSession, *, operation: Optional[str] = None
) -> list[dict[str, object]]:
    """
    Roll the per-cell summary up to one row per gateway.

    This is the headline comparison: how many round trips each gateway needs
    for the same work, and how long it takes.
    """
    cells = await benchmark_summary(
        session, operations=[operation] if operation else None
    )
    rolled: dict[str, Bucket] = {}
    for cell in cells:
        code = str(cell["gateway_code"])
        bucket = rolled.get(code)
        if bucket is None:
            bucket = Bucket(
                gateway_code=code,
                gateway_name=str(cell["gateway_name"]),
                integration_type_code=None,
                operation=operation or "ALL",
            )
            rolled[code] = bucket
        bucket.total += int(cell["sample_size"] or 0)
        bucket.succeeded += int(cell["succeeded"] or 0)
        bucket.failed += int(cell["failed"] or 0)
        bucket.errored += int(cell["errored"] or 0)
        bucket.timed_out += int(cell["timed_out"] or 0)
        bucket.pending += int(cell["pending"] or 0)

    # The rolled-up view reports counts and rates only. Re-deriving a p95 from
    # per-cell p95s would be arithmetically wrong, and quietly so.
    return [
        {
            key: value
            for key, value in bucket.to_dict().items()
            if not key.endswith("_duration_ms") and not key.endswith("request_count")
        }
        for bucket in sorted(rolled.values(), key=lambda item: item.gateway_code)
    ]
