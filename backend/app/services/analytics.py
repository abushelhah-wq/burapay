"""
Aggregation for the dashboard, the comparison views and the reports.

Every aggregate here obeys three rules that come straight from the specification:

1. **Never average alone.** Section 13: an average hides spikes. Every group carries
   count, min, max, mean, median, p50/p90/p95/p99, standard deviation and success
   rate together, and the UI is expected to show the sample size next to any figure.
2. **Never mix gateway latency with customer time.** Section 39. Two separate
   aggregates are computed for every group: ``api`` over ``gateway_api_time_ms`` and
   ``total`` over ``total_duration_ms``. They answer different questions and are
   labelled as such.
3. **Only completed transactions count toward latency.** A transaction that died on
   call 2 of 4 has a duration that is not comparable with one that finished, and
   averaging the two together quietly flatters whichever gateway fails fastest.
   Failures are counted in the success rate, which is where they belong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.stats import MIN_RANKING_SAMPLES, rates, summarize
from app.benchmarks.scoring import ScoreInput, score_all
from app.gateways.registry import SIMULATED_CODES, documented_calls
from app.models import (ApiMeasurement, BenchmarkRun, Gateway,
                        GatewayHealthCheck, Transaction, TransactionStatus)
from app.services import bootstrap as settings_service

#: Statuses that mean the flow reached its end and its duration is comparable.
COMPLETED_STATUSES = (TransactionStatus.SUCCESS.value, TransactionStatus.DECLINED.value)


class TransactionFilters:
    """The filter set shared by the transactions list, the charts and the reports."""

    def __init__(self, *, gateway_codes: Optional[Sequence[str]] = None,
                 integration_type: Optional[str] = None,
                 status: Optional[str] = None, currency: Optional[str] = None,
                 payment_mode: Optional[str] = None,
                 environment: Optional[str] = None,
                 benchmark_run_id: Optional[str] = None,
                 merchant_reference: Optional[str] = None,
                 gateway_transaction_id: Optional[str] = None,
                 date_from: Optional[datetime] = None,
                 date_to: Optional[datetime] = None,
                 methodology: Optional[str] = None,
                 include_simulated: bool = True) -> None:
        self.gateway_codes = list(gateway_codes) if gateway_codes else None
        self.integration_type = integration_type
        self.payment_mode = payment_mode
        self.status = status
        self.currency = currency.upper() if currency else None
        self.environment = environment
        self.benchmark_run_id = benchmark_run_id
        self.merchant_reference = merchant_reference
        self.gateway_transaction_id = gateway_transaction_id
        self.date_from = date_from
        self.date_to = date_to
        self.methodology = methodology
        self.include_simulated = include_simulated

    def apply(self, statement: Select) -> Select:
        if self.gateway_codes:
            statement = statement.where(Transaction.gateway_code.in_(self.gateway_codes))
        if not self.include_simulated:
            statement = statement.where(
                Transaction.gateway_code.notin_(tuple(SIMULATED_CODES)))
        if self.integration_type:
            statement = statement.where(Transaction.integration_type == self.integration_type)
        if self.payment_mode:
            statement = statement.where(Transaction.payment_mode == self.payment_mode)
        if self.status:
            statement = statement.where(Transaction.status == self.status)
        if self.currency:
            statement = statement.where(Transaction.currency == self.currency)
        if self.environment:
            statement = statement.where(Transaction.environment == self.environment)
        if self.benchmark_run_id:
            statement = statement.where(Transaction.benchmark_run_id == self.benchmark_run_id)
        if self.merchant_reference:
            statement = statement.where(
                Transaction.merchant_reference.ilike(f"%{self.merchant_reference}%"))
        if self.gateway_transaction_id:
            statement = statement.where(
                Transaction.gateway_transaction_id.ilike(f"%{self.gateway_transaction_id}%"))
        if self.methodology:
            statement = statement.where(Transaction.methodology == self.methodology)
        if self.date_from:
            statement = statement.where(Transaction.started_at >= self.date_from)
        if self.date_to:
            statement = statement.where(Transaction.started_at <= self.date_to)
        return statement


async def _rows(session: AsyncSession, filters: TransactionFilters) -> List[Transaction]:
    statement = filters.apply(select(Transaction).order_by(Transaction.started_at.desc()))
    return list((await session.execute(statement)).scalars())


def _group_stats(transactions: Sequence[Transaction]) -> Dict[str, Any]:
    """The full statistical picture for one group of transactions."""
    completed = [t for t in transactions if t.status in COMPLETED_STATUSES]
    api_values = [t.gateway_api_time_ms for t in completed if t.gateway_api_time_ms is not None]
    total_values = [t.total_duration_ms for t in completed if t.total_duration_ms is not None]
    successes = sum(1 for t in transactions if t.status == TransactionStatus.SUCCESS.value)

    call_counts = sorted({t.api_call_count for t in completed if t.api_call_count})
    return {
        "transactions": len(transactions),
        "completed": len(completed),
        # Latency over completed transactions only — see the module docstring.
        "api": summarize(api_values),
        "total": summarize(total_values),
        **rates(successes, len(transactions)),
        "api_call_count": call_counts[0] if len(call_counts) == 1 else None,
        "api_call_count_range": call_counts,
        "status_breakdown": _count_by(transactions, lambda t: t.status),
        "error_breakdown": _count_by([t for t in transactions if t.error_category],
                                     lambda t: t.error_category),
    }


def _count_by(items: Iterable[Any], key) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        value = key(item)
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Comparison dashboard
# --------------------------------------------------------------------------- #

async def comparison_table(session: AsyncSession, filters: TransactionFilters
                           ) -> List[Dict[str, Any]]:
    """One row per gateway + integration, as in section 13."""
    transactions = await _rows(session, filters)
    names = {g.code: g.name for g in (await session.execute(select(Gateway))).scalars()}

    grouped: Dict[tuple, List[Transaction]] = {}
    for transaction in transactions:
        grouped.setdefault((transaction.gateway_code, transaction.integration_type,
                            transaction.payment_mode or "standard"), []).append(transaction)

    rows: List[Dict[str, Any]] = []
    for (code, integration, mode), group in sorted(grouped.items()):
        # A gateway that documents a separate call count for the mode gets that one.
        docs = documented_calls(code) if code in names else {}
        entry = {
            "gateway_code": code,
            "gateway_name": names.get(code, code),
            "integration_type": integration,
            "payment_mode": mode,
            "is_simulated": code in SIMULATED_CODES,
            "documented_calls": docs.get(f"{integration}/{mode}") or docs.get(integration),
        }
        entry.update(_group_stats(group))
        rows.append(entry)
    rows.sort(key=lambda row: (row["api"]["mean"] is None, row["api"]["mean"] or 0))
    return rows


async def hpp_comparison(session: AsyncSession, filters: TransactionFilters
                         ) -> List[Dict[str, Any]]:
    """The HPP-specific breakdown from section 14.

    Each column is only populated where it could be measured: page load and redirect
    come from browser reports, which cross-origin restrictions can withhold entirely.
    A gateway with no browser data shows nulls there, not zeros.
    """
    filters.integration_type = "hpp"
    transactions = await _rows(session, filters)
    names = {g.code: g.name for g in (await session.execute(select(Gateway))).scalars()}

    ids = [t.id for t in transactions]
    session_ms: Dict[str, float] = {}
    if ids:
        statement = select(ApiMeasurement.transaction_id,
                           func.sum(ApiMeasurement.duration_ms)).where(
            ApiMeasurement.transaction_id.in_(ids),
            ApiMeasurement.normalized_operation == "SESSION_CREATION",
            ApiMeasurement.is_setup_call.is_(False)).group_by(ApiMeasurement.transaction_id)
        session_ms = {row[0]: float(row[1] or 0.0) for row in (await session.execute(statement))}

    grouped: Dict[str, List[Transaction]] = {}
    for transaction in transactions:
        grouped.setdefault(transaction.gateway_code, []).append(transaction)

    rows: List[Dict[str, Any]] = []
    for code, group in sorted(grouped.items()):
        completed = [t for t in group if t.status in COMPLETED_STATUSES]
        rows.append({
            "gateway_code": code,
            "gateway_name": names.get(code, code),
            "transactions": len(group),
            "completed": len(completed),
            "session_creation": summarize([session_ms[t.id] for t in group
                                           if t.id in session_ms]),
            "redirect": summarize([t.redirect_time_ms for t in group
                                   if t.redirect_time_ms is not None]),
            "page_load": summarize([t.page_load_time_ms for t in group
                                    if t.page_load_time_ms is not None]),
            "customer_interaction": summarize([t.customer_interaction_time_ms for t in group
                                               if t.customer_interaction_time_ms is not None]),
            "three_ds": summarize([t.three_ds_time_ms for t in group
                                   if t.three_ds_time_ms is not None]),
            "webhook": summarize([t.webhook_latency_ms for t in group
                                  if t.webhook_latency_ms is not None]),
            "gateway_api": summarize([t.gateway_api_time_ms for t in completed
                                      if t.gateway_api_time_ms is not None]),
            "total": summarize([t.total_duration_ms for t in completed
                                if t.total_duration_ms is not None]),
            **rates(sum(1 for t in group if t.status == TransactionStatus.SUCCESS.value),
                    len(group)),
        })
    return rows


async def integration_comparison(session: AsyncSession, filters: TransactionFilters
                                 ) -> List[Dict[str, Any]]:
    """HPP against Direct API for the same gateway — definition-of-done item 14."""
    rows = await comparison_table(session, filters)
    by_gateway: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        entry = by_gateway.setdefault(row["gateway_code"], {
            "gateway_code": row["gateway_code"], "gateway_name": row["gateway_name"],
            "hpp": None, "direct": None})
        entry[row["integration_type"]] = row
    out = []
    for entry in by_gateway.values():
        hpp, direct = entry.get("hpp"), entry.get("direct")
        entry["api_delta_ms"] = (
            round(hpp["api"]["mean"] - direct["api"]["mean"], 3)
            if hpp and direct and hpp["api"]["mean"] is not None
            and direct["api"]["mean"] is not None else None)
        out.append(entry)
    return sorted(out, key=lambda item: item["gateway_name"])


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

async def api_breakdown(session: AsyncSession, filters: TransactionFilters
                        ) -> List[Dict[str, Any]]:
    """Per-operation latency (section 27, "Gateway API Breakdown").

    Grouped by the gateway's *own* operation name with the normalized category
    alongside it, so Stripe's PaymentIntent and Adyen's /payments stay
    distinguishable while remaining comparable (section 38).
    """
    transaction_ids = [t.id for t in await _rows(session, filters)]
    if not transaction_ids:
        return []
    statement = select(
        ApiMeasurement.transaction_id, ApiMeasurement.operation_name,
        ApiMeasurement.normalized_operation, ApiMeasurement.http_method,
        ApiMeasurement.duration_ms, ApiMeasurement.success, ApiMeasurement.http_status,
        Transaction.gateway_code, Transaction.integration_type
    ).join(Transaction, Transaction.id == ApiMeasurement.transaction_id).where(
        ApiMeasurement.transaction_id.in_(transaction_ids),
        ApiMeasurement.is_setup_call.is_(False))

    grouped: Dict[tuple, Dict[str, Any]] = {}
    for row in await session.execute(statement):
        key = (row.gateway_code, row.integration_type, row.operation_name,
               row.normalized_operation)
        entry = grouped.setdefault(key, {"durations": [], "successes": 0, "total": 0,
                                         "http_method": row.http_method})
        entry["durations"].append(float(row.duration_ms or 0.0))
        entry["total"] += 1
        entry["successes"] += 1 if row.success else 0

    out = []
    for (code, integration, operation, normalized), entry in sorted(grouped.items()):
        item = {"gateway_code": code, "integration_type": integration,
                "operation_name": operation, "normalized_operation": normalized,
                "http_method": entry["http_method"]}
        item.update(summarize(entry["durations"]))
        item.update(rates(entry["successes"], entry["total"]))
        out.append(item)
    return out


async def gateway_summary(session: AsyncSession, filters: TransactionFilters
                          ) -> List[Dict[str, Any]]:
    """Section 27's Gateway Summary report."""
    return await comparison_table(session, filters)


