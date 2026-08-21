"""
Geidea adapter -- the reference implementation (§4).

Each method links the documentation page it was built from, as §8 requires.
Every one of those links is currently annotated with the provenance of what
stands behind it, because ``docs.geidea.net`` was unreachable during this build
(the egress proxy refuses the domain). See
:mod:`app.gateways.geidea.endpoints` for the full evidence table and for how to
clear each gap.

Operations backed only by inference or by nothing at all raise
:class:`~app.gateways.errors.DocumentationRequiredError` rather than sending a
guessed request to a real payment gateway (§0.2). The API publishes which
operations those are, so the frontend greys the control out and explains why
instead of offering a button that cannot work (§3).

CALL COUNTING
-------------
``request_count`` is derived from ``len(result.calls)`` -- every HTTP call the
operation made, counted by the shared client rather than asserted by hand.

One discrepancy is worth stating plainly: §3 describes a Geidea Direct API sale
as three calls (Initiate Authentication -> Authenticate Payer -> Pay). This
repository's own documentation study records a mandatory Create Session call
before those three, making the documented sequence four. This adapter does not
hardcode either number -- it makes the calls the flow actually requires and
reports what it did, which is the entire point of the benchmark.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from app.gateways.base import Capability, GatewayAdapter, GatewayContext, WebhookEvent
from app.gateways.errors import (
    DocumentationRequiredError, GatewayDeclined, GatewayHTTPStatusError,
    InvalidTransactionState, MerchantCapabilityError,
)
from app.gateways.geidea import endpoints as ep
from app.gateways.geidea.signing import (
    DEFAULT_SIGNATURE_FIELDS, BasicAuth, build_signature, callback_signature,
    format_amount, mit_signature, save_card_signature, timestamp,
    verify_callback_signature,
)
from app.gateways.http_client import GatewayHTTPClient
from app.logging_setup import get_logger
from app.gateways.results import (
    CardDetails, HPPSessionResult, OrderContext, OrderStatusResult, PaymentResult,
    RawCard, TokenResult, detect_brand,
)
from app.money import exponent
from app.models import IntegrationTypeCode, TransactionStatus

logger = get_logger(__name__)

#: Credential key names. These are the names shown on the Settings page and
#: stored in ``gateway_credentials.key_name``.
CRED_PUBLIC_KEY = "public_key"
CRED_API_PASSWORD = "api_password"

#: Order statuses mapped to platform statuses. Anything unrecognised maps to
#: PENDING and is reconciled by an order query rather than being guessed into a
#: terminal state -- calling an unknown status SUCCESS is how a benchmark ends
#: up reporting money that never moved.
#:
#: ``InProgress`` is explicitly NON-terminal (docs/geidea/04): an order stays
#: InProgress while the customer is on the 3DS page or is retrying, and Geidea
#: sends no callback during that time. Treating it as failure would mark live
#: payments dead.
#:
#: TODO(docs): the full vocabulary still needs
#: https://docs.geidea.net/docs/api-response-codes-and-messages.md
_STATUS_MAP: dict[str, TransactionStatus] = {
    "success": TransactionStatus.SUCCESS,
    "paid": TransactionStatus.SUCCESS,
    "captured": TransactionStatus.SUCCESS,
    "authorized": TransactionStatus.SUCCESS,
    "authorised": TransactionStatus.SUCCESS,
    "refunded": TransactionStatus.SUCCESS,
    "voided": TransactionStatus.SUCCESS,
    "cancelled": TransactionStatus.SUCCESS,
    "failed": TransactionStatus.FAILED,
    "declined": TransactionStatus.FAILED,
    "rejected": TransactionStatus.FAILED,
    "expired": TransactionStatus.FAILED,
    "inprogress": TransactionStatus.PENDING,
    "in progress": TransactionStatus.PENDING,
    "pending": TransactionStatus.PENDING,
    "initiated": TransactionStatus.PENDING,
}

#: Signals in an Initiate Authentication response meaning no challenge follows.
#: Whether a frictionless run may skip Authenticate Payer is undocumented, so
#: the default is conservative: anything unrecognised is treated as
#: "challenge required" and the full sequence runs. The measured call count is
#: what answers the question, not this list.
_FRICTIONLESS_MARKERS = (
    "not enrolled", "notenrolled", "frictionless",
    "authentication_not_required", "no authentication",
)


class GeideaAdapter(GatewayAdapter):
    code = "geidea"
    display_name = "Geidea"

    required_credentials = (CRED_PUBLIC_KEY, CRED_API_PASSWORD)
    optional_credentials = ()

    capabilities = frozenset({
        Capability.CREATE_HPP_SESSION,
        Capability.DIRECT_API_SALE,
        Capability.DIRECT_API_AUTH,
        Capability.CAPTURE,
        Capability.PARTIAL_CAPTURE,
        Capability.MULTIPLE_PARTIAL_CAPTURES,
        Capability.REFUND,
        Capability.PARTIAL_REFUND,
        Capability.VOID,
        Capability.QUERY_ORDER,
        Capability.TOKENIZE,
        Capability.CHARGE_WITH_TOKEN_CIT,
        Capability.CHARGE_WITH_TOKEN_MIT,
        Capability.VERIFY_WEBHOOK,
        # Cancel Order remains unimplemented: cancel-order-1.md has not been
        # retrieved. See endpoints.CANCEL_ORDER.
    })

    supported_integration_types = (
        IntegrationTypeCode.HPP,
        IntegrationTypeCode.DIRECT_API,
        IntegrationTypeCode.TOKENIZE,
        IntegrationTypeCode.TOKEN_CIT,
        IntegrationTypeCode.TOKEN_MIT,
    )

    doc_urls = {
        "pre_requisites": f"{ep.DOCS_ROOT}/pre-requisites.md",
        "create_hpp_session": f"{ep.DOCS_ROOT}/geidea-checkout-v2.md",
        "direct_api_sale": f"{ep.DOCS_ROOT}/pay-v2.md",
        "direct_api_auth": f"{ep.DOCS_ROOT}/pay-v2.md",
        "capture": f"{ep.REFERENCE_ROOT}/capture-transaction-1",
        "refund": f"{ep.DOCS_ROOT}/refund-2.md",
        "void": f"{ep.DOCS_ROOT}/void-1.md",
        "tokenize": f"{ep.DOCS_ROOT}/save-card.md",
        "charge_with_token_cit": f"{ep.DOCS_ROOT}/tokenization.md",
        "charge_with_token_mit": f"{ep.DOCS_ROOT}/merchant-initiated-mit.md",
        "query_order": f"{ep.DOCS_ROOT}/fetch-1.md",
        "verify_webhook": f"{ep.DOCS_ROOT}/sample-callback-responses.md",
        "test_cards": f"{ep.DOCS_ROOT}/test-cards.md",
        "response_codes": f"{ep.DOCS_ROOT}/api-response-codes-and-messages.md",
    }

    # -- configuration -----------------------------------------------------

    def __init__(self, context: GatewayContext) -> None:
        super().__init__(context)
        config = dict(context.config or {})
        region = str(config.get("region") or ep.DEFAULT_REGION).lower()
        self.region = region
        self.base_url = str(
            config.get("api_base")
            or ep.REGION_HOSTS.get(region, ep.REGION_HOSTS[ep.DEFAULT_REGION])
        ).rstrip("/")
        #: Both overridable because both are inferred (see signing.py).
        self.timestamp_format = config.get("timestamp_format") or None
        self.signature_fields = tuple(
            config.get("signature_fields") or ()
        ) or None
        #: Forces the full documented sequence even when the initiate response
        #: looks frictionless. Useful for confirming the documented call count.
        self.force_full_3ds = bool(config.get("force_full_3ds", False))

    def auth(self) -> BasicAuth:
        creds = self.require_credentials(CRED_PUBLIC_KEY, CRED_API_PASSWORD)
        return BasicAuth(creds[CRED_PUBLIC_KEY], creds[CRED_API_PASSWORD])

    @classmethod
    def doc_gaps(cls) -> list[dict[str, str]]:
        """
        Endpoints that are not doc-verified, for the API and the UI.

        §8 requires each adapter method to link the doc page it was built from.
        This exposes the inverse: which pages are still needed, and for what.
        """
        return [
            {
                "step_name": endpoint.step_name,
                "doc_url": endpoint.doc_url,
                "provenance": endpoint.provenance.value,
                "missing": endpoint.missing,
                "blocks_operation": str(endpoint.blocks_operation),
            }
            for endpoint in ep.ALL_ENDPOINTS
            if endpoint.provenance is not ep.Provenance.DOC_VERIFIED
        ]

    def _require_documented(self, endpoint: ep.Endpoint, operation: str) -> None:
        if not endpoint.usable:
            raise DocumentationRequiredError(
                operation=operation, doc_url=endpoint.doc_url, missing=endpoint.missing
            )

    def _url(self, endpoint: ep.Endpoint, **params: str) -> str:
        return endpoint.format(self.base_url, **params)

    # -- response handling -------------------------------------------------

    def _parse(self, call: Any, step: str) -> dict[str, Any]:
        """
        Turn a completed call into a validated body, or raise.

        §6 requires non-2xx responses to be a distinct failure class from a
        business decline, so this checks the HTTP status first. Calls are made
        with ``raise_on_status=False`` so the failing body is available here and
        gets attached to the error rather than discarded -- the body is where a
        gateway explains itself, and a bare "HTTP 502" is not actionable.
        """
        if not call.ok:
            raise GatewayHTTPStatusError(
                f"Geidea {step} returned HTTP {call.status_code}",
                status_code=int(call.status_code or 0),
                body=call.response_json,
            )
        return self._check_response_code(call.response_json, step)

    def _check_response_code(self, body: Any, step: str) -> dict[str, Any]:
        """
        Geidea reports the business outcome in ``responseCode``, not only in
        the HTTP status: ``'000'`` is success.

        A non-success code raises :class:`GatewayDeclined`, which is a FAILED
        transaction rather than an ERROR one -- the round trip worked, which is
        what the latency benchmark is measuring.
        """
        if not isinstance(body, dict):
            raise GatewayDeclined(
                f"Geidea {step} returned a non-object body.", detail=body
            )
        code = body.get("responseCode")
        if code in (None, ep.SUCCESS_RESPONSE_CODE):
            return body

        message = (
            body.get("detailedResponseMessage")
            or body.get("responseMessage")
            or f"responseCode={code}"
        )
        if self._is_merchant_capability_error(body):
            raise MerchantCapabilityError(
                f"Geidea {step}: {message} -- this merchant account does not "
                f"appear to be enabled for this operation.",
                gateway_code=str(code),
            )
        raise GatewayDeclined(
            f"Geidea {step}: {message}", gateway_code=str(code), detail=body
        )

    @staticmethod
    def _is_merchant_capability_error(body: Mapping[str, Any]) -> bool:
        """
        Detect "your merchant account cannot do this" (§4c).

        Pre-authorisation is Saudi-platform-only per Geidea's documentation, and
        §4c requires a merchant whose account lacks it to see a clear message
        rather than a generic failure. The detection is textual because the
        numeric code that means this is not known:

        TODO(docs): replace with an exact responseCode match once
        https://docs.geidea.net/docs/api-response-codes-and-messages.md can be
        fetched. Matching on message text is a stopgap -- it is
        deliberately conservative, so an unmatched capability error degrades to
        an ordinary decline carrying Geidea's own code and message verbatim,
        which is still actionable.
        """
        haystack = " ".join(
            str(body.get(key, ""))
            for key in ("responseMessage", "detailedResponseMessage")
        ).lower()
        needles = (
            "not enabled", "not supported", "not allowed for this merchant",
            "unsupported operation", "not configured", "not activated",
        )
        return any(needle in haystack for needle in needles)

    @staticmethod
    def _order(body: Mapping[str, Any]) -> dict[str, Any]:
        order = body.get("order")
        return order if isinstance(order, dict) else {}

    @classmethod
    def _order_id(cls, body: Mapping[str, Any]) -> Optional[str]:
        order = cls._order(body)
        value = order.get("orderId") or order.get("id") or body.get("orderId")
        return str(value) if value else None

    @classmethod
    def _transaction_id(cls, body: Mapping[str, Any]) -> Optional[str]:
        order = cls._order(body)
        transactions = order.get("transactions")
        if isinstance(transactions, list) and transactions:
            last = transactions[-1]
            if isinstance(last, dict):
                value = last.get("transactionId") or last.get("id")
                if value:
                    return str(value)
        value = body.get("transactionId")
        return str(value) if value else None

    @classmethod
    def _status(cls, body: Mapping[str, Any]) -> tuple[TransactionStatus, Optional[str]]:
        """
        Normalise an order's outcome.

        ``detailedStatus`` is preferred over ``status``: docs/geidea/04 names
        the outer order status as authoritative and the tokenization sample
        shows the pair ``status: "Success"`` / ``detailedStatus: "Paid"``, of
        which the latter is the more specific.

        Individual entries inside ``order.transactions[]`` are deliberately NOT
        consulted. A single order can legitimately carry several failed attempts
        followed by a success, all in one payload, and reading a sub-transaction
        would report a live payment as failed.
        """
        order = cls._order(body)
        raw = order.get("detailedStatus") or order.get("status") or body.get("status")
        if raw is None:
            # No status field at all: responseCode already said the call
            # succeeded, so treat it as success rather than inventing a state.
            return TransactionStatus.SUCCESS, None
        text = str(raw).strip().lower()
        return _STATUS_MAP.get(text, TransactionStatus.PENDING), str(raw)

    @classmethod
    def _token_id(cls, body: Mapping[str, Any]) -> Optional[str]:
        """
        The stored-card reference, wherever the payload carries it.

        On a tokenizing payment it arrives as ``order.tokenId`` in the callback
        (docs/geidea/01); on a save-card session response it appears under
        ``session.tokenId`` and is null until the customer completes the flow.
        """
        order = cls._order(body)
        session = body.get("session") if isinstance(body.get("session"), dict) else {}
        value = order.get("tokenId") or session.get("tokenId") or body.get("tokenId")
        return str(value) if value else None

    @staticmethod
    def _session_id(body: Mapping[str, Any]) -> Optional[str]:
        session = body.get("session")
        if isinstance(session, dict):
            value = session.get("id") or session.get("sessionId")
            if value:
                return str(value)
        value = body.get("sessionId")
        return str(value) if value else None

    @staticmethod
    def _redirect_url(body: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """
        Find the hosted payment page URL, and report which field it came from.

        The field name is not pinned down in the material available, so several
        known spellings are tried and the one that matched is recorded on the
        result and in the audit log. That turns "Geidea changed its response
        shape" into a visible fact rather than a KeyError.

        TODO(docs): confirm the canonical field name against
        https://docs.geidea.net/docs/geidea-checkout-v2.md
        """
        session = body.get("session") if isinstance(body.get("session"), dict) else {}
        for candidate in ("redirectUrl", "redirect_url", "url", "paymentUrl", "checkoutUrl"):
            value = session.get(candidate) or body.get(candidate)
            if value:
                return str(value), candidate
        return None, None

    def _card_payload(self, card: RawCard) -> dict[str, Any]:
        """
        Build the ``paymentMethod`` object.

        The PAN and CVV in the returned dict are redacted by the shared client
        before anything is written to ``api_call_logs`` (§0.8) -- that happens
        in one place so no adapter can forget it.
        """
        return {
            "cardNumber": card.number,
            "cardholderName": card.holder_name or "",
            "expiryMonth": int(card.exp_month),
            "expiryYear": int(card.exp_year),
            "cvv": card.cvv,
        }

    def _session_body(
        self, order: OrderContext, *, stamp: Optional[str] = None, **overrides: Any
    ) -> dict[str, Any]:
        """
        Build a Session Creation body.

        ``stamp`` is injectable because MIT signs a later call over the same
        timestamp this one carries; generating a fresh one there would sign a
        value Geidea never saw.
        """
        creds = self.require_credentials(CRED_PUBLIC_KEY, CRED_API_PASSWORD)
        stamp = stamp or timestamp(self.timestamp_format)
        amount = format_amount(order.amount_minor, order.currency)
        values = {
            "publicKey": creds[CRED_PUBLIC_KEY],
            "amount": amount,
            "currency": order.currency,
            "merchantReferenceId": order.merchant_reference,
            "timestamp": stamp,
        }
        body: dict[str, Any] = {
            "amount": amount,
            "currency": order.currency,
            "merchantReferenceId": order.merchant_reference,
            "timestamp": stamp,
            "signature": build_signature(
                api_password=creds[CRED_API_PASSWORD],
                values=values,
                fields=self.signature_fields or DEFAULT_SIGNATURE_FIELDS,
            ),
        }
        # Both URLs come from the service layer, which built them from
        # PUBLIC_BASE_URL. The adapter never constructs one itself.
        if order.callback_url:
            body["callbackUrl"] = order.callback_url
        if order.return_url:
            body["returnUrl"] = order.return_url
        if order.customer_email:
            body["customerEmail"] = order.customer_email
        body.update(overrides)
        return body

    async def _create_session(
        self,
        client: GatewayHTTPClient,
        order: OrderContext,
        *,
        stamp: Optional[str] = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        call = await client.call(
            step_name=ep.CREATE_SESSION.step_name,
            method=ep.CREATE_SESSION.method,
            url=self._url(ep.CREATE_SESSION),
            json_body=self._session_body(order, stamp=stamp, **overrides),
            raise_on_status=False,
        )
        return self._parse(call, "create session")

    # -- (a) hosted checkout ----------------------------------------------

    async def create_hpp_session(self, order: OrderContext) -> HPPSessionResult:
        """
        Create a hosted checkout session (§4a).

        Doc: https://docs.geidea.net/docs/geidea-checkout-v2.md
        Provenance: endpoint doc-derived; signature construction INFERRED.

        One call. The customer then pays on Geidea's page, and the outcome
        arrives by two independent routes -- the browser return and a
        server-to-server callback. Neither is treated as authoritative here;
        reconciliation is :meth:`query_order`, which the service layer calls
        after either arrives (§4a).
        """
        self.guard_production()
        self._require_documented(ep.CREATE_SESSION, "create_hpp_session")

        async with self.client() as client:
            body = await self._create_session(client, order)
            redirect_url, redirect_field = self._redirect_url(body)
            return HPPSessionResult(
                calls=client.calls,
                raw=body,
                session_id=self._session_id(body),
                redirect_url=redirect_url,
                redirect_field=redirect_field,
                gateway_order_id=self._order_id(body),
            )

    # -- (b, c) direct API -------------------------------------------------

    async def direct_api_sale(self, order: OrderContext, card: RawCard) -> PaymentResult:
        """
        Direct API sale -- authorise and capture in one operation (§4b).

        Docs: pay-v2.md, initiate-authentication-v-2.md, authenticate-payer-v2.md
        Provenance: endpoints doc-derived; signature INFERRED; the
        ``paymentOperation`` enum is only partially known (see endpoints.py).
        """
        return await self._direct_flow(order, card, payment_operation="Pay")

    async def direct_api_auth(self, order: OrderContext, card: RawCard) -> PaymentResult:
        """
        Pre-authorisation, no capture (§4c).

        Doc: https://docs.geidea.net/docs/pay-v2.md

        Geidea documents pre-authorisation as Saudi-platform-only. A merchant
        account without it produces :class:`MerchantCapabilityError`, which the
        API surfaces as "not supported for this merchant" rather than a generic
        failure -- see :meth:`_is_merchant_capability_error` for the caveat on
        how that is currently detected.
        """
        return await self._direct_flow(order, card, payment_operation="Authorize")

    async def _direct_flow(
        self, order: OrderContext, card: RawCard, *, payment_operation: str
    ) -> PaymentResult:
        """
        The Direct API sequence: create session, initiate authentication,
        authenticate payer (when a challenge is expected), pay.

        The number of calls is measured, not assumed. Authenticate Payer is
        skipped when the initiate response carries an explicit frictionless
        marker -- whether that is permitted is the open question this benchmark
        exists to answer, so the adapter records what the sandbox accepted
        rather than hardcoding a count either way.
        """
        self.guard_production()
        for endpoint in (
            ep.CREATE_SESSION, ep.INITIATE_AUTHENTICATION, ep.AUTHENTICATE_PAYER, ep.PAY
        ):
            self._require_documented(endpoint, f"direct_api_{payment_operation.lower()}")

        card_payload = self._card_payload(card)

        async with self.client() as client:
            session_body = await self._create_session(client, order)
            session_id = self._session_id(session_body)
            if not session_id:
                raise GatewayDeclined(
                    "Geidea create session returned no session id.", detail=session_body
                )

            initiate_call = await client.call(
                step_name=ep.INITIATE_AUTHENTICATION.step_name,
                method=ep.INITIATE_AUTHENTICATION.method,
                url=self._url(ep.INITIATE_AUTHENTICATION),
                json_body={"sessionId": session_id, "paymentMethod": card_payload},
                raise_on_status=False,
            )
            initiate_body = self._parse(initiate_call, "initiate authentication")

            if self.force_full_3ds or self._challenge_expected(initiate_body):
                await client.call(
                    step_name=ep.AUTHENTICATE_PAYER.step_name,
                    method=ep.AUTHENTICATE_PAYER.method,
                    url=self._url(ep.AUTHENTICATE_PAYER),
                    json_body={
                        "sessionId": session_id,
                        "paymentMethod": card_payload,
                        "returnUrl": order.return_url,
                    },
                    raise_on_status=False,
                )

            pay_call = await client.call(
                step_name=ep.PAY.step_name,
                method=ep.PAY.method,
                url=self._url(ep.PAY),
                json_body={
                    "sessionId": session_id,
                    "paymentMethod": card_payload,
                    "paymentOperation": payment_operation,
                },
                raise_on_status=False,
            )
            pay_body = self._parse(pay_call, "pay")

            status, _raw_status = self._status(pay_body)
            return PaymentResult(
                calls=client.calls,
                raw=pay_body,
                status=status,
                amount_minor=order.amount_minor,
                currency=order.currency,
                gateway_order_id=self._order_id(pay_body),
                gateway_transaction_id=self._transaction_id(pay_body),
                card=card.safe_details(self._response_brand(pay_body)),
            )

    @staticmethod
    def _response_brand(body: Mapping[str, Any]) -> Optional[str]:
        """Prefer the gateway's own brand over IIN detection when present."""
        order = body.get("order") if isinstance(body.get("order"), dict) else {}
        method = order.get("paymentMethod") if isinstance(order.get("paymentMethod"), dict) else {}
        brand = method.get("brand") or method.get("type") or body.get("cardBrand")
        return str(brand).upper() if brand else None

    @staticmethod
    def _challenge_expected(initiate: Mapping[str, Any]) -> bool:
        """True unless the response explicitly says no challenge is coming."""
        blob = " ".join(str(value) for value in initiate.values()).lower()
        return not any(marker in blob for marker in _FRICTIONLESS_MARKERS)

    # -- (d) capture -------------------------------------------------------

    async def capture(
        self, transaction: Any, amount_minor: Optional[int] = None
    ) -> PaymentResult:
        """
        Capture an authorisation, in full or in part (§4d).

        Doc: https://docs.geidea.net/reference/capture-transaction-1
        Provenance: doc-derived (``orderId``, optional ``captureAmount``).

        Multiple partial captures against one authorisation are supported --
        the running total lives on the parent transaction, and the caller has
        already validated the remaining capturable amount. The adapter
        re-checks it anyway, because a capture that exceeds the authorisation
        is worth refusing locally rather than discovering from a decline.
        """
        self.guard_production()
        self.require(Capability.CAPTURE)
        self._require_documented(ep.CAPTURE, "capture")

        order_id = getattr(transaction, "gateway_order_id", None)
        if not order_id:
            raise InvalidTransactionState(
                "This transaction has no Geidea orderId, so it cannot be captured."
            )

        currency = getattr(transaction, "currency", None) or "SAR"
        remaining = getattr(transaction, "remaining_capturable_minor", None)
        amount = amount_minor if amount_minor is not None else remaining
        if amount is None or amount <= 0:
            raise InvalidTransactionState("There is nothing left to capture.")
        if remaining is not None and amount > remaining:
            raise InvalidTransactionState(
                f"Capture of {amount} exceeds the remaining capturable amount "
                f"of {remaining} (minor units)."
            )

        body: dict[str, Any] = {
            "orderId": str(order_id),
            "captureAmount": format_amount(amount, currency),
        }

        async with self.client() as client:
            call = await client.call(
                step_name=ep.CAPTURE.step_name,
                method=ep.CAPTURE.method,
                url=self._url(ep.CAPTURE),
                json_body=body,
                raise_on_status=False,
            )
            parsed = self._parse(call, "capture")
            # The *capture* succeeded if the response code says so -- which is
            # what _check_response_code already established, since it raises
            # otherwise. Do not read the order's aggregate status here: an
            # order that is partially captured is not a pending capture, and
            # mapping the order's state onto this operation's row would leave a
            # completed partial capture sitting in PENDING forever.
            return PaymentResult(
                calls=client.calls, raw=parsed, status=TransactionStatus.SUCCESS,
                amount_minor=amount, currency=currency,
                gateway_order_id=self._order_id(parsed) or str(order_id),
                gateway_transaction_id=self._transaction_id(parsed),
            )

    # -- (e) refund --------------------------------------------------------

    async def refund(
        self, transaction: Any, amount_minor: Optional[int] = None
    ) -> PaymentResult:
        """
        Refund a captured payment, in full or in part (§4e).

        Doc: https://docs.geidea.net/docs/refund-2.md
        Provenance: doc-derived (``orderId``, ``refundAmount``, ``signature``);
        signature construction INFERRED.

        Refunding an authorisation that has never been captured is refused here
        rather than sent to Geidea, and the API blocks the control in the UI
        for the same reason.
        """
        self.guard_production()
        self.require(Capability.REFUND)
        self._require_documented(ep.REFUND, "refund")

        order_id = getattr(transaction, "gateway_order_id", None)
        if not order_id:
            raise InvalidTransactionState(
                "This transaction has no Geidea orderId, so it cannot be refunded."
            )

        currency = getattr(transaction, "currency", None) or "SAR"
        remaining = getattr(transaction, "remaining_refundable_minor", None)
        amount = amount_minor if amount_minor is not None else remaining
        if amount is None or amount <= 0:
            raise InvalidTransactionState(
                "There is nothing refundable on this transaction. An "
                "authorisation must be captured before it can be refunded."
            )
        if remaining is not None and amount > remaining:
            raise InvalidTransactionState(
                f"Refund of {amount} exceeds the refundable balance of "
                f"{remaining} (minor units)."
            )

        creds = self.require_credentials(CRED_PUBLIC_KEY, CRED_API_PASSWORD)
        stamp = timestamp(self.timestamp_format)
        formatted = format_amount(amount, currency)

        body = {
            "orderId": str(order_id),
            "refundAmount": formatted,
            "currency": currency,
            "timestamp": stamp,
            "signature": build_signature(
                api_password=creds[CRED_API_PASSWORD],
                values={
                    "publicKey": creds[CRED_PUBLIC_KEY],
                    "amount": formatted,
                    "currency": currency,
                    "merchantReferenceId": str(order_id),
                    "timestamp": stamp,
                },
                fields=self.signature_fields or DEFAULT_SIGNATURE_FIELDS,
            ),
        }

        async with self.client() as client:
            call = await client.call(
                step_name=ep.REFUND.step_name,
                method=ep.REFUND.method,
                url=self._url(ep.REFUND),
                json_body=body,
                raise_on_status=False,
            )
            parsed = self._parse(call, "refund")
            # As with capture: this row records the refund, not the order.
            return PaymentResult(
                calls=client.calls, raw=parsed, status=TransactionStatus.SUCCESS,
                amount_minor=amount, currency=currency,
                gateway_order_id=self._order_id(parsed) or str(order_id),
                gateway_transaction_id=self._transaction_id(parsed),
            )

    # -- (f) void ----------------------------------------------------------

    async def void(self, transaction: Any) -> PaymentResult:
        """
        Void/reverse an authorisation that has not been captured (§4f).

        Doc: https://docs.geidea.net/docs/void-1.md
        Provenance: doc-derived (``orderId``); void applies to
        authorised-not-captured payments only.
        """
        self.guard_production()
        self.require(Capability.VOID)
        self._require_documented(ep.VOID, "void")

        order_id = getattr(transaction, "gateway_order_id", None)
        if not order_id:
            raise InvalidTransactionState(
                "This transaction has no Geidea orderId, so it cannot be voided."
            )
        captured = getattr(transaction, "captured_amount_minor", 0) or 0
        if captured:
            raise InvalidTransactionState(
                "This authorisation has already been captured; refund it "
                "instead of voiding it."
            )

        async with self.client() as client:
            call = await client.call(
                step_name=ep.VOID.step_name,
                method=ep.VOID.method,
                url=self._url(ep.VOID),
                json_body={"orderId": str(order_id)},
                raise_on_status=False,
            )
            parsed = self._parse(call, "void")
            # As with capture: this row records the void, not the order.
            return PaymentResult(
                calls=client.calls, raw=parsed, status=TransactionStatus.SUCCESS,
                amount_minor=getattr(transaction, "amount_minor", None),
                currency=getattr(transaction, "currency", None),
                gateway_order_id=self._order_id(parsed) or str(order_id),
                gateway_transaction_id=self._transaction_id(parsed),
            )

    # -- (g, h, i) token operations ---------------------------------------

    def _agreement_id(self, order: OrderContext) -> str:
        """
        The consent identifier for a card-on-file agreement.

        Geidea does not issue this -- the merchant invents it at tokenization
        and must replay the exact same value on every later merchant-initiated
        charge (docs/geidea/02). The transaction's own merchant reference is
        used: it is unique, merchant-generated, already durable, and already
        stored against the token, so there is no second identifier to keep in
        sync.
        """
        return order.metadata.get("agreement_id") or order.merchant_reference

    async def tokenize(
        self, card: RawCard, order: Optional[OrderContext] = None
    ) -> TokenResult:
        """
        Stand-alone save-card (§4g).

        Doc: https://docs.geidea.net/docs/save-card.md
        Local copy: docs/geidea/03-standalone-save-card.md
        Provenance: DOC_VERIFIED.

        This is NOT the ``cardOnFile: true`` flow. That one tokenizes during a
        real payment and actually charges the amount. This endpoint exists so a
        card can be saved with no purchase, and Geidea **automatically voids**
        the underlying authorisation once the token is issued -- so this adapter
        must never add a void of its own, which would waste a logged call and
        corrupt the round-trip count.

        One merchant call. The card itself is never sent here: the customer
        enters it on Geidea's page. The token arrives later, on the callback.
        That is why the ``card`` argument is accepted (interface conformance)
        but deliberately unused -- and why nothing about it can leak.
        """
        self.guard_production()
        self.require(Capability.TOKENIZE)
        self._require_documented(ep.SAVE_CARD_SESSION, "tokenize")

        if order is None:
            raise InvalidTransactionState(
                "A save-card session needs a merchant reference and a currency."
            )

        creds = self.require_credentials(CRED_PUBLIC_KEY, CRED_API_PASSWORD)
        stamp = timestamp(self.timestamp_format)
        currency = order.currency
        agreement_id = self._agreement_id(order)

        body: dict[str, Any] = {
            "currency": currency,
            "merchantReferenceId": order.merchant_reference,
            "timeStamp": stamp,
            # Third of Geidea's four signature schemes: timestamp first, then
            # public key, then currency. Not interchangeable with the others.
            "signature": save_card_signature(
                timestamp_value=stamp,
                public_key=creds[CRED_PUBLIC_KEY],
                currency=currency,
                api_password=creds[CRED_API_PASSWORD],
            ),
            "cofAgreement": {
                "id": agreement_id,
                "type": ep.AGREEMENT_TYPE_UNSCHEDULED,
            },
        }
        if order.callback_url:
            body["callbackUrl"] = order.callback_url
        if order.return_url:
            body["returnUrl"] = order.return_url

        async with self.client() as client:
            call = await client.call(
                step_name=ep.SAVE_CARD_SESSION.step_name,
                method=ep.SAVE_CARD_SESSION.method,
                url=self._url(ep.SAVE_CARD_SESSION),
                json_body=body,
                raise_on_status=False,
            )
            parsed = self._parse(call, "save card session")
            redirect_url, redirect_field = self._redirect_url(parsed)
            return TokenResult(
                calls=client.calls,
                raw=parsed,
                # PENDING, not SUCCESS: no token exists until the customer has
                # completed the hosted flow and the callback has arrived.
                status=TransactionStatus.PENDING,
                token_reference=self._token_id(parsed),
                agreement_reference=agreement_id,
                session_id=self._session_id(parsed),
                redirect_url=redirect_url,
                redirect_field=redirect_field,
            )

    async def charge_with_token_cit(self, token: Any, order: OrderContext) -> PaymentResult:
        """
        Customer-initiated charge against a stored token (§4h).

        Doc: https://docs.geidea.net/docs/tokenization.md
        Local copy: docs/geidea/01-tokenization.md
        Provenance: DOC_VERIFIED.

        CIT is not a server-to-server charge. It is the ordinary Session
        Creation call carrying ``tokenId`` instead of card fields, followed by
        the hosted flow -- the customer is present and still supplies CVC and
        OTP. So this makes exactly ONE merchant call and leaves the transaction
        PENDING until the callback settles it.

        That single-call shape is itself a benchmark result: CIT costs one
        merchant round trip, where MIT costs two.
        """
        self.guard_production()
        self.require(Capability.CHARGE_WITH_TOKEN_CIT)
        self._require_documented(ep.CREATE_SESSION, "charge_with_token_cit")

        token_reference = getattr(token, "token_reference", None) or str(token)

        async with self.client() as client:
            parsed = await self._create_session(
                client,
                order,
                tokenId=token_reference,
                paymentOperation=ep.PAYMENT_OPERATION_PAY,
            )
            redirect_url, redirect_field = self._redirect_url(parsed)
            return PaymentResult(
                calls=client.calls,
                raw=parsed,
                status=TransactionStatus.PENDING,
                amount_minor=order.amount_minor,
                currency=order.currency,
                gateway_order_id=self._order_id(parsed),
                token_reference=token_reference,
                session_id=self._session_id(parsed),
                challenge_url=redirect_url,
            )

    async def charge_with_token_mit(self, token: Any, order: OrderContext) -> PaymentResult:
        """
        Merchant-initiated charge against a stored token (§4i).

        Doc: https://docs.geidea.net/docs/merchant-initiated-mit.md
        Local copy: docs/geidea/02-merchant-initiated-mit.md
        Provenance: DOC_VERIFIED.

        TWO merchant calls, and the benchmark's most interesting Geidea result:

          1. Session generation, carrying ``initiatedBy: "Merchant"``,
             ``agreementId``, ``agreementType`` and ``tokenId``. Signed with the
             BASE signature.
          2. ``POST /pgw/api/v2/direct/pay/token``, signed with the MIT
             signature -- HMAC-SHA256 over
             ``{publicKey}{sessionId}{timestamp}``.

        The two calls use DIFFERENT signature algorithms. The doc is explicit
        about this, and warns that using the wrong one produces a clean
        rejection that reads like "MIT does not work".

        Note the field shapes also differ from tokenization: step 1 of
        tokenization nests consent under ``cofAgreement: {id, type}``, while MIT
        session generation carries flat ``agreementId`` / ``agreementType``.
        They are not interchangeable.
        """
        self.guard_production()
        self.require(Capability.CHARGE_WITH_TOKEN_MIT)
        self._require_documented(ep.CREATE_SESSION, "charge_with_token_mit")
        self._require_documented(ep.PAY_WITH_TOKEN, "charge_with_token_mit")

        creds = self.require_credentials(CRED_PUBLIC_KEY, CRED_API_PASSWORD)
        token_reference = getattr(token, "token_reference", None) or str(token)
        agreement_id = (
            getattr(token, "agreement_reference", None) or self._agreement_id(order)
        )

        # The MIT signature covers a timestamp that the pay/token body does not
        # itself carry, so it has to be the one the session was created with --
        # there is no other timestamp in the exchange for Geidea to check
        # against. Reusing the session's stamp is the only reading consistent
        # with the documented field list.
        #
        # TODO(docs): confirm against a sandbox MIT charge. The doc's sample
        # pay/token body shows no timestamp field, which leaves this implicit.
        stamp = timestamp(self.timestamp_format)

        async with self.client() as client:
            session_body = await self._create_session(
                client,
                order,
                stamp=stamp,
                paymentOperation=ep.PAYMENT_OPERATION_PAY,
                initiatedBy=ep.INITIATED_BY_MERCHANT,
                agreementId=agreement_id,
                agreementType=ep.AGREEMENT_TYPE_UNSCHEDULED,
                tokenId=token_reference,
            )
            session_id = self._session_id(session_body)
            if not session_id:
                raise GatewayDeclined(
                    "Geidea MIT session generation returned no session id.",
                    detail=session_body,
                )

            body: dict[str, Any] = {
                # "sessionid", lowercase 'i', exactly as the doc's sample shows.
                # Not normalised to sessionId: the sample is the only evidence,
                # and correcting it would be inventing a field name.
                "sessionid": session_id,
                "initiatedBy": ep.INITIATED_BY_MERCHANT,
                "agreementId": agreement_id,
                "agreementType": ep.AGREEMENT_TYPE_UNSCHEDULED,
                "signature": mit_signature(
                    public_key=creds[CRED_PUBLIC_KEY],
                    session_id=session_id,
                    timestamp_value=stamp,
                    api_password=creds[CRED_API_PASSWORD],
                ),
            }
            if order.callback_url:
                body["callbackUrl"] = order.callback_url

            call = await client.call(
                step_name=ep.PAY_WITH_TOKEN.step_name,
                method=ep.PAY_WITH_TOKEN.method,
                url=self._url(ep.PAY_WITH_TOKEN),
                json_body=body,
                raise_on_status=False,
            )
            parsed = self._parse(call, "MIT pay with token")
            status, _ = self._status(parsed)
            return PaymentResult(
                calls=client.calls,
                raw=parsed,
                status=status,
                amount_minor=order.amount_minor,
                currency=order.currency,
                gateway_order_id=self._order_id(parsed),
                gateway_transaction_id=self._transaction_id(parsed),
                token_reference=token_reference,
                session_id=session_id,
            )

    # -- (j) order query ---------------------------------------------------

    async def query_order(self, transaction: Any) -> OrderStatusResult:
        """
        Fetch an order's authoritative status (§4j).

        Doc: https://docs.geidea.net/docs/fetch-1.md
        Provenance: doc-derived.

        This is the reconciliation call. It runs on demand, after an HPP return
        or callback, and automatically after a timeout so a transaction is
        never left stuck in PENDING with nobody having asked the gateway what
        actually happened.
        """
        self.guard_production()
        self.require(Capability.QUERY_ORDER)
        self._require_documented(ep.ORDER_QUERY, "query_order")

        order_id = (
            getattr(transaction, "gateway_order_id", None)
            or getattr(transaction, "gateway_transaction_id", None)
        )
        if not order_id:
            raise InvalidTransactionState(
                "This transaction has no Geidea orderId to query."
            )

        async with self.client() as client:
            call = await client.call(
                step_name=ep.ORDER_QUERY.step_name,
                method=ep.ORDER_QUERY.method,
                url=self._url(ep.ORDER_QUERY, order_id=str(order_id)),
                raise_on_status=False,
            )
            parsed = self._parse(call, "order query")
            status, raw_status = self._status(parsed)
            return OrderStatusResult(
                calls=client.calls,
                raw=parsed,
                status=status,
                gateway_status=raw_status,
                gateway_order_id=self._order_id(parsed) or str(order_id),
                gateway_transaction_id=self._transaction_id(parsed),
                currency=getattr(transaction, "currency", None),
            )

    # -- (k) webhooks ------------------------------------------------------

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """
        Verify a callback signature (§4k).

        Doc: https://docs.geidea.net/docs/sample-callback-responses.md
        Local copy: docs/geidea/04-webhook-callback-notifications.md
        Provenance: DOC_VERIFIED (field list and reference implementation).

        The signature is a top-level ``signature`` key in the callback BODY,
        not a header, and covers::

            {publicKey}{amount}{currency}{orderId}{status}
            {merchantReferenceId}{timeStamp}

        Two things the page does not pin down: which timestamp field feeds the
        signature (its sample payload shows none), and how the amount is
        rendered (``101`` in the sample -- as a bare integer, or as ``101.00``?).

        So this tries a small, bounded set of candidates and reports whether ANY
        of them reproduces the signature Geidea sent. That is verification by
        trial, not guessing: a match is self-proving, because an HMAC cannot
        match by accident. The combination that worked is logged so the
        ambiguity can be closed permanently once a real sandbox callback has
        been seen.
        """
        self.require(Capability.VERIFY_WEBHOOK)
        self._require_documented(ep.WEBHOOK_SIGNATURE, "verify_webhook")

        payload = self._decode_body(body)
        if not isinstance(payload, dict):
            return False

        received = payload.get("signature")
        if not received or not isinstance(received, str):
            # No signature at all is not a verification failure to be silent
            # about -- it is a callback we cannot authenticate.
            return False

        creds = self.require_credentials(CRED_PUBLIC_KEY, CRED_API_PASSWORD)
        order = self._order(payload)

        order_id = str(order.get("orderId") or payload.get("orderId") or "")
        currency = str(order.get("currency") or payload.get("currency") or "")
        status = str(order.get("status") or payload.get("status") or "")
        reference = str(
            order.get("merchantReferenceId") or payload.get("merchantReferenceId") or ""
        )

        for amount in self._amount_candidates(order, currency):
            for stamp in self._timestamp_candidates(payload, order):
                expected = callback_signature(
                    public_key=creds[CRED_PUBLIC_KEY],
                    amount=amount,
                    currency=currency,
                    order_id=order_id,
                    status=status,
                    merchant_reference_id=reference,
                    timestamp_value=stamp,
                    api_password=creds[CRED_API_PASSWORD],
                )
                if verify_callback_signature(expected, received):
                    logger.info(
                        "callback signature verified",
                        extra={
                            "gateway": self.code,
                            "order_id": order_id,
                            # Recorded so the remaining ambiguity in the doc can
                            # be closed once a real callback has been seen.
                            "amount_rendering": amount,
                            "timestamp_source": stamp or "(empty)",
                        },
                    )
                    return True

        logger.warning(
            "callback signature did not verify against any documented rendering",
            extra={"gateway": self.code, "order_id": order_id},
        )
        return False

    @staticmethod
    def _decode_body(body: bytes) -> Any:
        import json

        try:
            return json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _amount_candidates(order: Mapping[str, Any], currency: str) -> list[str]:
        """
        Renderings of the order amount that the signature might use.

        The sample shows ``"amount": 101`` as a JSON number, which could have
        been signed as ``101`` or as ``101.00``. Both are tried; only one can
        match.
        """
        raw = order.get("amount")
        if raw is None:
            raw = order.get("totalAmount")
        if raw is None:
            return [""]

        candidates: list[str] = []
        text = str(raw)
        candidates.append(text)
        # A JSON number of 101 decodes to int 101 -> "101"; 101.0 -> "101.0".
        if isinstance(raw, float) and raw.is_integer():
            candidates.append(str(int(raw)))
        try:
            from decimal import Decimal

            places = exponent(currency) if currency else 2
            fixed = str(
                Decimal(text).quantize(Decimal(1).scaleb(-places))
            )
            candidates.append(fixed)
        except Exception:
            pass
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _timestamp_candidates(
        payload: Mapping[str, Any], order: Mapping[str, Any]
    ) -> list[str]:
        """
        Fields that might carry the signed timestamp.

        The doc names ``timeStamp`` in the formula but its sample payload shows
        no such field, so the plausible carriers are tried in order, ending with
        the empty string -- which covers the reading that the timestamp
        contributes nothing when absent.
        """
        candidates = [
            payload.get("timeStamp"), payload.get("timestamp"),
            order.get("timeStamp"), order.get("timestamp"),
            order.get("createdDate"), order.get("orderDate"),
        ]
        out = [str(value) for value in candidates if value]
        out.append("")
        return list(dict.fromkeys(out))

    @classmethod
    def callback_reports_success(cls, body: Any) -> bool:
        """
        The documented checklist for treating a callback as a success (§4k).

        From docs/geidea/04: responseCode ``000``, responseMessage ``Success``,
        detailedResponseCode ``000``, detailedResponseMessage ``The operation
        was successful``. ALL must hold.

        A callback whose signature verifies but whose codes do not match is an
        authentic *failure* notice, not a security problem -- the distinction
        matters, and conflating them would either hide real declines or raise
        false alarms about forged callbacks.
        """
        if not isinstance(body, dict):
            return False
        order = cls._order(body)
        for key, expected in ep.CALLBACK_SUCCESS_CHECKS.items():
            actual = body.get(key, order.get(key))
            if actual is None:
                # Codes may sit on the last transaction rather than at the top
                # level, per the tokenization sample's nested "codes" object.
                actual = cls._last_transaction_code(order, key)
            if str(actual) != expected:
                return False
        return True

    @staticmethod
    def _last_transaction_code(order: Mapping[str, Any], key: str) -> Any:
        transactions = order.get("transactions")
        if not isinstance(transactions, list) or not transactions:
            return None
        last = transactions[-1]
        if not isinstance(last, dict):
            return None
        codes = last.get("codes")
        return codes.get(key) if isinstance(codes, dict) else None

    def parse_webhook(self, body: Any) -> WebhookEvent:
        """
        Reduce a callback to the fields reconciliation needs.

        Reads the OUTER order status only. docs/geidea/04 is explicit that a
        single callback consolidates every attempt against an order into one
        ``transactions[]`` array -- several failures followed by a success is a
        normal, successful payload, and reading a sub-transaction's status
        would report it as failed.

        The full array is still stored verbatim in
        ``webhooks_received.body_json``, because it is the evidence of what
        happened even though it does not drive the status.
        """
        if not isinstance(body, dict):
            return WebhookEvent(raw=body)
        order = self._order(body)
        status_value = (
            order.get("detailedStatus") or order.get("status") or body.get("status")
        )
        reference = order.get("merchantReferenceId") or body.get("merchantReferenceId")
        message = (
            body.get("detailedResponseMessage")
            or body.get("responseMessage")
            or self._last_transaction_code(order, "detailedResponseMessage")
        )
        code = body.get("responseCode") or self._last_transaction_code(
            order, "responseCode"
        )

        amount_minor = None
        currency = order.get("currency") or body.get("currency")
        raw_amount = order.get("amount")
        if raw_amount is not None and currency:
            try:
                from decimal import Decimal

                amount_minor = int(
                    Decimal(str(raw_amount)).scaleb(exponent(str(currency)))
                )
            except Exception:
                amount_minor = None

        return WebhookEvent(
            # Geidea sends no dedicated event id, so the order id plus its
            # status is the dedupe key: a replay of the same notification
            # carries both unchanged.
            event_reference=(
                f"{order.get('orderId')}:{status_value}"
                if order.get("orderId")
                else None
            ),
            merchant_reference=str(reference) if reference else None,
            gateway_order_id=self._order_id(body),
            gateway_transaction_id=self._transaction_id(body),
            status=str(status_value) if status_value else None,
            amount_minor=amount_minor,
            currency=str(currency) if currency else None,
            error_code=str(code) if code else None,
            error_message=str(message) if message else None,
            raw=body,
        )

    @classmethod
    def token_from_webhook(cls, body: Any) -> Optional[str]:
        """The ``tokenId`` a tokenizing payment or save-card flow issued (§4g)."""
        return cls._token_id(body) if isinstance(body, dict) else None

    @staticmethod
    def normalise_status(raw: str) -> TransactionStatus:
        """Expose the status mapping for the webhook reconciliation service."""
        return _STATUS_MAP.get(str(raw).strip().lower(), TransactionStatus.PENDING)
