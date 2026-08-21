#!/usr/bin/env python3
"""
Batch benchmark runner — the statistical companion to the web UI.

The web app measures one attempt per click, which is the right shape for "does this
gateway work and how long did that take". It is the wrong shape for a p95: one
sample is an anecdote. This runs each flow N times and reports the distribution.

It drives the same adapters the web app uses (app/adapters/), so there is exactly
one implementation of every gateway's call sequence and no possibility of the CLI
and the UI disagreeing about what a flow is.

    python scripts/bench.py                          # every configured gateway, every flow
    python scripts/bench.py --gateway geidea stripe  # a subset
    python scripts/bench.py --flow direct_api        # a subset of flows
    python scripts/bench.py --runs 5 --warmups 1     # a quick shakedown
    python scripts/bench.py --list                   # configuration status, then exit

Hosted checkout is deliberately excluded: its second leg only happens after a
human pays on the gateway's page, so it cannot be driven in a loop. Measure that
one through the web UI.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"), override=False)
except ImportError:
    if os.path.exists(os.path.join(ROOT, ".env")):
        print("A .env file exists but python-dotenv is not installed, so it is being "
              "ignored.\n  Fix: python3 -m pip install -r requirements.txt "
              "--break-system-packages", file=sys.stderr)

from app.adapters import FLOW_LABELS, build_adapters, get_adapter  # noqa: E402
from app.adapters.base import Card, Charge  # noqa: E402
from app.config import SETTINGS, env, env_float, env_int  # noqa: E402
from app.timing import (GatewayError, NotConfigured, NotSupported,  # noqa: E402
                        new_reference, summarize)

#: Everything except hosted checkout, which needs a browser and a human.
BATCH_FLOWS = ("direct_api", "capture", "refund", "void", "mit")


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    rule = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
                     for row in rows)
    return f"{line}\n{rule}\n{body}"


def test_card() -> Card:
    return Card(number=env("TEST_CARD_NUMBER", "4111111111111111") or "4111111111111111",
                month=env("TEST_CARD_MONTH", "12") or "12",
                year=env("TEST_CARD_YEAR", "2030") or "2030",
                cvc=env("TEST_CARD_CVC", "123") or "123",
                holder=env("TEST_CARD_HOLDER", "Benchmark Runner") or "Benchmark Runner")


def run_flow(adapter, flow: str, runs: int, warmups: int, delay_ms: int,
             amount: float, currency: str, verbose: bool) -> Dict[str, Any]:
    label = FLOW_LABELS.get(flow, flow)
    print(f"  {adapter.label} :: {label}", flush=True)

    totals: List[float] = []
    per_call: Dict[tuple, List[float]] = {}
    counts: List[int] = []
    errors: List[str] = []
    card = test_card()

    for index in range(warmups + runs):
        warmup = index < warmups
        charge = Charge(amount=amount, currency=currency,
                        reference=new_reference("bench"),
                        return_url=SETTINGS.url_for(f"/return/{adapter.name}"),
                        webhook_url=SETTINGS.url_for(f"/webhook/{adapter.name}"))
        with adapter.session_for(flow) as session:
            try:
                outcome = adapter.run_standalone(flow, session, charge, card)
                if outcome.status == "failed":
                    errors.append(outcome.detail)
                elif not warmup:
                    totals.append(session.total_ms)
                    counts.append(session.call_count)
                    for call in session.timed_calls:
                        per_call.setdefault((call.step, call.name, call.method), []).append(
                            call.elapsed_ms)
            except (NotConfigured, NotSupported) as exc:
                print(f"    skipped: {exc}", flush=True)
                return {"gateway": adapter.name, "flow": flow, "skipped": True,
                        "reason": str(exc), "runs_ok": 0, "errors": []}
            except GatewayError as exc:
                errors.append(str(exc))

            if verbose:
                for call in session.calls:
                    marker = " " if call.timed else "~"
                    print(f"      {marker} [{call.step}] {call.name:<44} "
                          f"{call.status_code} {call.elapsed_ms:>8.1f} ms", file=sys.stderr)

        # Three straight failures with nothing succeeding means the flow is broken,
        # not flaky. Stop rather than burn the whole batch on it.
        if len(errors) >= 3 and not totals:
            print(f"    aborting after {len(errors)} failures: {errors[-1][:150]}", flush=True)
            break
        if delay_ms:
            time.sleep(delay_ms / 1000.0)

    stats = summarize(totals)
    unique_counts = sorted(set(counts))
    result = {
        "gateway": adapter.name, "flow": flow, "skipped": False,
        "runs_ok": len(totals), "runs_attempted": runs,
        "call_count": unique_counts[0] if len(unique_counts) == 1 else None,
        "call_count_range": unique_counts,
        "end_to_end": stats,
        "per_call": [dict(step=s, name=n, method=m, **summarize(v))
                     for (s, n, m), v in sorted(per_call.items())],
        "errors": errors[:5],
    }
    if totals:
        spread = "" if len(unique_counts) == 1 else f" (varies {unique_counts[0]}-{unique_counts[-1]})"
        print(f"    {len(totals)}/{runs} ok | {result['call_count'] or unique_counts} "
              f"calls{spread} | median {stats['median']} ms | p95 {stats['p95']} ms",
              flush=True)
    elif errors:
        print(f"    0/{runs} ok | {errors[-1][:150]}", flush=True)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway", "-g", nargs="+", metavar="NAME")
    parser.add_argument("--flow", "-f", nargs="+", metavar="FLOW", choices=list(BATCH_FLOWS))
    parser.add_argument("--runs", "-n", type=int, default=env_int("RUNS_PER_CALL", 30))
    parser.add_argument("--warmups", type=int, default=env_int("WARMUP_RUNS", 2))
    parser.add_argument("--delay-ms", type=int, default=env_int("DELAY_BETWEEN_RUNS_MS", 0))
    parser.add_argument("--amount", type=float, default=env_float("TEST_AMOUNT", 10.0))
    parser.add_argument("--currency", default=env("TEST_CURRENCY", "SAR"))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--list", action="store_true",
                        help="Show which gateways are configured, then exit")
    parser.add_argument("--output-dir", default=os.path.join(ROOT, "results"))
    args = parser.parse_args(argv)

    adapters = build_adapters(SETTINGS.http_timeout)
    if args.gateway:
        unknown = set(args.gateway) - set(adapters)
        if unknown:
            raise SystemExit(f"unknown gateway(s): {', '.join(sorted(unknown))}. "
                             f"Known: {', '.join(adapters)}")
        adapters = {k: v for k, v in adapters.items() if k in args.gateway}

    print("=" * 74)
    print("busrapay batch benchmark")
    print("=" * 74)
    print(render_table(["Gateway", "Configured", "Missing"],
                       [[a.label, "yes" if a.configured() else "no",
                         ", ".join(a.missing_config()) or "-"] for a in adapters.values()]))

    if args.list:
        return 0

    runnable = [a for a in adapters.values() if a.configured()]
    if not runnable:
        print("\nNo gateway has credentials set. Copy .env.example to .env and fill in "
              "what you have — see docs/01_sandbox_signup_guide.md.")
        return 1

    flows = tuple(args.flow) if args.flow else BATCH_FLOWS
    print(f"\n{args.runs} measured runs per flow ({args.warmups} warmups discarded), "
          f"flows: {', '.join(flows)}")
    print("Hosted checkout is not in this list — its second leg needs a human on the "
          "gateway's page. Measure it through the web UI.\n")

    results: List[Dict[str, Any]] = []
    for adapter in runnable:
        print(f"\n--- {adapter.label} ---")
        for flow in flows:
            results.append(run_flow(adapter, flow, args.runs, args.warmups,
                                    args.delay_ms, args.amount, args.currency,
                                    args.verbose))

    print_summary(results, adapters)
    for path in write_results(results, runnable, args):
        print(f"  wrote {path}")
    return 0


def print_summary(results: Sequence[Dict[str, Any]], adapters) -> None:
    rows = []
    for result in results:
        if result["skipped"] or not result["runs_ok"]:
            continue
        stats = result["end_to_end"]
        rows.append([adapters[result["gateway"]].label,
                     FLOW_LABELS.get(result["flow"], result["flow"]),
                     result["runs_ok"],
                     result["call_count"] or "-".join(map(str, result["call_count_range"])),
                     f"{stats['min']:.1f}", f"{stats['mean']:.1f}",
                     f"{stats['median']:.1f}", f"{stats['p95']:.1f}", f"{stats['max']:.1f}"])
    if not rows:
        return
    print("\n" + "=" * 74)
    print("END-TO-END LATENCY (ms, warmups discarded)")
    print("=" * 74)
    print(render_table(["Gateway", "Flow", "Runs", "Calls", "min", "mean", "median",
                        "p95", "max"], rows))

    doc_rows = []
    for result in results:
        if result["skipped"] or not result["runs_ok"]:
            continue
        adapter = adapters[result["gateway"]]
        documented = adapter.documented_calls.get(result["flow"], "-")
        measured = result["call_count"]
        doc_rows.append([adapter.label, FLOW_LABELS.get(result["flow"], result["flow"]),
                         documented, measured,
                         "yes" if str(measured) in documented else "CHECK"])
    print("\n" + "=" * 74)
    print("DOCUMENTED vs MEASURED CALL COUNT")
    print("=" * 74)
    print(render_table(["Gateway", "Flow", "Documented", "Measured", "Agrees?"], doc_rows))


def write_results(results: Sequence[Dict[str, Any]], runnable, args) -> List[str]:
    os.makedirs(args.output_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runs_per_flow": args.runs, "warmup_runs": args.warmups,
        "amount": args.amount, "currency": args.currency,
        "gateways_run": [a.name for a in runnable],
        "documented_calls": {a.name: a.documented_calls for a in runnable},
        "gateway_notes": {a.name: a.notes for a in runnable},
        "host": socket.gethostname(), "platform": platform.platform(),
        "python": platform.python_version(),
        # Latency without a location is a number without a unit.
        "measurement_location": env(
            "MEASUREMENT_LOCATION",
            "NOT SET — set MEASUREMENT_LOCATION so results record where they ran"),
    }

    json_path = os.path.join(args.output_dir, f"benchmark_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "results": list(results)}, handle, indent=2)

    csv_path = os.path.join(args.output_dir, f"summary_{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["gateway", "flow", "status", "runs_ok", "api_calls", "min_ms",
                         "mean_ms", "median_ms", "p95_ms", "max_ms", "stdev_ms", "note"])
        for result in results:
            stats = result.get("end_to_end") or {}
            writer.writerow([
                result["gateway"], result["flow"],
                "skipped" if result["skipped"] else ("ok" if result["runs_ok"] else "failed"),
                result["runs_ok"], result.get("call_count") or "",
                stats.get("min") or "", stats.get("mean") or "", stats.get("median") or "",
                stats.get("p95") or "", stats.get("max") or "", stats.get("stdev") or "",
                result.get("reason") or (result["errors"][-1][:200]
                                         if result.get("errors") else "")])

    latest = os.path.join(args.output_dir, "latest.json")
    with open(latest, "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "results": list(results)}, handle, indent=2)
    return [json_path, csv_path, latest]


if __name__ == "__main__":
    sys.exit(main())
