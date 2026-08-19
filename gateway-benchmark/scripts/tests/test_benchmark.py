"""
Self-tests for the benchmark harness and all six gateway modules.

Run from the scripts directory:

    python -m unittest discover -s tests -v
    # or
    python tests/test_benchmark.py

Every gateway module is driven against tests/mock_gateway.py, which answers the
documented endpoints with correctly shaped responses. That verifies request
construction, response parsing, call sequencing, and the timed-vs-prep split
without needing a single sandbox credential. It does NOT verify that a real
gateway accepts these requests - only a live run does that.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "tests"))

import harness  # noqa: E402
from harness import (BenchmarkRunner, FlowRun, Gateway, GatewayError,  # noqa: E402
                     MeasuredSession, SkipFlow, percentile, summarize,
                     write_json, write_per_call_csv, write_summary_csv)
from mock_gateway import MockGatewayServer  # noqa: E402

import adyen  # noqa: E402
import checkout_com  # noqa: E402
import geidea  # noqa: E402
import hyperpay  # noqa: E402
import moyasar  # noqa: E402
import stripe_gw  # noqa: E402


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

class TestStatistics(unittest.TestCase):

    def test_summarize_basic(self):
        stats = summarize([10.0, 20.0, 30.0, 40.0])
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["max"], 40.0)
        self.assertEqual(stats["mean"], 25.0)
        self.assertEqual(stats["median"], 25.0)

    def test_summarize_empty(self):
        stats = summarize([])
        self.assertEqual(stats["n"], 0)
        self.assertIsNone(stats["median"])
        self.assertIsNone(stats["p95"])

    def test_summarize_single_value_has_zero_stdev(self):
        self.assertEqual(summarize([7.5])["stdev"], 0.0)

    def test_percentile_is_nearest_rank(self):
        values = [float(i) for i in range(1, 101)]
        self.assertEqual(percentile(values, 95), 95.0)
        self.assertEqual(percentile(values, 50), 50.0)
        self.assertEqual(percentile(values, 100), 100.0)

    def test_percentile_never_interpolates(self):
        # With 30 samples the p95 must be one of the observed values, not between two.
        values = [float(i) * 3 for i in range(1, 31)]
        self.assertIn(percentile(values, 95), values)

    def test_percentile_of_empty_is_none(self):
        self.assertIsNone(percentile([], 95))


# --------------------------------------------------------------------------- #
# Session accounting
# --------------------------------------------------------------------------- #

class TestMeasuredSession(unittest.TestCase):

    def setUp(self):
        self.server = MockGatewayServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def _session(self, flow="test"):
        run = FlowRun(gateway="mock", flow=flow, run_index=0, warmup=False)
        return MeasuredSession("mock", flow, run), run

    def test_prep_calls_are_excluded_from_count_and_timing(self):
        session, run = self._session()
        session.prep("setup", "POST", f"{self.server.base_url}/v1/payment_methods",
                     data={"type": "card"})
        session.call("measured", "POST", f"{self.server.base_url}/v1/payment_intents",
                     data={"amount": "1000"})
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.timed_calls[0].name, "measured")
        # total_ms covers only the timed call
        self.assertAlmostEqual(run.total_ms, run.timed_calls[0].elapsed_ms, places=3)
        session.close()

    def test_elapsed_is_recorded_and_positive(self):
        session, run = self._session()
        session.call("ping", "GET", f"{self.server.base_url}/v1/payments/pmt_1")
        self.assertGreater(run.calls[0].elapsed_ms, 0)
        session.close()

    def test_non_2xx_is_recorded_not_raised(self):
        session, run = self._session()
        response = session.call("missing", "GET", f"{self.server.base_url}/no/such/route")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(run.calls[0].ok)
        self.assertEqual(run.calls[0].error, "HTTP 404")
        self.assertIsNotNone(run.calls[0].snippet)
        session.close()

    def test_expect_ok_raises_gateway_error_on_bad_status(self):
        session, _run = self._session()
        response = session.call("missing", "GET", f"{self.server.base_url}/no/such/route")
        with self.assertRaises(GatewayError):
            session.expect_ok(response, "fetch something")
        session.close()

    def test_connection_failure_becomes_gateway_error(self):
        session, run = self._session()
        with self.assertRaises(GatewayError):
            # Port 1 is reserved and refuses connections.
            session.call("dead", "GET", "http://127.0.0.1:1/nothing", timeout=2)
        self.assertEqual(len(run.calls), 1)
        self.assertIsNotNone(run.calls[0].error)
        session.close()


# --------------------------------------------------------------------------- #
# Runner behaviour
# --------------------------------------------------------------------------- #

class _CountingGateway(Gateway):
    name = "counting"
    label = "Counting"

    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.invocations = 0

    def configured(self):
        return True

    def flow_direct_api(self, s):
        self.invocations += 1
        s.call("one", "GET", f"{self.base_url}/v1/payments/pmt_1")
        s.call("two", "GET", f"{self.base_url}/v1/payments/pmt_2")

    def flow_capture(self, s):
        raise SkipFlow("not supported here")

    def flow_refund(self, s):
        raise GatewayError("always broken")


class TestRunner(unittest.TestCase):

    def setUp(self):
        self.server = MockGatewayServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.gateway = _CountingGateway(self.server.base_url)

    def test_warmups_are_discarded_from_stats_but_still_executed(self):
        runner = BenchmarkRunner(runs=4, warmups=2, flows=["direct_api"])
        stats = runner.run_flow(self.gateway, "direct_api")
        self.assertEqual(self.gateway.invocations, 6)   # 4 measured + 2 warmups
        self.assertEqual(stats.runs_ok, 4)              # only measured runs counted
        self.assertEqual(stats.end_to_end["n"], 4)

    def test_call_count_reflects_timed_calls(self):
        runner = BenchmarkRunner(runs=2, warmups=0, flows=["direct_api"])
        stats = runner.run_flow(self.gateway, "direct_api")
        self.assertEqual(stats.call_count, 2)
        self.assertEqual([c["name"] for c in stats.per_call], ["one", "two"])
        self.assertEqual(stats.per_call[0]["n"], 2)

    def test_skip_is_reported_without_failing(self):
        runner = BenchmarkRunner(runs=3, warmups=0, flows=["capture"])
        stats = runner.run_flow(self.gateway, "capture")
        self.assertTrue(stats.skipped)
        self.assertEqual(stats.runs_ok, 0)
        self.assertIn("not supported", stats.skip_reason)

    def test_persistent_failure_aborts_early(self):
        runner = BenchmarkRunner(runs=30, warmups=0, flows=["refund"])
        stats = runner.run_flow(self.gateway, "refund")
        self.assertEqual(stats.runs_ok, 0)
        self.assertFalse(stats.skipped)
        # Aborts after 3 straight failures rather than burning all 30 attempts.
        self.assertLessEqual(stats.runs_attempted, 3)
        self.assertTrue(stats.errors)


# --------------------------------------------------------------------------- #
# Output files
# --------------------------------------------------------------------------- #

class TestOutputs(unittest.TestCase):

    def setUp(self):
        self.server = MockGatewayServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        gateway = _CountingGateway(self.server.base_url)
        runner = BenchmarkRunner(runs=2, warmups=0, flows=["direct_api", "capture"])
        self.stats = runner.run_gateway(gateway)
        self.tmp = tempfile.mkdtemp()

    def test_summary_csv_has_a_row_per_flow(self):
        path = write_summary_csv(self.stats, os.path.join(self.tmp, "summary.csv"))
        with open(path, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        by_flow = {r["flow"]: r for r in rows}
        self.assertEqual(by_flow["direct_api"]["status"], "ok")
        self.assertEqual(by_flow["direct_api"]["api_calls"], "2")
        self.assertEqual(by_flow["capture"]["status"], "skipped")

    def test_per_call_csv_has_a_row_per_step(self):
        path = write_per_call_csv(self.stats, os.path.join(self.tmp, "per_call.csv"))
        with open(path, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r["call"] for r in rows], ["one", "two"])

    def test_json_round_trips(self):
        path = write_json(self.stats, {"note": "test"}, os.path.join(self.tmp, "b.json"))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["meta"]["note"], "test")
        self.assertEqual(len(payload["results"]), 2)
        self.assertIn("per_call", payload["results"][0])


# --------------------------------------------------------------------------- #
# Gateway modules against the mock
# --------------------------------------------------------------------------- #

#: Expected number of *timed* calls per gateway+flow when run against the mock.
#: These mirror the documented minimums in docs/02_api_flow_comparison.md - if a
#: module's sequence changes, this table is what catches it.
EXPECTED_CALLS = {
    "geidea": {"hosted_checkout": 2, "direct_api": 4, "capture": 1, "refund": 1,
               "void": 1, "mit": 2},
    "adyen": {"hosted_checkout": 1, "direct_api": 2, "capture": 1, "refund": 1,
              "void": 1, "mit": 1},
    "stripe": {"hosted_checkout": 2, "direct_api": 2, "capture": 1, "refund": 1,
               "void": 1, "mit": 1},
    "checkout_com": {"hosted_checkout": 2, "direct_api": 1, "capture": 1, "refund": 1,
                     "void": 1, "mit": 1},
    "hyperpay": {"hosted_checkout": 2, "direct_api": 1, "capture": 1, "refund": 1,
                 "void": 1, "mit": 1},
    "moyasar": {"hosted_checkout": 2, "direct_api": 3, "capture": 1, "refund": 1,
                "void": 1, "mit": 1},
}


class TestGatewayFlows(unittest.TestCase):
    """Drive every flow of every gateway against the mock and check the call sequence."""

    @classmethod
    def setUpClass(cls):
        cls.server = MockGatewayServer()
        cls.server.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def _env(self, **overrides):
        """Point each gateway at its own slice of the mock, with credentials filled in.

        HyperPay and Moyasar both serve POST /v1/payments, so every gateway gets a
        /_<name>-prefixed base URL and the mock resolves routes within that namespace.
        """
        values = {
            "GEIDEA_PUBLIC_KEY": "pk_test", "GEIDEA_API_PASSWORD": "pw_test",
            "GEIDEA_API_BASE": self.server.base_url_for("geidea"),
            "GEIDEA_TOKEN_ID": "tok_geidea", "GEIDEA_AGREEMENT_ID": "agr_geidea",
            "ADYEN_API_KEY": "key_test", "ADYEN_MERCHANT_ACCOUNT": "MerchantECOM",
            "ADYEN_API_BASE": self.server.base_url_for("adyen"),
            "ADYEN_STORED_PAYMENT_METHOD_ID": "spm_test",
            "STRIPE_SECRET_KEY": "sk_test_x",
            "STRIPE_API_BASE": self.server.base_url_for("stripe"),
            "CHECKOUT_SECRET_KEY": "sk_sbox_x", "CHECKOUT_PUBLIC_KEY": "pk_sbox_x",
            "CHECKOUT_API_BASE": self.server.base_url_for("checkout_com"),
            "HYPERPAY_ACCESS_TOKEN": "tok_x", "HYPERPAY_ENTITY_ID": "ent_x",
            "HYPERPAY_API_BASE": self.server.base_url_for("hyperpay"),
            "MOYASAR_SECRET_KEY": "sk_test_x", "MOYASAR_PUBLISHABLE_KEY": "pk_test_x",
            "MOYASAR_API_BASE": self.server.base_url_for("moyasar"),
        }
        values.update(overrides)
        return values

    def _run(self, gateway_cls, flow, **env_overrides):
        saved = dict(os.environ)
        os.environ.update(self._env(**env_overrides))
        try:
            gateway = gateway_cls()
            self.assertTrue(gateway.configured(),
                            f"{gateway.label} should be configured in the test env")
            runner = BenchmarkRunner(runs=1, warmups=0, flows=[flow])
            return runner.run_flow(gateway, flow)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def _assert_flow(self, gateway_cls, name, flow):
        stats = self._run(gateway_cls, flow)
        self.assertFalse(stats.skipped, f"{name}/{flow} unexpectedly skipped: "
                                        f"{stats.skip_reason}")
        self.assertEqual(stats.runs_ok, 1,
                         f"{name}/{flow} failed: {stats.errors[-1] if stats.errors else '?'}")
        self.assertEqual(stats.call_count, EXPECTED_CALLS[name][flow],
                         f"{name}/{flow} made {stats.call_count} timed calls, "
                         f"expected {EXPECTED_CALLS[name][flow]}")
        self.assertGreater(stats.end_to_end["median"], 0)

    # One test per gateway keeps failures readable.

    def test_geidea_flows(self):
        for flow in EXPECTED_CALLS["geidea"]:
            with self.subTest(flow=flow):
                self._assert_flow(geidea.Geidea, "geidea", flow)

    def test_adyen_flows(self):
        for flow in EXPECTED_CALLS["adyen"]:
            with self.subTest(flow=flow):
                self._assert_flow(adyen.Adyen, "adyen", flow)

    def test_stripe_flows(self):
        for flow in EXPECTED_CALLS["stripe"]:
            with self.subTest(flow=flow):
                self._assert_flow(stripe_gw.Stripe, "stripe", flow)

    def test_checkout_com_flows(self):
        for flow in EXPECTED_CALLS["checkout_com"]:
            with self.subTest(flow=flow):
                self._assert_flow(checkout_com.CheckoutCom, "checkout_com", flow)

    def test_hyperpay_flows(self):
        for flow in EXPECTED_CALLS["hyperpay"]:
            with self.subTest(flow=flow):
                self._assert_flow(hyperpay.HyperPay, "hyperpay", flow)

    def test_moyasar_flows(self):
        for flow in EXPECTED_CALLS["moyasar"]:
            with self.subTest(flow=flow):
                self._assert_flow(moyasar.Moyasar, "moyasar", flow)

    # -- flow-specific behaviour ----------------------------------------- #

    def test_geidea_frictionless_shortcut_drops_to_three_calls(self):
        """The headline hypothesis: a frictionless initiate response should skip step 3.

        The mock's initiate response says 'Enrolled', so the default run makes all four
        calls. Forcing the frictionless marker must produce three - proving the module
        measures the real sequence rather than asserting the documented one.
        """
        from mock_gateway import RESPONSE_OVERRIDES

        RESPONSE_OVERRIDES["geidea_initiate"] = (
            200, {"responseCode": "000", "enrollmentStatus": "Not Enrolled"})
        self.addCleanup(RESPONSE_OVERRIDES.pop, "geidea_initiate", None)

        stats = self._run(geidea.Geidea, "direct_api")
        self.assertEqual(stats.runs_ok, 1)
        self.assertEqual(stats.call_count, 3)

    def test_geidea_force_full_3ds_keeps_four_calls(self):
        stats = self._run(geidea.Geidea, "direct_api", GEIDEA_FORCE_FULL_3DS="1")
        self.assertEqual(stats.call_count, 4)

    def test_adyen_mit_skips_without_stored_payment_method(self):
        stats = self._run(adyen.Adyen, "mit", ADYEN_STORED_PAYMENT_METHOD_ID="")
        self.assertTrue(stats.skipped)
        self.assertIn("ADYEN_STORED_PAYMENT_METHOD_ID", stats.skip_reason)

    def test_geidea_mit_skips_without_token(self):
        stats = self._run(geidea.Geidea, "mit", GEIDEA_TOKEN_ID="")
        self.assertTrue(stats.skipped)
        self.assertIn("GEIDEA_TOKEN_ID", stats.skip_reason)

    def test_moyasar_raw_card_mode_is_two_calls(self):
        stats = self._run(moyasar.Moyasar, "direct_api", MOYASAR_DIRECT_MODE="card")
        self.assertEqual(stats.call_count, 2)

    def test_hyperpay_backoffice_path_is_switchable(self):
        saved = dict(os.environ)
        os.environ.update(self._env(HYPERPAY_BACKOFFICE_PATH="nested"))
        try:
            gateway = hyperpay.HyperPay()
            self.assertTrue(gateway._backoffice_url("abc").endswith("/v1/payments/abc/payments"))
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_geidea_signature_is_deterministic_hmac(self):
        import base64
        import hmac
        import hashlib

        saved = dict(os.environ)
        os.environ.update(self._env())
        try:
            gateway = geidea.Geidea()
            signature = gateway.sign(amount=10.0, currency="EGP", reference="ref1",
                                     timestamp="2026-08-19T10:00:00")
            expected = base64.b64encode(hmac.new(
                b"pw_test", b"pk_test10.00EGPref12026-08-19T10:00:00",
                hashlib.sha256).digest()).decode()
            self.assertEqual(signature, expected)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class TestDocumentedCallsTable(unittest.TestCase):
    """Every gateway must document a call count for every flow the benchmark measures."""

    def test_all_flows_documented(self):
        for module_gateway in (geidea.Geidea, adyen.Adyen, stripe_gw.Stripe,
                               checkout_com.CheckoutCom, hyperpay.HyperPay,
                               moyasar.Moyasar):
            for flow in harness.FLOWS:
                with self.subTest(gateway=module_gateway.name, flow=flow):
                    self.assertIn(flow, module_gateway.documented_calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
