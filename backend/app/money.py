"""
Money handling (§0.4).

Every amount in this system is an ``int`` in the currency's minor unit --
fils, halalas, cents. Floats are never used for money anywhere: not in the
database, not in a Pydantic model, not in a gateway request body. Conversion to
a decimal string happens once, at the presentation edge, and only via
:func:`format_minor`.

The exponent table is ISO 4217. A currency that is not listed raises rather
than defaulting to 2, because silently assuming an exponent is how a
three-decimal currency (KWD, BHD, OMR, JOD, TND) gets charged 1000x wrong.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict

#: ISO 4217 minor-unit exponents for currencies this platform can transact in.
#: Extend deliberately -- see the module docstring for why there is no default.
CURRENCY_EXPONENTS: Dict[str, int] = {
    # zero-decimal
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "XOF": 0, "XAF": 0,
    # two-decimal
    "SAR": 2, "AED": 2, "EGP": 2, "QAR": 2, "USD": 2, "EUR": 2, "GBP": 2,
    "MAD": 2, "LBP": 2, "TRY": 2, "PKR": 2, "INR": 2, "SEK": 2, "NOK": 2,
    # three-decimal -- the reason this table exists
    "KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "TND": 3, "LYD": 3, "IQD": 3,
}


class UnknownCurrency(ValueError):
    """Raised for a currency whose minor-unit exponent is not known."""


class InvalidAmount(ValueError):
    """Raised when a decimal amount cannot be represented in minor units."""


def normalise_currency(currency: str) -> str:
    code = (currency or "").strip().upper()
    if code not in CURRENCY_EXPONENTS:
        raise UnknownCurrency(
            f"No ISO 4217 minor-unit exponent known for currency {code!r}. "
            f"Add it to CURRENCY_EXPONENTS deliberately -- defaulting to 2 "
            f"would charge a 3-decimal currency 1000x wrong."
        )
    return code


def exponent(currency: str) -> int:
    return CURRENCY_EXPONENTS[normalise_currency(currency)]


def to_minor(amount: str | Decimal | int, currency: str) -> int:
    """
    Convert a decimal amount (``"10.00"``) to minor units (``1000`` for SAR).

    Accepts ``str``, ``Decimal`` or ``int``. It deliberately does NOT accept
    ``float`` -- binary floating point cannot represent most decimal amounts
    exactly, and this is the boundary where that error would enter the system.
    """
    if isinstance(amount, float):
        raise InvalidAmount(
            "Refusing to convert a float to minor units. Pass a string or "
            "Decimal so the value is exact (§0.4)."
        )
    exp = exponent(currency)
    try:
        value = Decimal(amount) if not isinstance(amount, Decimal) else amount
    except (InvalidOperation, TypeError) as exc:
        raise InvalidAmount(f"{amount!r} is not a valid decimal amount") from exc

    scaled = value.scaleb(exp)
    rounded = scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if rounded != scaled:
        raise InvalidAmount(
            f"{value} has more precision than {normalise_currency(currency)} "
            f"supports ({exp} decimal places)."
        )
    return int(rounded)


def format_minor(amount_minor: int, currency: str) -> str:
    """
    Render minor units as a decimal string for display only.

    This is the presentation layer's function. Nothing that builds a gateway
    request body may call it -- gateways that want a decimal string get one
    built by their own adapter, which documents the format its docs require.
    """
    exp = exponent(currency)
    return str(Decimal(amount_minor).scaleb(-exp).quantize(Decimal(1).scaleb(-exp)))