async def response_time_series(session: AsyncSession, filters: TransactionFilters,
                               *, bucket: str = "hour") -> List[Dict[str, Any]]:
    """Latency over time (section 15), bucketed in Python.

    Bucketing here rather than in SQL keeps this working identically on PostgreSQL and
    on the SQLite the test suite uses, at the cost of pulling the rows — acceptable at
    benchmark volumes, where a busy month is thousands of rows, not millions.
    """
    transactions = [t for t in await _rows(session, filters)
                    if t.status in COMPLETED_STATUSES and t.gateway_api_time_ms is not None]
    buckets: Dict[tuple, List[Transaction]] = {}
    for transaction in transactions:
        started = transaction.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if bucket == "day":
            stamp = started.replace(hour=0, minute=0, second=0, microsecond=0)
        elif bucket == "minute":
            stamp = started.replace(second=0, microsecond=0)
        else:
            stamp = started.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault((stamp, transaction.gateway_code,
                            transaction.integration_type), []).append(transaction)

    series = []
    for (stamp, code, integration), group in sorted(buckets.items()):
        stats = summarize([t.gateway_api_time_ms for t in group])
        series.append({
            "bucket": stamp.isoformat(), "gateway_code": code,
            "integration_type": integration, "count": stats["count"],
            "mean": stats["mean"], "p95": stats["p95"], "median": stats["median"],
        })
    return series


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

