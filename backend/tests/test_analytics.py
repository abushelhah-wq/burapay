"""
Dashboard arithmetic (§5).

The dashboard's whole claim is that its numbers are real. These tests build a
known set of transactions and assert the reported figures match what you get by
counting the rows by hand -- which is exactly the check §8 asks for.
"""

from __future__ import annotations

import uuid

from app.models import Operation, Transaction, TransactionStatus
from app.services.analytics import (
    benchmark_summary, mean, median, nearest_rank_percentile,
)
from app.services.bootstrap import integration_type_by_code
from app.timeutil import utcnow


def test_nearest_rank_percentile_returns_an_observed_value():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    # p95 of ten samples is rank ceil(9.5) = 10 -> the largest observation.
    assert nearest_rank_percentile(values, 95) == 100
    assert nearest_rank_percentile(values, 50) == 50
    assert nearest_rank_percentile(values, 10) == 10
    # Every percentile is a value that actually occurred, never an interpolation.
    for percentile in range(1, 101):
        assert nearest_rank_percentile(values, percentile) in values


def test_empty_sample_is_none_not_zero():
    """'No data' and 'instantaneous' must not render identically."""
    assert nearest_rank_percentile([], 95) is None
    assert median([]) is None
    assert mean([]) is None


def test_median_of_an_even_sample_is_an_observed_value():
    assert median([10, 20, 30, 40]) == 20


async def _add(db, geidea, *, operation, status, duration, requests, amount=1000):
    integration = await integration_type_by_code(db, "DIRECT_API")
    row = Transaction(
        gateway_id=geidea.id,
        integration_type_id=integration.id,
        operation=operation.value,
        status=status.value,
        amount_minor=amount,
        currency="SAR",
        merchant_reference=f"BP-{uuid.uuid4().hex[:12]}",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        started_at=utcnow(),
        completed_at=utcnow(),
        duration_ms=duration,
        request_count=requests,
    )
    db.add(row)
    await db.flush()
    return row


async def test_summary_matches_a_hand_count(db, geidea):
    durations = [100, 200, 300, 400, 500]
    for index, duration in enumerate(durations):
        await _add(
            db, geidea,
            operation=Operation.SALE,
            status=(
                TransactionStatus.SUCCESS if index < 4 else TransactionStatus.FAILED
            ),
            duration=duration,
            requests=4,
        )
    await db.commit()

    cells = await benchmark_summary(db)
    assert len(cells) == 1
    cell = cells[0]

    assert cell["gateway_code"] == "geidea"
    assert cell["operation"] == "SALE"
    assert cell["sample_size"] == 5
    assert cell["succeeded"] == 4
    assert cell["failed"] == 1
    assert cell["success_rate"] == 0.8
    assert cell["avg_duration_ms"] == 300.0          # (100+200+300+400+500)/5
    assert cell["median_duration_ms"] == 300
    assert cell["p95_duration_ms"] == 500            # nearest-rank
    assert cell["min_duration_ms"] == 100
    assert cell["max_duration_ms"] == 500
    assert cell["avg_request_count"] == 4.0
    assert cell["latency_sample_size"] == 5


async def test_pending_rows_are_excluded_from_latency_and_success_rate(db, geidea):
    """
    A PENDING transaction has not succeeded or failed yet.

    Counting it as a failure would understate every gateway; including its
    absent duration would drag the averages toward zero.
    """
    await _add(
        db, geidea, operation=Operation.SALE, status=TransactionStatus.SUCCESS,
        duration=200, requests=4,
    )
    await _add(
        db, geidea, operation=Operation.SALE, status=TransactionStatus.PENDING,
        duration=None, requests=1,
    )
    await db.commit()

    cell = (await benchmark_summary(db))[0]
    assert cell["sample_size"] == 2
    assert cell["pending"] == 1
    assert cell["completed"] == 1
    assert cell["success_rate"] == 1.0
    assert cell["avg_duration_ms"] == 200.0
    assert cell["latency_sample_size"] == 1


async def test_operations_are_bucketed_separately(db, geidea):
    """A capture is not a sale; averaging them together compares nothing."""
    await _add(
        db, geidea, operation=Operation.SALE, status=TransactionStatus.SUCCESS,
        duration=400, requests=4,
    )
    await _add(
        db, geidea, operation=Operation.CAPTURE, status=TransactionStatus.SUCCESS,
        duration=100, requests=1,
    )
    await db.commit()

    cells = {cell["operation"]: cell for cell in await benchmark_summary(db)}
    assert set(cells) == {"SALE", "CAPTURE"}
    assert cells["SALE"]["avg_request_count"] == 4.0
    assert cells["CAPTURE"]["avg_request_count"] == 1.0


async def test_errors_and_timeouts_are_counted_apart_from_declines(db, geidea):
    """
    The reliability comparison depends on these being distinct.

    A gateway that declines is working; one that times out is not.
    """
    for status_value in (
        TransactionStatus.SUCCESS, TransactionStatus.FAILED,
        TransactionStatus.ERROR, TransactionStatus.TIMEOUT,
    ):
        await _add(
            db, geidea, operation=Operation.SALE, status=status_value,
            duration=150, requests=2,
        )
    await db.commit()

    cell = (await benchmark_summary(db))[0]
    assert cell["succeeded"] == 1
    assert cell["failed"] == 1
    assert cell["errored"] == 1
    assert cell["timed_out"] == 1
    assert cell["success_rate"] == 0.25


async def test_filtering_by_operation(db, geidea):
    await _add(
        db, geidea, operation=Operation.SALE, status=TransactionStatus.SUCCESS,
        duration=100, requests=4,
    )
    await _add(
        db, geidea, operation=Operation.REFUND, status=TransactionStatus.SUCCESS,
        duration=90, requests=1,
    )
    await db.commit()

    cells = await benchmark_summary(db, operations=["REFUND"])
    assert len(cells) == 1
    assert cells[0]["operation"] == "REFUND"


async def test_summary_is_empty_before_any_transaction(db, geidea):
    """No seeded or sample data: an empty system reports nothing."""
    assert await benchmark_summary(db) == []
