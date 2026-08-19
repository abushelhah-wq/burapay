"""
Shared timing / measurement framework for the payment-gateway API benchmark.

What this measures
------------------
For every flow (Hosted Checkout, Direct API, Capture, Refund, Void, MIT) and for
every gateway, the harness records two things:

1. **Call count** - how many merchant-server-to-gateway HTTP calls the flow needs.
   Only calls marked ``timed=True`` count. Setup work that a real merchant would
   have done earlier (creating a payment so there is something to refund, minting
   a token so there is something to charge) is issued as a *prep* call and is
   excluded from both the count and the timing stats.

2. **Wall-clock latency** - per call and end-to-end, aggregated over N repetitions
   with the first few discarded as warmup, reported as min / mean / median / p95 / max.

Timing boundary: the clock starts immediately before ``requests`` is handed the
request and stops when the response body has been fully read. That includes TCP +
TLS setup on the first call of a connection, which is why warmup runs are
discarded and why connection reuse is left on (a real merchant server keeps
keep-alive connections open to its gateway).
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import requests

__all__ = [
    "CallRecord",
    "FlowRun",
    "FlowStats",
    "GatewayError",
    "SkipFlow",
    "MeasuredSession",
    "Gateway",
    "BenchmarkRunner",
    "summarize",
    "env",
    "env_int",
    "env_float",
    "money",
    "new_reference",
    "RESULTS_DIR",
]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

# Flow identifiers. Order matters - it is the display order everywhere downstream.
FLOWS: Sequence[str] = (
    "hosted_checkout",
    "direct_api",
    "capture",
    "refund",
    "void",
    "mit",
)

FLOW_LABELS: Dict[str, str] = {
    "hosted_checkout": "Hosted Checkout",
    "direct_api": "Direct API (S2S)",
    "capture": "Capture",
    "refund": "Refund",
    "void": "Void",
    "mit": "MIT / Recurring",
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class GatewayError(RuntimeError):
    """A gateway returned something the flow cannot continue from."""


class SkipFlow(Exception):
    """This flow is not runnable for this gateway/config - record it as skipped, not failed."""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class CallRecord:
    """One HTTP round trip."""

    step: int
    name: str
    method: str
    url: str
    timed: bool
    elapsed_ms: float
    status_code: Optional[int] = None
    ok: bool = False
    error: Optional[str] = None
    # Small excerpt of the response, kept for debugging a failed run. Never contains
    # request bodies, so card data submitted upstream is not written to disk here.
    snippet: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FlowRun:
    """One end-to-end execution of one flow."""

    gateway: str
    flow: str
    run_index: int
    warmup: bool
    calls: List[CallRecord] = field(default_factory=list)
    ok: bool = False
    error: Optional[str] = None
    skipped: bool = False

    @property
    def timed_calls(self) -> List[CallRecord]:
        return [c for c in self.calls if c.timed]

    @property
    def call_count(self) -> int:
        return len(self.timed_calls)

    @property
    def total_ms(self) -> float:
        return round(sum(c.elapsed_ms for c in self.timed_calls), 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gateway": self.gateway,
            "flow": self.flow,
            "run_index": self.run_index,
            "warmup": self.warmup,
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error,
            "call_count": self.call_count,
            "total_ms": self.total_ms,
            "calls": [c.to_dict() for c in self.calls],
        }


@dataclass
class FlowStats:
    """Aggregated statistics for one gateway+flow across all non-warmup runs."""

    gateway: str
    flow: str
    runs_attempted: int
    runs_ok: int
    call_count: Optional[int]
    end_to_end: Dict[str, Optional[float]]
    per_call: List[Dict[str, Any]]
    skipped: bool = False
    skip_reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile.

    Deliberately not interpolated: with the default 30 runs, interpolating a p95
    invents a number that sits between two real observations. Nearest-rank always
    returns a latency that was actually measured.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct / 100 * len(ordered) // 1))))
    return round(ordered[rank - 1], 3)


def summarize(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """min / mean / median / p95 / max / stdev over a list of millisecond timings."""
    if not values:
        return {"n": 0, "min": None, "mean": None, "median": None,
                "p95": None, "max": None, "stdev": None}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": percentile(values, 95),
        "max": round(max(values), 3),
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #

def env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def env_int(key: str, default: int) -> int:
    raw = env(key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    raw = env(key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def money(amount: float) -> str:
    """Two-decimal string form, used by gateways that sign or send decimal amounts."""
    return f"{amount:.2f}"


def new_reference(prefix: str = "bench") -> str:
    """Unique merchant reference. Gateways reject repeats, so every run needs a fresh one."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# Measured HTTP session
