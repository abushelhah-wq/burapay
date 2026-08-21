"""
Server-side feature gates (§7b).

Every flag here is enforced in the backend, at the layer that would otherwise
act. None of them is a frontend hint: hiding a button is a courtesy to the
user, and these functions are what actually stop the request when someone calls
the API directly.

* ``ALLOW_PRODUCTION_GATEWAYS`` -- enforced in
  :meth:`app.gateways.base.GatewayAdapter.guard_production`, at the adapter
  layer, before any outbound call.
* ``ALLOW_DIRECT_CARD_ENTRY`` -- enforced by :func:`require_direct_card_entry`
  on every endpoint that accepts a raw PAN.
* ``BENCHMARK_MIN_INTERVAL_SECONDS`` / ``BENCHMARK_MAX_TRANSACTIONS_PER_RUN``
  -- enforced by :mod:`app.services.benchmark`.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.config import get_settings
from app.gateways.errors import FeatureDisabled


def direct_card_entry_enabled() -> bool:
    return get_settings().ALLOW_DIRECT_CARD_ENTRY


def require_direct_card_entry() -> None:
    """
    Refuse a raw-card request when ALLOW_DIRECT_CARD_ENTRY is off.

    Raises 403 with a machine-readable code so the frontend can explain *why*
    the control is unavailable rather than showing a bare "forbidden".
    """
    if not direct_card_entry_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "feature_disabled",
                "feature": "direct_card_entry",
                "env_var": "ALLOW_DIRECT_CARD_ENTRY",
                "message": (
                    "Raw card entry is disabled on this deployment. Set "
                    "ALLOW_DIRECT_CARD_ENTRY=1 to enable the Direct API, "
                    "tokenization and CIT flows."
                ),
            },
        )


def production_gateways_allowed() -> bool:
    return get_settings().ALLOW_PRODUCTION_GATEWAYS


def assert_direct_card_entry_allowed() -> None:
    """Non-HTTP variant, for use inside the service layer and workers."""
    if not direct_card_entry_enabled():
        raise FeatureDisabled("direct card entry", "ALLOW_DIRECT_CARD_ENTRY")
