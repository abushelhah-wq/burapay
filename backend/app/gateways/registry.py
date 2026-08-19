"""
The adapter registry.

Adding a gateway means writing one adapter module and appending its class here.
Nothing else in the application knows the difference between one gateway and another
(specification sections 6 and 61).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Type

from app.core.config import settings
from app.gateways.adyen import AdyenAdapter
from app.gateways.base import CredentialField, PaymentGatewayAdapter
from app.gateways.checkout import CheckoutAdapter
from app.gateways.geidea import GeideaAdapter
from app.gateways.hyperpay import HyperPayAdapter
from app.gateways.mock import MockPayAdapter
from app.gateways.moyasar import MoyasarAdapter
from app.gateways.stripe import StripeAdapter

#: Display order: Geidea first, since it is the baseline the project exists to
#: measure; MockPay last, since it is a simulator rather than a gateway.
ADAPTER_CLASSES: Sequence[Type[PaymentGatewayAdapter]] = (
    GeideaAdapter,
    StripeAdapter,
    AdyenAdapter,
    CheckoutAdapter,
    HyperPayAdapter,
    MoyasarAdapter,
    MockPayAdapter,
)

ADAPTERS: Dict[str, Type[PaymentGatewayAdapter]] = {cls.code: cls for cls in ADAPTER_CLASSES}

#: Adapters that do not represent a real gateway and must be excluded from rankings.
SIMULATED_CODES = frozenset({MockPayAdapter.code})


def adapter_class(code: str) -> Type[PaymentGatewayAdapter]:
    try:
        return ADAPTERS[code]
    except KeyError as exc:
        raise KeyError(f"unknown gateway {code!r}; known: {', '.join(sorted(ADAPTERS))}") from exc


def build_adapter(code: str, credentials: Mapping[str, Any],
                  environment: str = "sandbox") -> PaymentGatewayAdapter:
    """Instantiate an adapter with the credentials decrypted for this request."""
    return adapter_class(code)(credentials, environment=environment,
                               public_base_url=settings.public_base_url)


def credential_schema(code: str) -> List[Dict[str, Any]]:
    """The Settings-page form definition for one gateway."""
    return [
        {
            "key": field.key,
            "label": field.label,
            "required": field.required,
            "secret": field.secret,
            "placeholder": field.placeholder,
            "help_text": field.help_text,
            "default": field.default,
            "choices": list(field.choices),
        }
        for field in adapter_class(code).credential_fields
    ]


def descriptor(cls: Type[PaymentGatewayAdapter], order: int) -> Dict[str, Any]:
    """The static facts about a gateway, used to seed and refresh the database."""
    return {
        "code": cls.code,
        "name": cls.display_name,
        "supports_hpp": cls.supports_hpp,
        "supports_direct": cls.supports_direct,
        "supported_currencies": list(cls.supported_currencies),
        "logo_slug": cls.code,
        "display_order": order,
        "docs_url": cls.docs_url,
        "notes": list(cls.notes),
    }


def all_descriptors() -> List[Dict[str, Any]]:
    return [descriptor(cls, index * 10) for index, cls in enumerate(ADAPTER_CLASSES)]


def documented_calls(code: str) -> Dict[str, str]:
    return dict(adapter_class(code).documented_calls)


def secret_fields(code: str) -> set[str]:
    return adapter_class(code).secret_field_names()


__all__ = ["ADAPTERS", "ADAPTER_CLASSES", "SIMULATED_CODES", "CredentialField",
           "adapter_class", "all_descriptors", "build_adapter", "credential_schema",
           "descriptor", "documented_calls", "secret_fields"]
