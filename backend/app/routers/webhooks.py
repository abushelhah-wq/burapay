"""
Callback receiver and browser return handler (§4a, §4k).

Both legs of a hosted checkout land here, and neither is trusted on its own:

* ``POST /api/webhooks/{gateway_code}`` is the server-to-server callback.
* ``GET /api/payments/return/{gateway_code}`` is where the customer's browser
  comes back to.

They can arrive in either order, or one without the other. Whichever arrives,
the transaction's status is confirmed with an authenticated order query rather
than read out of the payload -- that is the §4a requirement, and it is also the
only safe option while Geidea's callback signature scheme is undocumented.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import Gateway, Transaction
from app.services import execution
from app.services.webhooks import process_webhook

router = APIRouter(tags=["webhooks"])
logger = get_logger(__name__)


@router.post("/api/webhooks/{gateway_code}")
async def receive_webhook(
    gateway_code: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """
    Accept a gateway callback.

    Unauthenticated by design -- the gateway has no session cookie. Every
    payload is stored before it is judged, and a payload that cannot be
    verified updates nothing on its own.

    Always answers 200 for a known gateway, even when the payload matched no
    transaction. A 500 would make the gateway retry a callback we have already
    recorded, which adds noise without adding information; the unmatched
    callback is visible in the webhook log instead.
    """
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes) if body_bytes.strip() else None
    except ValueError:
        body_json = None

    result = await session.execute(select(Gateway).where(Gateway.code == gateway_code))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        # 404 rather than storing it: without a gateway row there is nothing to
        # attribute the payload to, and an open endpoint that stores anything
        # posted to it is a place to dump data.
        response.status_code = status.HTTP_404_NOT_FOUND
        logger.warning("callback for unknown gateway", extra={"gateway": gateway_code})
        return {"received": False, "reason": f"Unknown gateway {gateway_code!r}."}

    record = await process_webhook(
        session,
        gateway=gateway,
        headers=dict(request.headers),
        body_bytes=body_bytes,
        body_json=body_json,
    )
    await session.commit()

    return {
        "received": True,
        "webhook_id": record.id,
        "matched_transaction_id": record.matched_transaction_id,
        "duplicate": record.duplicate_of_id is not None,
        "signature_valid": record.signature_valid,
    }


@router.get("/api/payments/return/{gateway_code}")
async def payment_return(
    gateway_code: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """
    Where the customer's browser returns after a hosted checkout (§4a).

    The redirect return is explicitly **not** authoritative: the query string is
    attacker-controllable, and a customer can close the tab before it fires. It
    is used only to identify which transaction to reconcile; the status then
    comes from an authenticated order query.

    Redirects to the frontend's transaction view so the operator lands on the
    audit trail rather than on a bare JSON body.
    """
    params = dict(request.query_params)
    reference = (
        params.get("merchantReferenceId")
        or params.get("merchant_reference")
        or params.get("reference")
    )
    order_id = params.get("orderId") or params.get("order_id")

    transaction = await _find_transaction(
        session, gateway_code=gateway_code, reference=reference, order_id=order_id
    )

    if transaction is not None:
        result = await session.execute(
            select(Gateway).where(Gateway.id == transaction.gateway_id)
        )
        gateway = result.scalar_one_or_none()
        if gateway is not None:
            # One authenticated query settles it. Failures are logged and left
            # for the callback or a manual query -- the customer should not see
            # an error page because reconciliation was slow.
            try:
                adapter = await execution.build_adapter(
                    session, gateway, transaction_id=transaction.id
                )
                status_result = await adapter.query_order(transaction)
                transaction.status = status_result.status.value
                transaction.request_count += status_result.request_count
                if status_result.gateway_order_id:
                    transaction.gateway_order_id = status_result.gateway_order_id
                await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "order query on browser return failed",
                    extra={"transaction_id": transaction.id},
                )

    settings = get_settings()
    target = (
        f"{settings.public_base_url}/transactions/{transaction.id}"
        if transaction is not None
        else f"{settings.public_base_url}/transactions"
    )
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


async def _find_transaction(
    session: AsyncSession,
    *,
    gateway_code: str,
    reference: Optional[str],
    order_id: Optional[str],
) -> Optional[Transaction]:
    result = await session.execute(select(Gateway).where(Gateway.code == gateway_code))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        return None

    if reference:
        found = await session.execute(
            select(Transaction).where(
                Transaction.gateway_id == gateway.id,
                Transaction.merchant_reference == reference,
            )
        )
        row = found.scalar_one_or_none()
        if row is not None:
            return row

    if order_id:
        found = await session.execute(
            select(Transaction).where(
                Transaction.gateway_id == gateway.id,
                Transaction.gateway_order_id == order_id,
            )
        )
        row = found.scalar_one_or_none()
        if row is not None:
            return row

    return None
