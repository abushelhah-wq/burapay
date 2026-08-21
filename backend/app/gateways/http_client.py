"""
The single shared gateway HTTP client (§0.3).

**This is the only module in the application permitted to import httpx.**
``tests/test_no_direct_http.py`` enforces that by scanning the source tree, so
the rule is checked rather than merely stated. Everything an outbound gateway
call must do happens here, once:

1. Authenticate/sign the request according to that gateway's spec, via a
   :class:`GatewayAuth` supplied by the adapter.
2. Write an ``api_call_logs`` row **before** the request leaves, and commit it
   on its own connection.
3. Send with an explicit timeout, and record that timeout as a field so
   "gateway X times out more than gateway Y" is a measured output (§6).
4. Complete the same row after the response, the timeout, or the failure --
   whichever happens. There is no path through this function that sends a
   request and leaves no row.
5. Return a normalized :class:`~app.gateways.results.GatewayCall`.

Why the log row commits on its own connection: the audit trail has to survive
the business transaction rolling back. If both shared one session, a handler
that raised would roll back its logs too, and a failed operation -- exactly the
case worth investigating -- would be the one case with no evidence.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Protocol

import httpx
from sqlalchemy import func, select, update

from app.db import session_scope
from app.gateways.errors import (
    GatewayHTTPStatusError, GatewayNetworkError, GatewayResponseParseError,
    GatewayTimeoutError,
)
from app.gateways.results import GatewayCall
from app.logging_setup import get_logger
from app.models import ApiCallLog, Transaction
from app.redaction import redact_body, redact_headers
from app.timeutil import duration_ms, utcnow

logger = get_logger(__name__)

#: Response bodies larger than this are truncated before storage. A gateway
#: returning an HTML error page can otherwise put megabytes into a JSONB
#: column, and nothing past this point is diagnostically useful.
MAX_STORED_BODY_CHARS = 64_000


class GatewayAuth(Protocol):
    """
    Per-gateway authentication/signing, applied by the shared client.

    Returning the body allows a gateway whose signature is carried *inside* the
    payload (rather than in a header) to add it here, so signing is still
    something the shared client does rather than something each call site
    remembers to do.
    """

    def apply(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Any,
    ) -> tuple[dict[str, str], Any]:
        ...


class NoAuth:
    """For unauthenticated calls (a public status page, a health probe)."""

    def apply(
        self, *, method: str, url: str, headers: dict[str, str], body: Any
    ) -> tuple[dict[str, str], Any]:
        return headers, body


def _truncate(text: str) -> str:
    if len(text) <= MAX_STORED_BODY_CHARS:
        return text
    return text[:MAX_STORED_BODY_CHARS] + f"...[truncated, {len(text)} chars total]"


class GatewayHTTPClient:
    """
    Makes and logs every outbound call for one adapter operation.

    An instance is scoped to a single operation (one ``transactions`` row), and
    its ``calls`` list becomes that transaction's ordered audit trail. Use it
    as an async context manager so the underlying connection pool is closed.
    """

    def __init__(
        self,
        *,
        gateway_id: int,
        gateway_code: str,
        timeout_seconds: float,
        auth: Optional[GatewayAuth] = None,
        transaction_id: Optional[int] = None,
        verify: bool = True,
    ) -> None:
        self.gateway_id = gateway_id
        self.gateway_code = gateway_code
        self.timeout_seconds = float(timeout_seconds)
        self.auth = auth or NoAuth()
        self.transaction_id = transaction_id
        self._verify = verify
        self._sequence = 0
        self._client: httpx.AsyncClient | None = None
        #: Every call made through this client, in order. The adapter returns
        #: this as ``result.calls``; ``request_count`` is its length.
        self.calls: list[GatewayCall] = []

    async def __aenter__(self) -> "GatewayHTTPClient":
        self._client = httpx.AsyncClient(verify=self._verify, follow_redirects=False)
        return self

    async def _ensure_sequence_base(self) -> None:
        """
        Continue the transaction's existing call sequence rather than restart it.

        ``sequence_number`` is the order of a call *within the transaction's
        flow* (§2), and a transaction can be touched by more than one client:
        a reconciliation query after a timeout, or an on-demand order query,
        both belong to the same flow. Starting each client at 1 would collide
        with the unique (transaction_id, sequence_number) constraint and, worse,
        would make the audit trail claim two different calls were both first.
        """
        if self._sequence or self.transaction_id is None:
            return
        try:
            async with session_scope() as session:
                highest = await session.scalar(
                    select(func.max(ApiCallLog.sequence_number)).where(
                        ApiCallLog.transaction_id == self.transaction_id
                    )
                )
            self._sequence = int(highest or 0)
        except Exception:
            logger.exception(
                "could not determine the existing call sequence; starting from 0",
                extra={"transaction_id": self.transaction_id},
            )

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- logging ----------------------------------------------------------

    async def _open_log(
        self,
        *,
        sequence_number: int,
        step_name: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Any,
        timeout_seconds: float,
        started_at: Any,
    ) -> Optional[int]:
        """
        Write the pre-flight log row and commit it immediately.

        Returns the row id, or ``None`` if the log write itself failed. A
        logging failure must never stop a payment from being attempted -- it is
        recorded to the application log and the call proceeds, because losing a
        log line is strictly better than losing the operator's money in a
        half-known state.
        """
        try:
            async with session_scope() as session:
                row = ApiCallLog(
                    transaction_id=self.transaction_id,
                    gateway_id=self.gateway_id,
                    sequence_number=sequence_number,
                    step_name=step_name,
                    http_method=method.upper(),
                    url=url,
                    request_headers_json=redact_headers(headers),
                    request_body_json=redact_body(body) if body is not None else None,
                    timeout_seconds=timeout_seconds,
                    started_at=started_at,
                )
                session.add(row)
                await session.flush()
                return row.id
        except Exception:
            logger.exception(
                "failed to write pre-flight api_call_log row",
                extra={
                    "gateway": self.gateway_code,
                    "step": step_name,
                    "transaction_id": self.transaction_id,
                },
            )
            return None

    async def _close_log(self, log_id: Optional[int], **values: Any) -> None:
        """Complete the log row. Never raises into the caller's path."""
        if log_id is None:
            return
        try:
            async with session_scope() as session:
                await session.execute(
                    update(ApiCallLog).where(ApiCallLog.id == log_id).values(**values)
                )
        except Exception:
            logger.exception(
                "failed to complete api_call_log row", extra={"log_id": log_id}
            )

    async def _bump_request_count(self) -> None:
        """
        Increment ``transactions.request_count`` as each call completes.

        The service layer also sets the final count from ``len(result.calls)``.
        Doing it live as well is what makes the count correct for an operation
        that crashed halfway: the row shows two calls made, and two log rows
        exist to match (§6).
        """
        if self.transaction_id is None:
            return
        try:
            async with session_scope() as session:
                await session.execute(
                    update(Transaction)
                    .where(Transaction.id == self.transaction_id)
                    .values(request_count=Transaction.request_count + 1)
                )
        except Exception:
            logger.exception(
                "failed to increment request_count",
                extra={"transaction_id": self.transaction_id},
            )

    async def attach_transaction(self, transaction_id: int) -> None:
        """
        Bind calls made before the transaction row existed (§2).

        Used by the HPP flow, where a session is created before an order ID is
        known. Backfills every unattached row this client wrote so the audit
        trail is complete from the first call.
        """
        previous, self.transaction_id = self.transaction_id, transaction_id
        if previous is not None:
            return
        ids = [call.log_id for call in self.calls if call.log_id is not None]
        if not ids:
            return
        try:
            async with session_scope() as session:
                await session.execute(
                    update(ApiCallLog)
                    .where(ApiCallLog.id.in_(ids))
                    .values(transaction_id=transaction_id)
                )
                await session.execute(
                    update(Transaction)
                    .where(Transaction.id == transaction_id)
                    .values(request_count=Transaction.request_count + len(ids))
                )
        except Exception:
            logger.exception(
                "failed to attach api_call_log rows to transaction",
                extra={"transaction_id": transaction_id},
            )

    # -- the call ---------------------------------------------------------

    async def call(
        self,
        *,
        step_name: str,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        form_body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        raise_on_status: bool = True,
        expect_json: bool = True,
    ) -> GatewayCall:
        """
        Make one authenticated, logged, timed HTTP call.

        ``raise_on_status`` defaults to True. Adapters that need to read the
        body of a 4xx -- which is where most gateways put their error code --
        pass ``False`` and inspect ``call.status_code`` themselves.
        """
        if self._client is None:
            raise RuntimeError(
                "GatewayHTTPClient must be used as an async context manager."
            )

        await self._ensure_sequence_base()
        self._sequence += 1
        sequence_number = self._sequence
        effective_timeout = (
            float(timeout_seconds) if timeout_seconds is not None else self.timeout_seconds
        )

        request_headers = {"Accept": "application/json", **(dict(headers) if headers else {})}
        body: Any = json_body
        request_headers, body = self.auth.apply(
            method=method.upper(), url=url, headers=request_headers, body=body
        )

        logged_body: Any = body if body is not None else (dict(form_body) if form_body else None)

        started_at = utcnow()
        log_id = await self._open_log(
            sequence_number=sequence_number,
            step_name=step_name,
            method=method,
            url=url,
            headers=request_headers,
            body=logged_body,
            timeout_seconds=effective_timeout,
            started_at=started_at,
        )

        def finish(
            *,
            status_code: Optional[int],
            response_json: Any,
            response_text: Optional[str],
            response_headers: Optional[dict[str, str]],
            error_text: Optional[str],
        ) -> GatewayCall:
            completed_at = utcnow()
            call = GatewayCall(
                log_id=log_id,
                sequence_number=sequence_number,
                step_name=step_name,
                method=method.upper(),
                url=url,
                status_code=status_code,
                response_json=response_json,
                response_text=response_text,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms(started_at, completed_at),
                timeout_seconds=effective_timeout,
                error_text=error_text,
            )
            self.calls.append(call)
            return call

        # -- network layer -------------------------------------------------
        try:
            response = await self._client.request(
                method.upper(),
                url,
                headers=request_headers,
                json=body if form_body is None else None,
                data=dict(form_body) if form_body is not None else None,
                params=dict(params) if params else None,
                timeout=effective_timeout,
            )
        except httpx.TimeoutException as exc:
            message = f"{self.gateway_code} {step_name} timed out after {effective_timeout}s"
            call = finish(
                status_code=None, response_json=None, response_text=None,
                response_headers=None, error_text=f"{message}: {exc!r}",
            )
            await self._close_log(
                log_id, completed_at=call.completed_at, duration_ms=call.duration_ms,
                error_text=call.error_text,
            )
            await self._bump_request_count()
            raise GatewayTimeoutError(message, timeout_seconds=effective_timeout) from exc
        except httpx.HTTPError as exc:
            # DNS failure, TLS failure, connection reset -- the request never
            # completed, which is a different measurement from a slow response.
            message = f"{self.gateway_code} {step_name} network error: {exc!r}"
            call = finish(
                status_code=None, response_json=None, response_text=None,
                response_headers=None, error_text=message,
            )
            await self._close_log(
                log_id, completed_at=call.completed_at, duration_ms=call.duration_ms,
                error_text=message,
            )
            await self._bump_request_count()
            raise GatewayNetworkError(message) from exc

        # -- body parsing --------------------------------------------------
        raw_text = response.text
        parsed: Any = None
        parse_error: Optional[str] = None
        if expect_json:
            try:
                parsed = json.loads(raw_text) if raw_text.strip() else None
            except ValueError as exc:
                parse_error = (
                    f"{self.gateway_code} {step_name} returned HTTP "
                    f"{response.status_code} with a body that is not JSON: {exc}"
                )

        stored_body = redact_body(parsed) if parsed is not None else (
            redact_body(_truncate(raw_text)) if raw_text else None
        )

        call = finish(
            status_code=response.status_code,
            response_json=parsed,
            response_text=raw_text,
            response_headers=dict(response.headers),
            error_text=parse_error,
        )
        await self._close_log(
            log_id,
            response_status_code=response.status_code,
            response_headers_json=redact_headers(response.headers),
            response_body_json=stored_body,
            completed_at=call.completed_at,
            duration_ms=call.duration_ms,
            error_text=parse_error,
        )
        await self._bump_request_count()

        # -- outcome classification (§6: three distinct failure modes) -----
        if not call.ok and raise_on_status:
            raise GatewayHTTPStatusError(
                f"{self.gateway_code} {step_name} returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=stored_body,
            )
        if parse_error and call.ok:
            # A 2xx we cannot read is an error: we do not know whether money
            # moved, and guessing either way would corrupt the audit trail.
            raise GatewayResponseParseError(parse_error)

        return call
