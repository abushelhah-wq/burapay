"""Shared response primitives (§1: Pydantic v2 at every boundary)."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.money import format_minor
from app.timeutil import iso

T = TypeVar("T")


class ApiModel(BaseModel):
    """
    Base for every response model.

    Serialises datetimes as ISO-8601 UTC with millisecond precision (§0.5) in
    one place, so no endpoint can emit a differently-shaped timestamp.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialise(self, value: object) -> object:
        if isinstance(value, datetime):
            return iso(value)
        return value


class Money(ApiModel):
    """
    An amount, carried as minor units with a display string alongside.

    ``amount_minor`` is the value; ``display`` is a convenience for the UI so
    the frontend never has to know a currency's exponent (§0.4).
    """

    amount_minor: int
    currency: str
    display: str

    @classmethod
    def of(cls, amount_minor: Optional[int], currency: Optional[str]) -> Optional["Money"]:
        if amount_minor is None or not currency:
            return None
        return cls(
            amount_minor=amount_minor,
            currency=currency,
            display=format_minor(amount_minor, currency),
        )


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


class ErrorDetail(ApiModel):
    """
    A failure, in the shape the frontend renders.

    ``code`` is stable and machine-readable; ``message`` is for a human;
    ``doc_url`` is populated when the failure is "this needs documentation we
    do not have", so the UI can link straight to the page required.
    """

    code: str
    message: str
    doc_url: Optional[str] = None
    detail: Optional[dict] = None
