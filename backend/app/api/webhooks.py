"""
Gateway webhooks (specification section 23).

One endpoint per gateway, mounted at ``/api/webhooks/{gateway}``. Three things happen
on every delivery, in this order:

1. ``webhook_received_at`` is stamped immediately, before any parsing or lookup. That
   timestamp is the measurement — it is how asynchronous completion latency is
   computed — so nothing the platform does may sit between arrival and the stamp.
2. The signature is verified where the gateway offers one and the secret is
   configured. The result is recorded as verified / failed / unknown; it is never
   recorded as verified because the request merely arrived.
3. The payload is sanitized and stored, matched to a transaction by merchant
   reference or gateway id where possible.

Every delivery gets a 200. A gateway that retries on non-2xx would otherwise keep
redelivering a payload the platform has already recorded, and the retries would
pollute the very latency figures this endpoint exists to produce.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.sanitizer import sanitize
from app.db.session import get_sessionmaker
from app.gateways.registry import ADAPTERS, build_adapter
from app.models import TimelineEvent, Transaction, TransactionEvent, WebhookEvent
from app.services.credentials import load_credentials

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


async def _match_transaction(session: AsyncSession, gateway_code: str,
                             reference: Optional[str],
                             gateway_reference: Optional[str]) -> Optional[Transaction]:
    if reference:
        row = (await session.execute(
            select(Transaction).where(Transaction.merchant_reference == reference)
            .order_by(Transaction.started_at.desc()).limit(1))).scalar_one_or_none()
        if row is not None:
            return row
    if gateway_reference:
        row = (await session.execute(
            select(Transaction).where(
                Transaction.gateway_code == gateway_code,
                Transaction.gateway_transaction_id == gateway_reference)
            .order_by(Transaction.started_at.desc()).limit(1))).scalar_one_or_none()
        if row is not None:
            return row
    return None


@router.post("/{gateway_code}")
async def receive_webhook(gateway_code: str, request: Request) -> JSONResponse:
    # Stamp first. Everything below is our latency, not the gateway's.
    received_at = datetime.now(timezone.utc)
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}

    if gateway_code not in ADAPTERS:
        return JSONResponse({"received": True, "matched": False,
                             "detail": "unknown gateway"}, status_code=200)

    async with get_sessionmaker()() as session:
        parsed: Dict[str, Any] = {}
        try:
            credentials = await load_credentials(session, gateway_code)
            adapter = build_adapter(gateway_code, credentials)
            parsed = adapter.parse_webhook(headers, body)
        except Exception as exc:                          # noqa: BLE001
            # An unparseable webhook is still a data point: its arrival time is the
            # thing being measured, so it is stored either way.
            logger.warning("webhook parse failed",
                           extra={"gateway": gateway_code, "operation": "webhook",
                                  "status": "error", "error": str(exc)[:300]})

        transaction = await _match_transaction(
            session, gateway_code, parsed.get("reference"), parsed.get("gateway_reference"))

        session.add(WebhookEvent(
            gateway_code=gateway_code,
            transaction_id=transaction.id if transaction else None,
            received_at=received_at,
            event_type=str(parsed.get("event_type") or "")[:120] or None,
            signature_verified=parsed.get("signature_verified"),
            merchant_reference=parsed.get("reference"),
            payload=sanitize(parsed.get("payload") or {})))

        if transaction is not None:
            if transaction.webhook_received_at is None:
                transaction.webhook_received_at = received_at
                started = transaction.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                offset_ms = round((received_at - started).total_seconds() * 1000.0, 3)
                transaction.webhook_latency_ms = offset_ms
                session.add(TransactionEvent(
                    transaction_id=transaction.id,
                    event_type=TimelineEvent.WEBHOOK_RECEIVED.value,
                    event_timestamp=received_at, offset_ms=offset_ms,
                    label="Gateway webhook received",
                    event_metadata={"event_type": parsed.get("event_type"),
                                    "signature_verified": parsed.get("signature_verified")}))
        await session.commit()

    logger.info("webhook received",
                extra={"gateway": gateway_code, "operation": "webhook",
                       "transaction_id": transaction.id if transaction else None,
                       "status": "ok", "signature_verified": parsed.get("signature_verified")})
    return JSONResponse({"received": True, "matched": transaction is not None},
                        status_code=200)
