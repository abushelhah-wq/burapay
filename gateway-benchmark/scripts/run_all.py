#!/usr/bin/env python3
"""
Run the benchmark across every configured gateway and print a comparison table.

Usage
-----
    python run_all.py                              # every configured gateway, every flow
    python run_all.py --gateway stripe moyasar     # a subset of gateways
    python run_all.py --flow direct_api            # a subset of flows
    python run_all.py --runs 5 --warmups 1         # a quick shakedown
    python run_all.py --verbose                    # per-call timings as they happen
    python run_all.py --list                       # which gateways are configured, then exit

Credentials come from the environment, or from a .env file next to this script
(see .env.example). Gateways with no credentials are reported as "not configured"
and skipped - they never fail the run.

Results land in ../results/:
    benchmark_<timestamp>.json        full detail, every call of every run
    summary_<timestamp>.csv           one row per gateway+flow
    per_call_<timestamp>.csv          one row per gateway+flow+step
    latest.json / latest_summary.csv  copies of the newest run, for the workbook builder
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
from datetime import datetime, timezone
from typing import Dict, List, Sequence

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def _load_dotenv() -> None:
    """Load scripts/.env if python-dotenv is installed, with a clear message if it is not."""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    try:
        from dotenv import load_dotenv
    except ImportError:
        if os.path.exists(env_path):
            print("scripts/.env exists but python-dotenv is not installed - values in it "
                  "are being ignored.\n"
                  "  Fix: python3 -m pip install -r requirements.txt --break-system-packages\n"
                  "  (see the README's ModuleNotFoundError troubleshooting section)",
                  file=sys.stderr)
        return
    load_dotenv(env_path, override=False)


_load_dotenv()

from harness import (FLOWS, FLOW_LABELS, BenchmarkRunner, FlowStats,  # noqa: E402
                     Gateway, RESULTS_DIR, env, env_int, ensure_results_dir,
                     write_json, write_per_call_csv, write_summary_csv)

import adyen  # noqa: E402
import checkout_com  # noqa: E402
import geidea  # noqa: E402
import hyperpay  # noqa: E402
import moyasar  # noqa: E402
import stripe_gw  # noqa: E402

# Geidea first: it is the baseline every other column is compared against.
GATEWAY_CLASSES = [
    geidea.Geidea,
    adyen.Adyen,
    stripe_gw.Stripe,
    checkout_com.CheckoutCom,
    hyperpay.HyperPay,
    moyasar.Moyasar,
]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a table with `tabulate` when available, falling back to plain padding."""
    try:
        from tabulate import tabulate
        return tabulate(rows, headers=headers, tablefmt="github")
    except ImportError:
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))
        line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
        rule = "  ".join("-" * widths[i] for i in range(len(headers)))
        body = "\n".join("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
                         for row in rows)
        return f"{line}\n{rule}\n{body}"


def _cell(stat: FlowStats | None) -> str:
    if stat is None:
        return "-"
    if stat.skipped:
        return "skip"
    if not stat.runs_ok:
        return "FAIL"
    median = stat.end_to_end["median"]
    plural = "call" if stat.call_count == 1 else "calls"
    return f"{stat.call_count} {plural} / {median:.0f} ms"


def print_comparison(all_stats: Dict[str, List[FlowStats]], flows: Sequence[str]) -> None:
    """Gateway x flow grid of 'call count / median end-to-end ms'."""
    by_gateway = {name: {s.flow: s for s in stats} for name, stats in all_stats.items()}
    gateways = list(all_stats.keys())

    headers = ["Flow"] + gateways
    rows = []
    for flow in flows:
        rows.append([FLOW_LABELS.get(flow, flow)]
                    + [_cell(by_gateway[g].get(flow)) for g in gateways])

    print("\n" + "=" * 78)
    print("COMPARISON - API calls / median end-to-end latency")
    print("=" * 78)
    print(_render_table(headers, rows))


