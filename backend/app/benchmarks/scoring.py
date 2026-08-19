"""
The Internal Benchmark Score (specification section 18).

This is **not** an industry standard and is labelled as such everywhere it is shown.
It is a weighted roll-up of four measured components, provided because "which gateway
should we use" is easier to discuss with one number alongside the distributions than
with distributions alone.

Scoring is relative: each gateway's component score is its performance against the
best observed value in the same comparison, so the numbers only mean something within
a single comparison set. Latency components use ``best / value`` (lower is better),
capped at 100; rate components are the percentage itself.

A gateway with too few samples is not scored at all. Ranking eleven transactions
against a hundred would produce a number that looks authoritative and is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from app.core.stats import MIN_RANKING_SAMPLES

DEFAULT_WEIGHTS: Dict[str, float] = {
    "api_response_time": 0.35,
    "p95_latency": 0.25,
    "transaction_completion": 0.20,
    "success_rate": 0.20,
}

#: Shown wherever a score appears. Section 18: never presented as a standard.
SCORE_LABEL = "Internal Benchmark Score"

COMPONENT_LABELS = {
    "api_response_time": "API response time",
    "p95_latency": "P95 latency",
    "transaction_completion": "Transaction completion time",
    "success_rate": "Success rate",
}


@dataclass
class ScoreInput:
    """One gateway's measured inputs to the score."""

    gateway_code: str
    gateway_name: str
    integration_type: str
    sample_size: int
    mean_api_ms: Optional[float]
    p95_api_ms: Optional[float]
    mean_total_ms: Optional[float]
    success_rate: Optional[float]


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """Scale supplied weights to sum to 1, ignoring unknown keys."""
    cleaned = {key: max(0.0, float(value)) for key, value in weights.items()
               if key in DEFAULT_WEIGHTS}
    for key in DEFAULT_WEIGHTS:
        cleaned.setdefault(key, 0.0)
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {key: round(value / total, 6) for key, value in cleaned.items()}


def _lower_is_better(value: Optional[float], best: Optional[float]) -> Optional[float]:
    if value is None or best is None or value <= 0 or best <= 0:
        return None
    return round(min(100.0, best / value * 100.0), 2)


def score_all(inputs: Sequence[ScoreInput], weights: Optional[Dict[str, float]] = None,
              *, min_samples: int = MIN_RANKING_SAMPLES) -> Dict[str, Any]:
    """Score and rank a comparison set.

    Returns the scored entries, the entries excluded for insufficient samples, and the
    weights actually used — the UI shows all three, so a ranking can never be read
    without knowing what it excluded.
    """
    applied = normalize_weights(weights or DEFAULT_WEIGHTS)
    eligible = [i for i in inputs if i.sample_size >= min_samples]
    excluded = [
        {"gateway_code": i.gateway_code, "gateway_name": i.gateway_name,
         "integration_type": i.integration_type, "sample_size": i.sample_size,
         "reason": f"needs at least {min_samples} comparable transactions to be ranked"}
        for i in inputs if i.sample_size < min_samples
    ]

    if not eligible:
        return {"weights": applied, "entries": [], "excluded": excluded,
                "min_samples": min_samples, "label": SCORE_LABEL,
                "note": ("Not enough comparable transactions to produce a ranking. "
                         f"A gateway needs at least {min_samples} completed "
                         "transactions before it is scored.")}

    best_mean = min((i.mean_api_ms for i in eligible if i.mean_api_ms), default=None)
    best_p95 = min((i.p95_api_ms for i in eligible if i.p95_api_ms), default=None)
    best_total = min((i.mean_total_ms for i in eligible if i.mean_total_ms), default=None)

    entries: List[Dict[str, Any]] = []
    for item in eligible:
        components = {
            "api_response_time": _lower_is_better(item.mean_api_ms, best_mean),
            "p95_latency": _lower_is_better(item.p95_api_ms, best_p95),
            "transaction_completion": _lower_is_better(item.mean_total_ms, best_total),
            "success_rate": (round(item.success_rate, 2)
                             if item.success_rate is not None else None),
        }
        # Components that could not be measured are dropped and their weight is
        # redistributed, rather than scored as zero — an unmeasurable metric is not a
        # bad one (section 9).
        usable = {key: value for key, value in components.items() if value is not None}
        weight_total = sum(applied[key] for key in usable) or 1.0
        score = sum(value * applied[key] for key, value in usable.items()) / weight_total

        entries.append({
            "gateway_code": item.gateway_code,
            "gateway_name": item.gateway_name,
            "integration_type": item.integration_type,
            "sample_size": item.sample_size,
            "score": round(score, 2),
            "components": components,
            "measured_components": sorted(usable),
            "unmeasurable_components": sorted(set(components) - set(usable)),
        })

    entries.sort(key=lambda entry: entry["score"], reverse=True)
    for position, entry in enumerate(entries, start=1):
        entry["rank"] = position

    return {"weights": applied, "entries": entries, "excluded": excluded,
            "min_samples": min_samples,
            "label": SCORE_LABEL,
            "note": ("Internal Benchmark Score — a weighted roll-up defined by this "
                     "platform, not an industry standard. Scores are relative to the "
                     "best value observed within this comparison set.")}
