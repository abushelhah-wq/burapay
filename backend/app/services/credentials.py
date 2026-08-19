"""
Gateway credential storage.

Credentials are written encrypted and read back in two different ways depending on
who is asking:

* :func:`masked_credentials` — for the API and the UI. Secrets come back as
  ``sk_test_************X92D``, never in full (specification section 5).
* :func:`load_credentials` — for the benchmark engine only. Returns plaintext, held
  in memory for the duration of one transaction and never serialised into a response.

Saving is a merge, not a replace: submitting the Settings form without retyping every
secret keeps the ones already stored. That is what makes masked display workable —
the UI can show a mask and send it back untouched without destroying the value.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_dict, encrypt_dict, mask
from app.gateways.registry import adapter_class, credential_schema, secret_fields
from app.models import Gateway, GatewayCredential

#: What the UI sends back for a secret the user did not retype.
MASK_SENTINEL_CHARS = "*"


async def get_gateway(session: AsyncSession, code: str) -> Optional[Gateway]:
    result = await session.execute(select(Gateway).where(Gateway.code == code))
    return result.scalar_one_or_none()


async def _row(session: AsyncSession, gateway_id: str,
               environment: str) -> Optional[GatewayCredential]:
    result = await session.execute(
        select(GatewayCredential).where(
            GatewayCredential.gateway_id == gateway_id,
            GatewayCredential.environment == environment))
    return result.scalar_one_or_none()


async def load_credentials(session: AsyncSession, code: str,
                           environment: str = "sandbox") -> Dict[str, Any]:
    """Plaintext credentials for the benchmark engine. Never returned by the API."""
    gateway = await get_gateway(session, code)
    if gateway is None:
        return {}
    row = await _row(session, gateway.id, environment)
    if row is None:
        return {}
    return decrypt_dict(row.encrypted_credentials)


async def masked_credentials(session: AsyncSession, code: str,
                             environment: str = "sandbox") -> Dict[str, Any]:
    """Credentials with every secret field masked, safe to send to a client."""
    values = await load_credentials(session, code, environment)
    secrets = secret_fields(code)
    return {key: (mask(value) if key in secrets else value)
            for key, value in values.items()}


def _is_mask(value: Any) -> bool:
    """True when the client echoed a mask back instead of a new secret."""
    text = str(value or "")
    return bool(text) and set(text) <= set(MASK_SENTINEL_CHARS + "_abcdefghijklmnopqrstuvwxyz0123456789") \
        and text.count(MASK_SENTINEL_CHARS) >= 4


async def save_credentials(session: AsyncSession, code: str, environment: str,
                           values: Dict[str, Any], *,
                           updated_by: Optional[str] = None) -> Dict[str, Any]:
    """Merge and store credentials. Returns the masked result.

    Values that are empty, or that are the mask the UI rendered, leave the stored
    value alone. Explicitly clearing a field is done by sending ``null``.
    """
    gateway = await get_gateway(session, code)
    if gateway is None:
        raise KeyError(f"unknown gateway {code!r}")

    known = {field["key"] for field in credential_schema(code)}
    secrets = secret_fields(code)

    row = await _row(session, gateway.id, environment)
    current: Dict[str, Any] = decrypt_dict(row.encrypted_credentials) if row else {}

    for key, value in values.items():
        if key not in known:
            continue                     # ignore fields this adapter does not declare
        if value is None:
            current.pop(key, None)
            continue
        text = str(value).strip()
        if not text:
            continue                     # blank means "leave as is"
        if key in secrets and _is_mask(text) and key in current:
            continue                     # the UI echoed the mask back
        current[key] = text

    blob = encrypt_dict(current)
    if row is None:
        row = GatewayCredential(gateway_id=gateway.id, environment=environment,
                                encrypted_credentials=blob,
                                field_names=sorted(current), updated_by=updated_by)
        session.add(row)
    else:
        row.encrypted_credentials = blob
        row.field_names = sorted(current)
        row.updated_by = updated_by
    await session.commit()

    return {key: (mask(value) if key in secrets else value)
            for key, value in current.items()}


async def delete_credentials(session: AsyncSession, code: str, environment: str) -> bool:
    gateway = await get_gateway(session, code)
    if gateway is None:
        return False
    row = await _row(session, gateway.id, environment)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def configuration_status(session: AsyncSession, code: str,
                               environment: str = "sandbox") -> Dict[str, Any]:
    """Whether a gateway can run, and what is missing if it cannot."""
    credentials = await load_credentials(session, code, environment)
    adapter = adapter_class(code)(credentials, environment=environment)
    missing = adapter.missing_credentials()
    return {
        "configured": not missing,
        "missing_fields": missing,
        "configured_fields": sorted(credentials),
        "status": "Configured" if not missing else "Not Configured",
    }


async def all_configuration_status(session: AsyncSession,
                                   environment: str = "sandbox") -> Dict[str, Dict[str, Any]]:
    result = await session.execute(select(Gateway))
    statuses: Dict[str, Dict[str, Any]] = {}
    for gateway in result.scalars():
        try:
            statuses[gateway.code] = await configuration_status(session, gateway.code,
                                                                environment)
        except KeyError:
            # A gateway row with no adapter (removed in a later version) is reported
            # rather than crashing the Settings page.
            statuses[gateway.code] = {"configured": False,
                                      "missing_fields": [],
                                      "configured_fields": [],
                                      "status": "No adapter installed"}
    return statuses


def credential_form(code: str) -> List[Dict[str, Any]]:
    return credential_schema(code)
