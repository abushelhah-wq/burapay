#!/usr/bin/env python3
"""
Run the whole benchmark against the local mock gateway - no credentials needed.

Use this to confirm the toolchain works end to end (dependencies installed, flows
execute, results written, workbook built) before you have a single sandbox key,
and to see the shape of the output you will get from a real run.

    python demo.py
    python demo.py --runs 10 --keep      # keep the generated files for inspection

The latency figures are meaningless - they measure a loopback socket, not a
payment gateway. Only the structure is real: call counts, sequencing, and the
files produced.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "tests"))

from mock_gateway import MockGatewayServer  # noqa: E402


def mock_env(server: MockGatewayServer) -> dict:
    """Credentials and base URLs pointing every gateway at its slice of the mock."""
    return {
        "GEIDEA_PUBLIC_KEY": "pk_demo", "GEIDEA_API_PASSWORD": "pw_demo",
        "GEIDEA_API_BASE": server.base_url_for("geidea"),
        "GEIDEA_TOKEN_ID": "tok_demo", "GEIDEA_AGREEMENT_ID": "agr_demo",
        "ADYEN_API_KEY": "key_demo", "ADYEN_MERCHANT_ACCOUNT": "DemoMerchant",
        "ADYEN_API_BASE": server.base_url_for("adyen"),
        "ADYEN_STORED_PAYMENT_METHOD_ID": "spm_demo",
        "STRIPE_SECRET_KEY": "sk_test_demo",
        "STRIPE_API_BASE": server.base_url_for("stripe"),
        "CHECKOUT_SECRET_KEY": "sk_sbox_demo", "CHECKOUT_PUBLIC_KEY": "pk_sbox_demo",
        "CHECKOUT_API_BASE": server.base_url_for("checkout_com"),
        "HYPERPAY_ACCESS_TOKEN": "tok_demo", "HYPERPAY_ENTITY_ID": "ent_demo",
        "HYPERPAY_API_BASE": server.base_url_for("hyperpay"),
        "MOYASAR_SECRET_KEY": "sk_test_demo", "MOYASAR_PUBLISHABLE_KEY": "pk_test_demo",
        "MOYASAR_API_BASE": server.base_url_for("moyasar"),
        "MEASUREMENT_LOCATION": "DEMO - local mock gateway, latency is not meaningful",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=5, help="Measured runs per flow")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--keep", action="store_true",
                        help="Write into ../results instead of a temp directory")
    args = parser.parse_args(argv)

    with MockGatewayServer() as server:
        print(f"Mock gateway on {server.base_url}\n")
        saved = dict(os.environ)
        os.environ.update(mock_env(server))
        # A stray real .env must not leak into a demo run.
        os.environ["RUNS_PER_CALL"] = str(args.runs)
        try:
            import run_all

            output_dir = None
            temp_dir = None
            if args.keep:
                from harness import RESULTS_DIR
                output_dir = RESULTS_DIR
            else:
                temp_dir = tempfile.mkdtemp(prefix="gateway-benchmark-demo-")
                output_dir = temp_dir

            code = run_all.main(["--runs", str(args.runs), "--warmups", str(args.warmups),
                                 "--output-dir", output_dir])
            if code != 0:
                return code

            import build_workbook

            workbook = os.path.join(output_dir, "gateway_benchmark_workbook.xlsx")
            build_workbook.main(["--results", os.path.join(output_dir, "latest.json"),
                                 "--output", workbook])
            print(f"\nDemo complete. Files in {output_dir}")
            if temp_dir:
                print("(temporary - re-run with --keep to write into results/)")
        finally:
            os.environ.clear()
            os.environ.update(saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
