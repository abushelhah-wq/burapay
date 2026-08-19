"""
Gateway health checks (specification section 36).

Read-only probes only. Nothing here creates a payment, and a gateway with no safe
read-only endpoint is reported as "no safe probe available" rather than being charged
a card to see whether it answers.

An HTTP 4xx from a probe still counts as available: a 404 for a deliberately
non-existent payment id proves the API is up, authenticating and routing, which is
exactly what is being asked. Only a 5xx or a transport failure is a real outage.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotConfigured, NotSupported
from app.core.logging import get_logger
from app.gateways.registry import build_adapter
from app.models import Gateway, GatewayHealthCheck
from app.services.credentials import load_credentials

logger = get_logger(__name__)


async def probe_gateway(session: AsyncSession, code: str,
                        environment: str = "sandbox") -> Dict[str, Any]:
    credentials = await load_credentials(session, code, environment)
    adapter = build_adapter(code, credentials, environment)

    if not adapter.is_configured():
        return {"gateway_code": code, "available": None, "response_time_ms": None,
                "http_status": None, "checked_at": datetime.now(timezone.utc).isoformat(),
                "detail": "Not configured — no credentials to probe with.",
                "status": "Not Configured"}

    client = adapter.build_client(min(settings.http_timeout_seconds, 15.0))
    try:
        probe = await adapter.health_probe(client)
        detail, available = probe.detail, probe.available
        response_ms, http_status = probe.response_time_ms, probe.http_status
    except (NotSupported, NotConfigured) as exc:
        detail, available, response_ms, http_status = str(exc), None, None, None
    except Exception as exc:                              # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:500]
        available, response_ms, http_status = False, None, None
    finally:
        await client.client.aclose()

    if available is not None:
        session.add(GatewayHealthCheck(gateway_code=code, available=available,
                                       response_time_ms=response_ms,
                                       http_status=http_status, detail=detail[:1000]))
        await session.commit()

    return {"gateway_code": code, "available": available, "response_time_ms": response_ms,
            "http_status": http_status, "detail": detail,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": ("Available" if available else
                       "Unavailable" if available is False else "Unknown")}


async def probe_all(session: AsyncSession, environment: str = "sandbox") -> List[Dict[str, Any]]:
    """Probe every enabled gateway.

    Sequential: these are reachability checks, and firing six concurrent probes from
    one host makes each of them contend with the others for the same socket buffer and
    CPU, which is precisely the artefact this platform exists to avoid introducing.
    """
    gateways = list((await session.execute(
        select(Gateway).where(Gateway.enabled.is_(True)).order_by(Gateway.display_order)
    )).scalars())
    results = []
    for gateway in gateways:
        results.append(await probe_gateway(session, gateway.code, environment))
    return results


async def recent_health(session: AsyncSession, *, hours: int = 24) -> List[Dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list((await session.execute(
        select(GatewayHealthCheck).where(GatewayHealthCheck.checked_at >= since)
        .order_by(GatewayHealthCheck.checked_at.desc()))).scalars())
    latest: Dict[str, GatewayHealthCheck] = {}
    history: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        latest.setdefault(row.gateway_code, row)
        history.setdefault(row.gateway_code, []).append(
            {"checked_at": row.checked_at.isoformat(), "available": row.available,
             "response_time_ms": row.response_time_ms})
    return [
        {"gateway_code": code, "available": row.available,
         "response_time_ms": row.response_time_ms, "http_status": row.http_status,
         "checked_at": row.checked_at.isoformat(), "detail": row.detail,
         "history": list(reversed(history.get(code, [])[:60]))}
        for code, row in sorted(latest.items())]


async def periodic_health_loop(interval_seconds: int = 300) -> None:
    """Background probe loop, started at application boot.

    Deliberately slow: these are reachability checks, not a monitoring product, and
    a gateway sandbox does not need to hear from us every ten seconds.
    """
    from app.db.session import get_sessionmaker

    while True:
        try:
            async with get_sessionmaker()() as session:
                await probe_all(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001
            logger.warning("health probe loop error",
                           extra={"operation": "health_probe", "status": "error",
                                  "error": str(exc)[:300]})
        await asyncio.sleep(interval_seconds)
