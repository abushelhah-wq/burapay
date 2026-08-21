"""
Payment request models.

Every payment-creating request carries a client-generated ``idempotency_key``
(§0.6). Amounts are integer minor units (§0.4) -- there is no decimal amount
field anywhere in this module, so a float can never enter the system through
the API.

:class:`CardIn` is request-only. It is never used as a response model, never
embedded in one, and never logged: the shared HTTP client redacts the PAN and
destroys the CVV before anything reaches ``api_call_logs`` (§0.8).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.money import CURRENCY_EXPONENTS


class IdempotentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        description=(
            "Client-generated key. Re-sending a request with the same key "
            "returns the original transaction instead of creating a second one."
        ),
    )


class AmountMixin(BaseModel):
    amount_minor: int = Field(
        gt=0,
        description="Amount in the currency's minor unit (halalas, fils, cents).",
    )
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _known_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if code not in CURRENCY_EXPONENTS:
            raise ValueError(
                f"Unsupported currency {code}. Add its ISO 4217 minor-unit "
                f"exponent to app.money.CURRENCY_EXPONENTS first."
            )
        return code


class CardIn(BaseModel):
    """
    A raw test card. Request-only -- never serialised into any response.

    Accepted only when ``ALLOW_DIRECT_CARD_ENTRY`` is enabled server-side;
    the gate is enforced in the route, not in the UI (§7b).
    """

    model_config = ConfigDict(extra="forbid")

    number: str = Field(min_length=12, max_length=25)
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2000, le=2100)
    cvv: str = Field(min_length=3, max_length=4)
    holder_name: Optional[str] = Field(default=None, max_length=128)

    @field_validator("number")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        digits = "".join(char for char in value if char.isdigit())
        if not 12 <= len(digits) <= 19:
            raise ValueError("A card number must be 12-19 digits.")
        return digits

    @field_validator("cvv")
    @classmethod
    def _cvv_digits(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("A CVV must be numeric.")
        return value

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Pydantic prints the model in validation errors; without this a
        # rejected request could put a PAN into an error response and an
        # application log line.
        return f"CardIn(last4={self.number[-4:]!r})"

    __str__ = __repr__


class HPPSessionRequest(IdempotentRequest, AmountMixin):
    customer_email: Optional[str] = Field(default=None, max_length=320)


class DirectPaymentRequest(IdempotentRequest, AmountMixin):
    card: CardIn
    customer_email: Optional[str] = Field(default=None, max_length=320)


class TokenizeRequest(IdempotentRequest):
    card: CardIn


class TokenChargeRequest(IdempotentRequest, AmountMixin):
    token_reference: str = Field(min_length=1, max_length=255)


class CaptureRequest(IdempotentRequest):
    #: Omit for a full capture of the remaining authorised amount. Supplying a
    #: smaller value performs a partial capture; repeat for multiple partials
    #: where the gateway supports it (§4d).
    amount_minor: Optional[int] = Field(default=None, gt=0)


class RefundRequest(IdempotentRequest):
    #: Omit for a full refund of the remaining refundable balance (§4e).
    amount_minor: Optional[int] = Field(default=None, gt=0)


class VoidRequest(IdempotentRequest):
    pass


class BenchmarkRunRequest(IdempotentRequest, AmountMixin):
    operation: str
    integration_type: str
    count: int = Field(ge=1, description="Number of transactions to run.")
    card: Optional[CardIn] = None