async def dashboard(session: AsyncSession, *, days: int = 30) -> Dict[str, Any]:
    """Everything the home page needs, in one query pass (section 47)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    filters = TransactionFilters(date_from=since)
    transactions = await _rows(session, filters)
    completed = [t for t in transactions if t.status in COMPLETED_STATUSES]
    api_values = [t.gateway_api_time_ms for t in completed if t.gateway_api_time_ms is not None]
    successes = sum(1 for t in transactions if t.status == TransactionStatus.SUCCESS.value)

    comparison = await comparison_table(session, TransactionFilters(
        date_from=since, include_simulated=False))
    ranked = [row for row in comparison
              if row["api"]["mean"] is not None and row["completed"] >= MIN_RANKING_SAMPLES]
    fastest = ranked[0] if ranked else None

    latest_runs = list((await session.execute(
        select(BenchmarkRun).order_by(BenchmarkRun.created_at.desc()).limit(5))).scalars())
    recent = transactions[:10]

    health_rows = (await session.execute(
        select(GatewayHealthCheck).order_by(GatewayHealthCheck.checked_at.desc()).limit(50)
    )).scalars()
    latest_health: Dict[str, GatewayHealthCheck] = {}
    for row in health_rows:
        latest_health.setdefault(row.gateway_code, row)

    stats = summarize(api_values)
    return {
        "window_days": days,
        "summary": {
            "total_transactions": len(transactions),
            "completed_transactions": len(completed),
            "average_response_ms": stats["mean"],
            "p95_response_ms": stats["p95"],
            "sample_size": stats["count"],
            "statistically_reliable": stats["reliable"],
            **rates(successes, len(transactions)),
            "fastest_gateway": ({"gateway_code": fastest["gateway_code"],
                                 "gateway_name": fastest["gateway_name"],
                                 "integration_type": fastest["integration_type"],
                                 "mean_api_ms": fastest["api"]["mean"],
                                 "sample_size": fastest["completed"]}
                                if fastest else None),
            "fastest_gateway_note": (
                None if fastest else
                f"No gateway yet has the {MIN_RANKING_SAMPLES} comparable transactions "
                f"needed before one can be called fastest."),
        },
        "comparison": comparison,
        "latest_runs": [
            {"id": run.id, "name": run.name, "status": run.status,
             "integration_type": run.integration_type, "currency": run.currency,
             "amount": run.amount, "transaction_count": run.transaction_count,
             "created_at": run.created_at.isoformat(),
             "completed_at": run.completed_at.isoformat() if run.completed_at else None}
            for run in latest_runs],
        "recent_transactions": [
            {"id": t.id, "gateway_code": t.gateway_code,
             "integration_type": t.integration_type, "status": t.status,
             "amount": t.amount, "currency": t.currency,
             "gateway_api_time_ms": t.gateway_api_time_ms,
             "total_duration_ms": t.total_duration_ms,
             "started_at": t.started_at.isoformat()} for t in recent],
        "gateway_health": [
            {"gateway_code": code, "available": row.available,
             "response_time_ms": row.response_time_ms, "http_status": row.http_status,
             "checked_at": row.checked_at.isoformat(), "detail": row.detail}
            for code, row in sorted(latest_health.items())],
    }


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

async def ranking(session: AsyncSession, filters: TransactionFilters,
                  weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """The Internal Benchmark Score over a filtered set.

    Simulated gateways are excluded outright — ranking MockPay against Stripe would be
    ranking this server's sleep timer against a payment network.
    """
    filters.include_simulated = False
    rows = await comparison_table(session, filters)
    if weights is None:
        weights = await settings_service.get_setting(session, "scoring_weights")

    inputs = [
        ScoreInput(gateway_code=row["gateway_code"], gateway_name=row["gateway_name"],
                   integration_type=row["integration_type"], sample_size=row["completed"],
                   mean_api_ms=row["api"]["mean"], p95_api_ms=row["api"]["p95"],
                   mean_total_ms=row["total"]["mean"], success_rate=row["success_rate"])
        for row in rows]
    result = score_all(inputs, weights)
    result["period"] = {
        "from": filters.date_from.isoformat() if filters.date_from else None,
        "to": filters.date_to.isoformat() if filters.date_to else None,
    }
    return result


async def transaction_detail(session: AsyncSession, transaction: Transaction) -> Dict[str, Any]:
    """One transaction with its call list, timeline and browser metrics (section 45)."""
    await session.refresh(transaction, ["measurements", "events", "browser_measurements"])
    timed = [m for m in transaction.measurements if not m.is_setup_call]
    return {
        "transaction": transaction,
        "measurements": transaction.measurements,
        "events": transaction.events,
        "browser_measurements": transaction.browser_measurements,
        "gateway_api_time_ms": round(sum(m.duration_ms for m in timed), 3),
        "setup_call_time_ms": round(
            sum(m.duration_ms for m in transaction.measurements if m.is_setup_call), 3),
        "documented_calls": documented_calls(transaction.gateway_code).get(
            f"{transaction.integration_type}/{transaction.payment_mode}")
            or documented_calls(transaction.gateway_code).get(transaction.integration_type),
        # Present only on the transaction that minted it. Shown so an operator can copy
        # it into the gateway's settings; never written there automatically.
        "stored_token": (transaction.context or {}).get("stored_token"),
        "stored_token_hint": (transaction.context or {}).get("stored_token_hint"),
        # Geidea needs the agreement as well as the token to charge the card again,
        # and showing one without the other sends people looking for the missing half.
        "agreement_id": (transaction.context or {}).get("agreement_id"),
        "agreement_type": (transaction.context or {}).get("agreement_type"),
        # Set while a payment is parked on a 3DS challenge, so the transaction page can
        # offer a way back into it rather than showing a PENDING row and no explanation.
        "awaiting_customer_action": bool(
            (transaction.context or {}).get("awaiting_customer_action")),
    }
