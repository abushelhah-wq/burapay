"""Application settings (specification sections 16, 18, 26)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.benchmarks.scoring import COMPONENT_LABELS, DEFAULT_WEIGHTS, normalize_weights
from app.core.config import settings as app_settings
from app.db.session import get_session
from app.models import AppSetting, User
from app.services import bootstrap as settings_service

router = APIRouter(prefix="/settings", tags=["settings"])

#: Settings an admin may change through the API. Anything not listed here is
#: environment-only — encryption keys and database URLs are deliberately not editable
#: from a web form.
EDITABLE = {"scoring_weights", "benchmark_limits", "test_defaults", "test_card"}


@router.get("")
async def get_settings(_: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    rows = {row.key: row for row in (await session.execute(select(AppSetting))).scalars()}
    values = {key: (rows[key].value if key in rows
                    else settings_service.DEFAULT_SETTINGS.get(key, {}).get("value", {}))
              for key in settings_service.DEFAULT_SETTINGS}

    # The test card is a sandbox card, but there is no reason to hand its number to
    # every viewer; only the last four are returned.
    card = dict(values.get("test_card") or {})
    if card.get("number"):
        card["number"] = f"****{str(card['number'])[-4:]}"
    card.pop("cvc", None)
    values["test_card"] = card

    return {
        "settings": values,
        "descriptions": {key: payload["description"]
                         for key, payload in settings_service.DEFAULT_SETTINGS.items()},
        "scoring_components": COMPONENT_LABELS,
        "default_scoring_weights": DEFAULT_WEIGHTS,
        "environment": {
            "app_env": app_settings.app_env,
            "app_version": app_settings.app_version,
            "public_base_url": app_settings.public_base_url,
            # Section 26: the UI shows a large TEST MODE banner off the back of this.
            "test_mode": not app_settings.allow_production_gateways,
            "allow_production_gateways": app_settings.allow_production_gateways,
            "allow_direct_card_entry": app_settings.allow_direct_card_entry,
            "insecure_defaults": app_settings.insecure_defaults(),
        },
    }


@router.put("/{key}")
async def update_setting(key: str, value: Dict[str, Any] = Body(...),
                         admin: User = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    if key not in EDITABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{key!r} is not editable through the API. Editable settings: "
                   f"{', '.join(sorted(EDITABLE))}.")

    if key == "scoring_weights":
        value = normalize_weights({k: float(v) for k, v in value.items()})
    if key == "benchmark_limits":
        # The floor exists to protect the providers; an admin may raise it, not remove it.
        interval = float(value.get("min_interval_seconds", 2.0))
        value = {
            "min_interval_seconds": max(0.25, interval),
            "max_transactions_per_run": max(1, min(int(value.get("max_transactions_per_run", 500)),
                                                   5000)),
            "max_concurrency": max(1, min(int(value.get("max_concurrency", 1)), 5)),
        }
    if key == "test_card":
        current = await settings_service.get_setting(session, "test_card")
        merged = dict(current)
        for field, item in value.items():
            text = str(item or "").strip()
            if text and not text.startswith("*"):
                merged[field] = text
        value = merged

    stored = await settings_service.set_setting(session, key, value, updated_by=admin.email)
    if key == "test_card":
        stored = {**stored, "number": f"****{str(stored.get('number', ''))[-4:]}"}
        stored.pop("cvc", None)
    return {"key": key, "value": stored}
