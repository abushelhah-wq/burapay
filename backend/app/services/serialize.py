"""
Model -> schema conversion.

Kept in one module so every endpoint returns the same shape for the same
entity, and so the "which follow-up actions are legal" logic exists exactly
once. The frontend renders its Capture/Refund/Void buttons from
:func:`transaction_actions`, which means a button can never be offered for an
operation the API would reject (§5).
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from app.gateways.base import Capability, GatewayAdapter
from app.models import (
    ApiCallLog, Gateway, IntegrationType, Operation, Token, Transaction,
    TransactionStatus, WebhookReceived,
)
from app.schemas.common import Money
from app.schemas.transactions import (
    ApiCallLogOut, TokenOut, TransactionActions, TransactionDetail, TransactionOut,
    WebhookOut,
)


def api_call_out(row: ApiCallLog) -> ApiCallLogOut:
    return ApiCallLogOut.model_validate(row)


def transaction_out(
    row: Transaction,
    *,
    gateways: Mapping[int, Gateway],
    integration_types: Mapping[int, IntegrationType],
) -> TransactionOut:
    gateway = gateways.get(row.gateway_id)
    integration = integration_types.get(row.integration_type_id)
    return TransactionOut(
        id=row.id,
        gateway_code=gateway.code if gateway else str(row.gateway_id),
        gateway_name=gateway.display_name if gateway else str(row.gateway_id),
        integration_type=integration.code if integration else None,
        operation=row.operation,
        status=row.status,
        parent_transaction_id=row.parent_transaction_id,
        amount=Money.of(row.amount_minor, row.currency),
        captured=Money.of(row.captured_amount_minor, row.currency),
        refunded=Money.of(row.refunded_amount_minor, row.currency),
        card_brand=row.card_brand,
        card_last4=row.card_last4,
        card_exp_month=row.card_exp_month,
        card_exp_year=row.card_exp_year,
        token_reference=row.token_reference,
        gateway_order_id=row.gateway_order_id,
        gateway_transaction_id=row.gateway_transaction_id,
        merchant_reference=row.merchant_reference,
        idempotency_key=row.idempotency_key,
        error_code=row.error_code,
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        request_count=row.request_count,
        created_at=row.created_at,
    )


def transaction_actions(
    row: Transaction, adapter_class: Optional[type[GatewayAdapter]]
) -> TransactionActions:
    """
    Decide which follow-up operations are legal for this transaction *now*.

    Two independent gates: what the gateway's adapter can do at all, and what
    this transaction's state permits. Both must pass. The rules encode §4d-f:

    * Capture applies to a successful authorisation with capacity left.
    * Refund applies to a sale, or to an authorisation only up to what has
      actually been captured -- never to an uncaptured auth.
    * Void applies to an authorisation that has not been captured, and stops
      being offered the moment any capture succeeds.
    * Nothing is offered on a FAILED, ERROR or PENDING transaction.
    """
    actions = TransactionActions(
        remaining_capturable=Money.of(row.remaining_capturable_minor, row.currency),
        remaining_refundable=Money.of(row.remaining_refundable_minor, row.currency),
    )
    if adapter_class is None:
        actions.blocked_reason = "No adapter is registered for this gateway."
        return actions

    capabilities = adapter_class.capabilities
    actions.can_query = Capability.QUERY_ORDER in capabilities

    if row.status != TransactionStatus.SUCCESS.value:
        actions.blocked_reason = (
            f"This transaction is {row.status}; follow-up operations apply only "
            f"to a successful payment."
        )
        return actions

    is_auth = row.operation == Operation.AUTH.value
    is_sale_like = row.operation in (
        Operation.SALE.value, Operation.CAPTURE.value, Operation.PARTIAL_CAPTURE.value,
        Operation.CIT_CHARGE.value, Operation.MIT_CHARGE.value,
    )

    if is_auth:
        has_capacity = row.remaining_capturable_minor > 0
        actions.can_capture = has_capacity and Capability.CAPTURE in capabilities
        actions.can_partial_capture = (
            has_capacity and Capability.PARTIAL_CAPTURE in capabilities
        )
        # Void is only meaningful while nothing has been captured.
        actions.can_void = (
            row.captured_amount_minor == 0 and Capability.VOID in capabilities
        )

    refundable = row.remaining_refundable_minor > 0
    if (is_sale_like or is_auth) and refundable:
        actions.can_refund = Capability.REFUND in capabilities
        actions.can_partial_refund = Capability.PARTIAL_REFUND in capabilities
    elif is_auth and not refundable and row.captured_amount_minor == 0:
        actions.blocked_reason = (
            "This authorisation has not been captured, so there is nothing to "
            "refund. Capture it first, or void it."
        )

    return actions


def transaction_detail(
    row: Transaction,
    *,
    gateways: Mapping[int, Gateway],
    integration_types: Mapping[int, IntegrationType],
    api_calls: Sequence[ApiCallLog],
    children: Sequence[Transaction],
    adapter_class: Optional[type[GatewayAdapter]],
) -> TransactionDetail:
    base = transaction_out(row, gateways=gateways, integration_types=integration_types)
    return TransactionDetail(
        **base.model_dump(),
        api_calls=[api_call_out(call) for call in api_calls],
        actions=transaction_actions(row, adapter_class),
        children=[
            transaction_out(child, gateways=gateways, integration_types=integration_types)
            for child in children
        ],
    )


def webhook_out(row: WebhookReceived, gateway: Optional[Gateway]) -> WebhookOut:
    return WebhookOut(
        id=row.id,
        gateway_code=gateway.code if gateway else str(row.gateway_id),
        signature_valid=row.signature_valid,
        received_at=row.received_at,
        matched_transaction_id=row.matched_transaction_id,
        duplicate_of_id=row.duplicate_of_id,
        event_reference=row.event_reference,
        headers_json=row.headers_json or {},
        body_json=row.body_json,
    )


def token_out(row: Token, gateway: Optional[Gateway]) -> TokenOut:
    return TokenOut(
        id=row.id,
        gateway_code=gateway.code if gateway else str(row.gateway_id),
        token_reference=row.token_reference,
        card_brand=row.card_brand,
        card_last4=row.card_last4,
        card_exp_month=row.card_exp_month,
        card_exp_year=row.card_exp_year,
        is_active=row.is_active,
        created_at=row.created_at,
    )
