"""Shared response shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int
    limit: int
    offset: int


class Message(BaseModel):
    message: str
    detail: Optional[str] = None


class StatSummary(BaseModel):
    """The statistical set every group carries (specification section 40)."""

    count: int = 0
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    median: Optional[float] = None
    p50: Optional[float] = None
    p90: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    stdev: Optional[float] = None
    #: False when the sample is too small for percentiles to mean much. Shown in the
    #: UI rather than hidden, so a p99 over six samples is never read as precision.
    reliable: bool = False


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str
    environment: str
    test_mode: bool
    checked_at: datetime


class ErrorResponse(BaseModel):
    """The normalized error shape from specification section 37."""

    gateway: Optional[str] = None
    operation: Optional[str] = None
    category: str
    http_status: Optional[int] = None
    gateway_code: Optional[str] = None
    message: str
    duration_ms: Optional[float] = None
    detail: Optional[Dict[str, Any]] = Field(default=None)
