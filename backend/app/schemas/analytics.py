"""Benchmark dashboard responses (§5)."""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.schemas.common import ApiModel


class BenchmarkCell(ApiModel):
    """
    One gateway x integration type x operation cell.

    Every field is computed from stored transactions at request time. The
    sample sizes are part of the response on purpose: a p95 over four runs is
    not a p95, and the UI is expected to say so.
    """

    gateway_code: str
    gateway_name: str
    integration_type_code: Optional[str] = None
    operation: str

    sample_size: int
    completed: int
    succeeded: int
    failed: int
    errored: int
    timed_out: int
    pending: int
    success_rate: Optional[float] = None

    avg_duration_ms: Optional[float] = None
    median_duration_ms: Optional[int] = None
    #: Nearest-rank, never interpolated: a latency that actually occurred.
    p95_duration_ms: Optional[int] = None
    min_duration_ms: Optional[int] = None
    max_duration_ms: Optional[int] = None
    latency_sample_size: int = 0

    #: The headline comparison: how many round trips this gateway needs to
    #: complete this operation (§3).
    avg_request_count: Optional[float] = None
    median_request_count: Optional[int] = None
    max_request_count: Optional[int] = None


class BenchmarkSummary(ApiModel):
    cells: list[BenchmarkCell] = Field(default_factory=list)
    #: Where these measurements were taken. Latency without a location is not
    #: comparable to anything.
    measurement_location: Optional[str] = None
    generated_at: str