def print_latency_detail(all_stats: Dict[str, List[FlowStats]]) -> None:
    """Full min/mean/median/p95/max per gateway+flow."""
    headers = ["Gateway", "Flow", "Runs", "Calls", "min", "mean", "median", "p95", "max"]
    rows = []
    for gateway, stats in all_stats.items():
        for stat in stats:
            if stat.skipped or not stat.runs_ok:
                continue
            e2e = stat.end_to_end
            rows.append([
                gateway, FLOW_LABELS.get(stat.flow, stat.flow),
                f"{stat.runs_ok}/{stat.runs_attempted}", stat.call_count,
                f"{e2e['min']:.1f}", f"{e2e['mean']:.1f}", f"{e2e['median']:.1f}",
                f"{e2e['p95']:.1f}", f"{e2e['max']:.1f}",
            ])
    if not rows:
        return
    print("\n" + "=" * 78)
    print("END-TO-END LATENCY (ms, warmups discarded)")
    print("=" * 78)
    print(_render_table(headers, rows))


def print_doc_vs_measured(gateways: Sequence[Gateway],
                          all_stats: Dict[str, List[FlowStats]]) -> None:
    """Where the measured call count differs from what the vendor documents.

    This is the point of the exercise: a documented minimum that the sandbox does not
    reproduce is a finding, in either direction.
    """
    headers = ["Gateway", "Flow", "Documented", "Measured"]
    rows = []
    for gateway in gateways:
        for stat in all_stats.get(gateway.label, []):
            documented = gateway.documented_calls.get(stat.flow, "-")
            if stat.skipped or not stat.runs_ok:
                continue
            rows.append([gateway.label, FLOW_LABELS.get(stat.flow, stat.flow),
                         documented, str(stat.call_count)])
    if not rows:
        return
    print("\n" + "=" * 78)
    print("DOCUMENTED vs MEASURED CALL COUNT")
    print("=" * 78)
    print(_render_table(headers, rows))


def print_notes(gateways: Sequence[Gateway], all_stats: Dict[str, List[FlowStats]]) -> None:
    printed_header = False
    for gateway in gateways:
        relevant = [s for s in all_stats.get(gateway.label, [])
                    if s.skipped or (not s.runs_ok and s.errors)]
        if not gateway.notes and not relevant:
            continue
        if not printed_header:
            print("\n" + "=" * 78)
            print("CAVEATS AND SKIPS")
            print("=" * 78)
            printed_header = True
        print(f"\n{gateway.label}")
        for note in gateway.notes:
            print(f"  - {note}")
        for stat in relevant:
            label = FLOW_LABELS.get(stat.flow, stat.flow)
            reason = stat.skip_reason or (stat.errors[-1] if stat.errors else "unknown")
            verb = "skipped" if stat.skipped else "failed"
            print(f"  ! {label} {verb}: {reason[:220]}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Payment gateway API response-time benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--gateway", "-g", nargs="+", metavar="NAME",
                        help="Gateway names to run (default: every configured one)")
    parser.add_argument("--flow", "-f", nargs="+", metavar="FLOW", choices=list(FLOWS),
                        help=f"Flows to run (default: all of {', '.join(FLOWS)})")
    parser.add_argument("--runs", "-n", type=int,
                        help="Measured runs per flow (default: RUNS_PER_CALL or 30)")
    parser.add_argument("--warmups", type=int,
                        help="Warmup runs discarded before measuring (default: WARMUP_RUNS or 2)")
    parser.add_argument("--delay-ms", type=int,
                        help="Pause between runs, for rate-limited gateways "
                             "(default: DELAY_BETWEEN_RUNS_MS or 0)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print every call as it completes")
    parser.add_argument("--list", action="store_true",
                        help="Show which gateways are configured, then exit")
    parser.add_argument("--output-dir", default=RESULTS_DIR,
                        help="Where to write results (default: ../results)")
    return parser.parse_args(argv)


def build_gateways(selected: Sequence[str] | None) -> List[Gateway]:
    wanted = {name.lower() for name in selected} if selected else None
    instances = []
    for cls in GATEWAY_CLASSES:
        instance = cls()
        if wanted and instance.name.lower() not in wanted:
            continue
        instances.append(instance)
    if wanted:
        known = {cls.name for cls in GATEWAY_CLASSES}
        unknown = wanted - known
        if unknown:
            raise SystemExit(f"unknown gateway(s): {', '.join(sorted(unknown))}. "
                             f"Known: {', '.join(sorted(known))}")
    return instances


