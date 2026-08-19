"""
Statistics for benchmark groups (specification sections 13, 40, 60).

Two decisions worth stating, because they change what the numbers mean:

**Percentiles are nearest-rank, not interpolated.** Every figure the platform
reports is a latency that actually occurred, not an average of two that did. With
n=20 an interpolated p95 is a number no request ever took.

**Small samples are labelled, not hidden.** A p99 over 8 samples is just the maximum
wearing a different name. :func:`summarize` still reports it, but flags the group as
statistically thin so the UI can say so instead of implying precision that is not
there.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, Iterable, List, Optional, Sequence

#: Below this, percentile figures are reported but marked as unreliable.
MIN_RELIABLE_SAMPLES = 20
#: Below this, no ranking may be generated at all (section 60).
MIN_RANKING_SAMPLES = 10


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile: the smallest observed value at or above ``pct``."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered))
    rank = max(1, min(len(ordered), rank))
    return round(ordered[rank - 1], 3)


def summarize(values: Iterable[float]) -> Dict[str, Optional[float]]:
    """The full descriptive set for one group of durations."""
    data: List[float] = [float(v) for v in values if v is not None]
    if not data:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None,
                "p50": None, "p90": None, "p95": None, "p99": None, "stdev": None,
                "reliable": False}
    return {
        "count": len(data),
        "min": round(min(data), 3),
        "max": round(max(data), 3),
        "mean": round(statistics.fmean(data), 3),
        "median": round(statistics.median(data), 3),
        "p50": percentile(data, 50),
        "p90": percentile(data, 90),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
        # Sample standard deviation needs two points; one point has zero spread,
        # which is true but uninformative, so it is reported as 0.0 rather than null.
        "stdev": round(statistics.stdev(data), 3) if len(data) > 1 else 0.0,
        "reliable": len(data) >= MIN_RELIABLE_SAMPLES,
    }


def rates(successes: int, total: int) -> Dict[str, Optional[float]]:
    """Success/failure percentages. Undefined rather than 0% when nothing ran.

    The count is returned as ``evaluated`` rather than ``total``: these keys are
    merged into result rows that already carry a ``total`` latency summary, and a
    collision there would silently replace a statistics object with an integer.
    """
    if total <= 0:
        return {"success_rate": None, "failure_rate": None, "evaluated": 0,
                "successes": 0}
    success_rate = round(successes / total * 100, 2)
    return {
        "success_rate": success_rate,
        "failure_rate": round(100 - success_rate, 2),
        "evaluated": total,
        "successes": successes,
    }