# --------------------------------------------------------------------------- #

class MeasuredSession:
    """A ``requests.Session`` wrapper that records the wall-clock time of every call.

    Gateway modules call :meth:`call` for steps that belong to the measured flow and
    :meth:`prep` for setup that a real merchant would already have done.
    """

    def __init__(self, gateway: str, flow: str, run: FlowRun,
                 timeout: float = 30.0, session: Optional[requests.Session] = None,
                 verbose: bool = False) -> None:
        self.gateway = gateway
        self.flow = flow
        self.run = run
        self.timeout = timeout
        self.session = session or requests.Session()
        self.verbose = verbose
        self._step = 0

    # -- public API ------------------------------------------------------- #

    def call(self, name: str, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Perform a *timed* call. Counts toward call count and latency stats."""
        return self._request(name, method, url, timed=True, **kwargs)

    def prep(self, name: str, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Perform an *untimed* setup call. Excluded from call count and stats."""
        return self._request(name, method, url, timed=False, **kwargs)

    def json_of(self, response: requests.Response) -> Dict[str, Any]:
        """Parse a JSON body, raising GatewayError with context instead of a bare ValueError."""
        try:
            body = response.json()
        except ValueError:
            raise GatewayError(
                f"{self.gateway}/{self.flow}: expected JSON from "
                f"{response.request.method} {response.url}, got "
                f"{response.status_code} {response.text[:200]!r}"
            )
        if not isinstance(body, dict):
            raise GatewayError(
                f"{self.gateway}/{self.flow}: expected a JSON object from {response.url}, "
                f"got {type(body).__name__}"
            )
        return body

    def expect_ok(self, response: requests.Response, what: str,
                  accept: Iterable[int] = (200, 201, 202)) -> Dict[str, Any]:
        """Assert an acceptable status code, then return the parsed JSON body."""
        if response.status_code not in tuple(accept):
            raise GatewayError(
                f"{self.gateway}/{self.flow}: {what} returned HTTP "
                f"{response.status_code}: {response.text[:300]}"
            )
        return self.json_of(response)

    def close(self) -> None:
        self.session.close()

    # -- internals -------------------------------------------------------- #

    def _request(self, name: str, method: str, url: str, timed: bool,
                 **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        self._step += 1
        step = self._step
        started = time.perf_counter()
        record = CallRecord(step=step, name=name, method=method.upper(), url=url,
                            timed=timed, elapsed_ms=0.0)
        try:
            response = self.session.request(method.upper(), url, **kwargs)
            # Touch .content so the timing covers reading the full body, not just headers.
            _ = response.content
            record.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record.status_code = response.status_code
            record.ok = response.ok
            if not response.ok:
                record.error = f"HTTP {response.status_code}"
                record.snippet = response.text[:300]
        except requests.RequestException as exc:
            record.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record.error = f"{type(exc).__name__}: {exc}"
            self.run.calls.append(record)
            if self.verbose:
                self._echo(record)
            raise GatewayError(
                f"{self.gateway}/{self.flow}: {method.upper()} {url} failed: {exc}"
            ) from exc

        self.run.calls.append(record)
        if self.verbose:
            self._echo(record)
        return response

    def _echo(self, record: CallRecord) -> None:
        marker = " " if record.timed else "~"
        status = record.status_code if record.status_code is not None else "ERR"
        print(f"      {marker} [{record.step}] {record.name:<28} "
              f"{record.method:<4} {status} {record.elapsed_ms:>9.1f} ms",
              file=sys.stderr)


# --------------------------------------------------------------------------- #
# Gateway base class
# --------------------------------------------------------------------------- #

class Gateway:
    """Base class for a gateway module.

    Subclasses set :attr:`name` / :attr:`label`, implement :meth:`configured`, and
    implement whichever ``flow_*`` methods the gateway supports. Each flow method
    receives a :class:`MeasuredSession` and drives the documented call sequence,
    calling ``s.call(...)`` for measured steps and ``s.prep(...)`` for setup.

    A flow that cannot run (unsupported, or missing an optional credential) should
    raise :class:`SkipFlow` with a reason rather than failing.
    """

    name: str = "gateway"
    label: str = "Gateway"
    #: Documented minimum call counts, from docs/02_api_flow_comparison.md. Used to
    #: flag where measured reality diverges from the vendor's own documentation.
    documented_calls: Dict[str, str] = {}
    #: Free-text caveats surfaced in the report for this gateway.
    notes: List[str] = []

    def __init__(self) -> None:
        self.amount = env_float("TEST_AMOUNT", 10.00)
        self.currency = env("TEST_CURRENCY", "USD")
        self.timeout = env_float("HTTP_TIMEOUT_SECONDS", 30.0)

    # -- configuration ---------------------------------------------------- #

    def configured(self) -> bool:
        """True when every credential this gateway needs is present in the environment."""
        return False

    def missing_config(self) -> List[str]:
        """Names of the environment variables that are missing. Used for the skip message."""
        return []

    # -- flows ------------------------------------------------------------ #

    def flow_hosted_checkout(self, s: MeasuredSession) -> None:
        raise SkipFlow("not implemented for this gateway")

    def flow_direct_api(self, s: MeasuredSession) -> None:
        raise SkipFlow("not implemented for this gateway")

    def flow_capture(self, s: MeasuredSession) -> None:
        raise SkipFlow("not implemented for this gateway")

    def flow_refund(self, s: MeasuredSession) -> None:
        raise SkipFlow("not implemented for this gateway")

    def flow_void(self, s: MeasuredSession) -> None:
        raise SkipFlow("not implemented for this gateway")

    def flow_mit(self, s: MeasuredSession) -> None:
        raise SkipFlow("not implemented for this gateway")

    # -- lifecycle -------------------------------------------------------- #

    def build_session(self) -> requests.Session:
        """A fresh ``requests.Session``, pre-loaded with auth headers if the gateway uses them."""
        return requests.Session()

    def flow_callable(self, flow: str) -> Callable[[MeasuredSession], None]:
        return getattr(self, f"flow_{flow}")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

class BenchmarkRunner:
    """Executes flows repeatedly and aggregates the results."""

    def __init__(self, runs: Optional[int] = None, warmups: Optional[int] = None,
                 flows: Optional[Sequence[str]] = None, verbose: bool = False,
                 delay_ms: Optional[int] = None) -> None:
        self.runs = runs if runs is not None else env_int("RUNS_PER_CALL", 30)
        self.warmups = warmups if warmups is not None else env_int("WARMUP_RUNS", 2)
        self.flows = tuple(flows) if flows else FLOWS
        self.verbose = verbose
        self.delay_ms = delay_ms if delay_ms is not None else env_int("DELAY_BETWEEN_RUNS_MS", 0)
        self.runs_log: List[FlowRun] = []

    def run_gateway(self, gateway: Gateway) -> List[FlowStats]:
        stats: List[FlowStats] = []
        for flow in self.flows:
            stats.append(self.run_flow(gateway, flow))
        return stats

    def run_flow(self, gateway: Gateway, flow: str) -> FlowStats:
        label = FLOW_LABELS.get(flow, flow)
        print(f"  {gateway.label} :: {label}", flush=True)

        session = gateway.build_session()
        completed: List[FlowRun] = []
        errors: List[str] = []
        skip_reason: Optional[str] = None
        total = self.warmups + self.runs

        try:
            for index in range(total):
                warmup = index < self.warmups
                run = FlowRun(gateway=gateway.name, flow=flow,
                              run_index=index - self.warmups, warmup=warmup)
                measured = MeasuredSession(gateway.name, flow, run,
                                           timeout=gateway.timeout, session=session,
                                           verbose=self.verbose)
                try:
                    gateway.flow_callable(flow)(measured)
                    run.ok = True
                except SkipFlow as exc:
                    run.skipped = True
                    skip_reason = str(exc)
                    self.runs_log.append(run)
                    print(f"    skipped: {skip_reason}", flush=True)
                    return FlowStats(gateway=gateway.name, flow=flow, runs_attempted=0,
                                     runs_ok=0, call_count=None,
                                     end_to_end=summarize([]), per_call=[],
                                     skipped=True, skip_reason=skip_reason)
                except GatewayError as exc:
                    run.error = str(exc)
                    errors.append(str(exc))
                except Exception as exc:  # noqa: BLE001 - one bad run must not kill the batch
                    run.error = f"{type(exc).__name__}: {exc}"
                    errors.append(run.error)

                self.runs_log.append(run)
                if not warmup:
                    completed.append(run)

                if run.error and len(errors) >= 3 and not any(r.ok for r in self.runs_log
                                                              if r.flow == flow):
                    # Three consecutive failures with nothing succeeding: the flow is
                    # broken (bad credential, wrong endpoint), not merely flaky. Stop
                    # rather than burn 30 attempts on it.
                    print(f"    aborting after {len(errors)} failures: {errors[-1][:160]}",
                          flush=True)
                    break

                if self.delay_ms:
                    time.sleep(self.delay_ms / 1000.0)
        finally:
            session.close()

        return self._aggregate(gateway, flow, completed, errors)

    # -- aggregation ------------------------------------------------------ #

    def _aggregate(self, gateway: Gateway, flow: str, runs: List[FlowRun],
                   errors: List[str]) -> FlowStats:
        ok_runs = [r for r in runs if r.ok]
        end_to_end = summarize([r.total_ms for r in ok_runs])

        # Group per-call timings by (step, name) so a flow whose length varies with the
        # 3DS outcome still aggregates cleanly - a conditional step simply has fewer
        # samples than the unconditional ones, and its `n` shows that.
        buckets: Dict[tuple, List[float]] = {}
        for run in ok_runs:
            for call in run.timed_calls:
                buckets.setdefault((call.step, call.name, call.method), []).append(call.elapsed_ms)

        per_call = []
        for (step, name, method), values in sorted(buckets.items()):
            entry = {"step": step, "name": name, "method": method}
            entry.update(summarize(values))
            per_call.append(entry)

        counts = sorted({r.call_count for r in ok_runs})
        call_count = counts[0] if len(counts) == 1 else (counts[-1] if counts else None)

        stats = FlowStats(
            gateway=gateway.name, flow=flow, runs_attempted=len(runs),
            runs_ok=len(ok_runs), call_count=call_count,
            end_to_end=end_to_end, per_call=per_call, errors=errors[:5],
        )
        if ok_runs:
            spread = "" if len(counts) == 1 else f" (varies {counts[0]}-{counts[-1]})"
            print(f"    {len(ok_runs)}/{len(runs)} ok | {call_count} calls{spread} | "
                  f"median {end_to_end['median']} ms | p95 {end_to_end['p95']} ms", flush=True)
        elif errors:
            print(f"    0/{len(runs)} ok | {errors[-1][:160]}", flush=True)
        return stats


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def ensure_results_dir(path: str = RESULTS_DIR) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def write_json(all_stats: Sequence[FlowStats], meta: Dict[str, Any],
               path: str) -> str:
    ensure_results_dir(os.path.dirname(path))
    payload = {"meta": meta, "results": [s.to_dict() for s in all_stats]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path


def write_summary_csv(all_stats: Sequence[FlowStats], path: str) -> str:
    ensure_results_dir(os.path.dirname(path))
    columns = ["gateway", "flow", "flow_label", "status", "runs_ok", "runs_attempted",
               "api_calls", "min_ms", "mean_ms", "median_ms", "p95_ms", "max_ms",
               "stdev_ms", "note"]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for stat in all_stats:
            e2e = stat.end_to_end
            writer.writerow({
                "gateway": stat.gateway,
                "flow": stat.flow,
                "flow_label": FLOW_LABELS.get(stat.flow, stat.flow),
                "status": "skipped" if stat.skipped else ("ok" if stat.runs_ok else "failed"),
                "runs_ok": stat.runs_ok,
                "runs_attempted": stat.runs_attempted,
                "api_calls": stat.call_count if stat.call_count is not None else "",
                "min_ms": e2e["min"] or "",
                "mean_ms": e2e["mean"] or "",
                "median_ms": e2e["median"] or "",
                "p95_ms": e2e["p95"] or "",
                "max_ms": e2e["max"] or "",
                "stdev_ms": e2e["stdev"] if e2e["stdev"] is not None else "",
                "note": stat.skip_reason or (stat.errors[-1][:200] if stat.errors else ""),
            })
    return path


def write_per_call_csv(all_stats: Sequence[FlowStats], path: str) -> str:
    ensure_results_dir(os.path.dirname(path))
    columns = ["gateway", "flow", "flow_label", "step", "call", "method", "samples",
               "min_ms", "mean_ms", "median_ms", "p95_ms", "max_ms", "stdev_ms"]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for stat in all_stats:
            for call in stat.per_call:
                writer.writerow({
                    "gateway": stat.gateway,
                    "flow": stat.flow,
                    "flow_label": FLOW_LABELS.get(stat.flow, stat.flow),
                    "step": call["step"],
                    "call": call["name"],
                    "method": call["method"],
                    "samples": call["n"],
                    "min_ms": call["min"],
                    "mean_ms": call["mean"],
                    "median_ms": call["median"],
                    "p95_ms": call["p95"],
                    "max_ms": call["max"],
                    "stdev_ms": call["stdev"],
                })
    return path