def print_config_status(gateways: Sequence[Gateway]) -> None:
    headers = ["Gateway", "Configured", "Missing environment variables"]
    rows = [[g.label, "yes" if g.configured() else "no", ", ".join(g.missing_config()) or "-"]
            for g in gateways]
    print(_render_table(headers, rows))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    gateways = build_gateways(args.gateway)

    if args.list:
        print_config_status(gateways)
        return 0

    runnable = [g for g in gateways if g.configured()]
    unconfigured = [g for g in gateways if not g.configured()]

    print("=" * 78)
    print("PAYMENT GATEWAY API BENCHMARK")
    print("=" * 78)
    print_config_status(gateways)

    if not runnable:
        print("\nNo gateway has credentials set. Copy .env.example to .env and fill in "
              "what you have - see docs/01_sandbox_signup_guide.md.")
        return 1

    flows = tuple(args.flow) if args.flow else FLOWS
    runner = BenchmarkRunner(runs=args.runs, warmups=args.warmups, flows=flows,
                             verbose=args.verbose, delay_ms=args.delay_ms)

    print(f"\n{runner.runs} measured runs per flow "
          f"({runner.warmups} warmups discarded), flows: {', '.join(flows)}\n")

    all_stats: Dict[str, List[FlowStats]] = {}
    flat_stats: List[FlowStats] = []
    for gateway in runnable:
        print(f"\n--- {gateway.label} ---")
        stats = runner.run_gateway(gateway)
        all_stats[gateway.label] = stats
        flat_stats.extend(stats)

    print_comparison(all_stats, flows)
    print_latency_detail(all_stats)
    print_doc_vs_measured(runnable, all_stats)
    print_notes(runnable, all_stats)

    paths = write_results(flat_stats, runnable, unconfigured, runner, args.output_dir)
    print("\n" + "=" * 78)
    print("RESULTS WRITTEN")
    print("=" * 78)
    for path in paths:
        print(f"  {path}")
    print("\nNext: python build_workbook.py   (folds these numbers into "
          "results/gateway_benchmark_workbook.xlsx)")
    return 0


def write_results(stats: Sequence[FlowStats], runnable: Sequence[Gateway],
                  unconfigured: Sequence[Gateway], runner: BenchmarkRunner,
                  output_dir: str) -> List[str]:
    ensure_results_dir(output_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runs_per_flow": runner.runs,
        "warmup_runs": runner.warmups,
        "flows": list(runner.flows),
        "amount": env("TEST_AMOUNT", "10.00"),
        "currency": env("TEST_CURRENCY", "USD"),
        "gateways_run": [g.name for g in runnable],
        "gateways_not_configured": [g.name for g in unconfigured],
        "documented_calls": {g.name: g.documented_calls for g in runnable},
        "gateway_notes": {g.name: g.notes for g in runnable},
        # Latency is meaningless without knowing where it was measured from. Region
        # matters more than anything else in this benchmark.
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "measurement_location_note": env(
            "MEASUREMENT_LOCATION",
            "NOT SET - set MEASUREMENT_LOCATION so results state where they were run from"),
    }

    json_path = write_json(stats, meta, os.path.join(output_dir, f"benchmark_{stamp}.json"))
    summary_path = write_summary_csv(stats, os.path.join(output_dir, f"summary_{stamp}.csv"))
    per_call_path = write_per_call_csv(stats, os.path.join(output_dir, f"per_call_{stamp}.csv"))

    latest_json = os.path.join(output_dir, "latest.json")
    latest_summary = os.path.join(output_dir, "latest_summary.csv")
    latest_per_call = os.path.join(output_dir, "latest_per_call.csv")
    shutil.copyfile(json_path, latest_json)
    shutil.copyfile(summary_path, latest_summary)
    shutil.copyfile(per_call_path, latest_per_call)

    return [json_path, summary_path, per_call_path, latest_json, latest_summary,
            latest_per_call]


if __name__ == "__main__":
    sys.exit(main())
