"""Adapter registry — Geidea first, since it is the baseline everything compares to."""

from __future__ import annotations

from typing import Dict, List, Type

from app.adapters.adyen_adapter import AdyenAdapter
from app.adapters.base import (Adapter, Card, Charge, FLOW_LABELS, FLOWS, Outcome,
                               Started)
from app.adapters.checkout_adapter import CheckoutAdapter
from app.adapters.geidea_adapter import GeideaAdapter
from app.adapters.hyperpay_adapter import HyperPayAdapter
from app.adapters.moyasar_adapter import MoyasarAdapter
from app.adapters.stripe_adapter import StripeAdapter

ADAPTER_CLASSES: List[Type[Adapter]] = [
    GeideaAdapter, AdyenAdapter, StripeAdapter, CheckoutAdapter,
    HyperPayAdapter, MoyasarAdapter,
]

__all__ = ["Adapter", "Card", "Charge", "Outcome", "Started", "FLOWS", "FLOW_LABELS",
           "ADAPTER_CLASSES", "build_adapters", "get_adapter"]


def build_adapters(timeout: float = 30.0) -> Dict[str, Adapter]:
    """Fresh adapter instances. Built per request so a changed .env needs no code reload."""
    return {cls.name: cls(timeout=timeout) for cls in ADAPTER_CLASSES}


def get_adapter(name: str, timeout: float = 30.0) -> Adapter:
    for cls in ADAPTER_CLASSES:
        if cls.name == name:
            return cls(timeout=timeout)
    known = ", ".join(cls.name for cls in ADAPTER_CLASSES)
    raise KeyError(f"unknown gateway {name!r}; known: {known}")
