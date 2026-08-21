"""
Adapter registry.

Adding a gateway is: write ``app/gateways/<name>/adapter.py``, then add one
entry to :data:`ADAPTERS`. That is the entire contract from §1 -- no shared
module, route, model or migration changes to support a new gateway.

The registry maps ``gateways.code`` (the database identity) to an adapter
class. A code present in the database but absent here is reported as
unsupported rather than crashing a request, so removing an adapter cannot take
the transaction history for that gateway offline.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Type

from app.gateways.base import GatewayAdapter
from app.gateways.geidea.adapter import GeideaAdapter

#: code -> adapter class. The one line a new gateway adds.
ADAPTERS: Dict[str, Type[GatewayAdapter]] = {
    GeideaAdapter.code: GeideaAdapter,
}


class UnknownGateway(KeyError):
    """Raised for a gateway code with no adapter compiled into this build."""


def get_adapter_class(code: str) -> Type[GatewayAdapter]:
    try:
        return ADAPTERS[code]
    except KeyError as exc:
        raise UnknownGateway(
            f"No adapter is registered for gateway code {code!r}. "
            f"Known: {', '.join(sorted(ADAPTERS)) or '(none)'}."
        ) from exc


def has_adapter(code: str) -> bool:
    return code in ADAPTERS


def all_adapter_classes() -> Iterable[Type[GatewayAdapter]]:
    return ADAPTERS.values()


def adapter_class_or_none(code: str) -> Optional[Type[GatewayAdapter]]:
    return ADAPTERS.get(code)
