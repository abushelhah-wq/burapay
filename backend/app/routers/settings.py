"""
Settings page: credentials, gateway config, runtime flags (§7b).

Credential values go in and are never seen again. Every response here is masked
to the last four characters, including for an administrator -- there is no
"reveal" endpoint, because a credential store you can read back is a credential
store that leaks through any bug in access control.

The "test connection" action fires exactly one harmless, read-only sandbox call
before confirming a save, so a wrong key is caught immediately rather than at
the next payment attempt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.gateways.errors import (
    DocumentationRequiredError, GatewayError, UnsupportedOperationError,
)
from app.gateways.registry import adapter_class_or_none
from app.logging_setup import get_logger
from app.models import Gateway, User
from app.schemas.settings import (
    CredentialIn, CredentialOut, GatewayConfigIn, GatewayCredentialsOut, RuntimeFlags,
)
from app.schemas.gateways import TestConnectionOut
from app.security.auth import require_admin
from app.security.credentials import CredentialStore
from app.services import execution
from app.timeutil import duration_ms, utcnow

router = APIRouter(prefix="/api/settings", tags=["settings"])
logger = get_logger(__name__)


async def _gateway(session: AsyncSession, code: str) -> Gateway:
    result = await session.execute(select(Gateway).where(Gateway.code == code))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown gateway {code!r}.")
    return gateway


@router.get("/flags", response_model=RuntimeFlags)
async def runtime_flags() -> RuntimeFlags:
    """
    Report the server-side gates so the UI can explain what it is doing.

    This endpoint is a mirror, never the enforcement. Each flag is checked
    again on the request that would act on it.
    """
    settings = get_settings()
    return RuntimeFlags(
        allow_direct_card_entry=settings.ALLOW_DIRECT_CARD_ENTRY,
        allow_production_gateways=settings.ALLOW_PRODUCTION_GATEWAYS,
        benchmark_min_interval_seconds=settings.BENCHMARK_MIN_INTERVAL_SECONDS,
        benchmark_max_transactions_per_run=settings.BENCHMARK_MAX_TRANSACTIONS_PER_RUN,
        http_timeout_seconds=settings.HTTP_TIMEOUT_SECONDS,
        public_base_url=settings.public_base_url,
        public_base_is_https=settings.public_base_is_https,
        measurement_location=settings.MEASUREMENT_LOCATION or None,
    )


@router.get("/gateways/{code}/credentials", response_model=GatewayCredentialsOut)
async def get_credentials(
    code: str,
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> GatewayCredentialsOut:
    gateway = await _gateway(session, code)
    adapter_class = adapter_class_or_none(code)
    store = CredentialStore(session)

    described = await store.describe(gateway.id)
    stored = {str(item["key_name"]) for item in described}
    required = list(adapter_class.required_credentials) if adapter_class else []

    return GatewayCredentialsOut(
        gateway_code=gateway.code,
        display_name=gateway.display_name,
        required_credentials=required,
        optional_credentials=(
            list(adapter_class.optional_credentials) if adapter_class else []
        ),
        credentials=[CredentialOut(**item) for item in described],
        missing=[key for key in required if key not in stored],
    )


@router.put("/gateways/{code}/credentials", response_model=GatewayCredentialsOut)
async def put_credential(
    code: str,
    payload: CredentialIn,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> GatewayCredentialsOut:
    """Store one credential, encrypted. Update in place if it already exists."""
    gateway = await _gateway(session, code)
    store = CredentialStore(session)
    await store.put(
        gateway_id=gateway.id,
        key_name=payload.key_name,
        value=payload.value,
        is_production=payload.is_production,
        user_id=admin.id,
    )
    await session.commit()
    return await get_credentials(code, session, admin)


@router.delete("/gateways/{code}/credentials/{key_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    code: str,
    key_name: str,
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> None:
    gateway = await _gateway(session, code)
    store = CredentialStore(session)
    if not await store.delete(gateway_id=gateway.id, key_name=key_name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No credential {key_name!r}.")
    await session.commit()


@router.put("/gateways/{code}/config")
async def put_config(
    code: str,
    payload: GatewayConfigIn,
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> dict[str, object]:
    """
    Update non-secret adapter configuration (region, API base, flags).

    Refuses anything that looks like a credential: those belong in the
    encrypted store, and accepting one here would write a plaintext secret to
    ``gateways.config_json``.
    """
    gateway = await _gateway(session, code)
    from app.redaction import classify_key

    rejected = [key for key in payload.config if classify_key(key) != "safe"]
    if rejected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "code": "credential_in_config",
                "message": (
                    "Configuration must not contain credentials; they belong in "
                    "the encrypted credential store."
                ),
                "rejected_keys": rejected,
            },
        )

    gateway.config_json = {**(gateway.config_json or {}), **payload.config}
    if payload.enabled is not None:
        gateway.enabled = payload.enabled
    await session.commit()
    return {"gateway_code": gateway.code, "config": gateway.config_json,
            "enabled": gateway.enabled}


@router.post("/gateways/{code}/test-connection", response_model=TestConnectionOut)
async def test_connection(
    code: str,
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> TestConnectionOut:
    """
    Fire one harmless read-only sandbox call to confirm the credentials work (§7b).

    Uses an order query against a reference that does not exist. That is the
    cheapest call that still exercises authentication: a credential problem
    answers differently from "no such order", and nothing is created in the
    merchant's sandbox.
    """
    gateway = await _gateway(session, code)
    adapter_class = adapter_class_or_none(code)
    if adapter_class is None:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED, f"No adapter for {code!r}."
        )

    started = utcnow()
    checked = "order query against a non-existent reference"

    class _Probe:
        """Minimal stand-in for a transaction, for a read-only status call."""

        gateway_order_id = "BURAPAY-CONNECTION-TEST"
        gateway_transaction_id = None
        currency = get_settings().TEST_CURRENCY
        amount_minor = 0

    try:
        adapter = await execution.build_adapter(session, gateway, transaction_id=None)
        await adapter.query_order(_Probe())
        message = (
            "Credentials accepted: the gateway answered the probe call. "
            "Nothing was created in your sandbox."
        )
        ok = True
    except (UnsupportedOperationError, DocumentationRequiredError) as exc:
        ok = False
        message = (
            f"Could not test this gateway: {exc.message}"
        )
    except GatewayError as exc:
        # A decline on a non-existent order still proves authentication worked.
        # Only an auth/credential failure means the keys are wrong.
        ok = exc.code in {"declined", "http_status"}
        message = (
            (
                "Credentials appear valid: the gateway rejected the probe "
                f"reference, which is expected. It said: {exc.message}"
            )
            if ok
            else f"Connection test failed: {exc.message}"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        message = f"Connection test failed: {exc}"
        logger.exception("connection test raised", extra={"gateway": code})

    return TestConnectionOut(
        ok=ok,
        message=message,
        checked=checked,
        duration_ms=duration_ms(started, utcnow()),
    )
