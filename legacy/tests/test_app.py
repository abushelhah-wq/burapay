"""
Tests for the busrapay web application.

Every adapter is driven against tests/mock_gateway.py, which answers the documented
endpoints with correctly shaped responses. That verifies request construction,
response parsing, call sequencing and the timed/prep split without a single sandbox
credential. It does not verify that a real gateway accepts these requests — only a
live run does that.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from mock_gateway import MockGatewayServer, RESPONSE_OVERRIDES  # noqa: E402


def mock_env(server: MockGatewayServer) -> dict:
    """Credentials and per-gateway base URLs pointing at the mock."""
    return {
        "GEIDEA_PUBLIC_KEY": "pk_test", "GEIDEA_API_PASSWORD": "pw_test",
        "GEIDEA_API_BASE": server.base_url_for("geidea"),
        "GEIDEA_TOKEN_ID": "tok_geidea", "GEIDEA_AGREEMENT_ID": "agr_geidea",
        "ADYEN_API_KEY": "key", "ADYEN_MERCHANT_ACCOUNT": "Merchant",
        "ADYEN_API_BASE": server.base_url_for("adyen"),
        "ADYEN_STORED_PAYMENT_METHOD_ID": "spm_test",
        "STRIPE_SECRET_KEY": "sk_test",
        "STRIPE_API_BASE": server.base_url_for("stripe"),
        "CHECKOUT_SECRET_KEY": "sk_sbox", "CHECKOUT_PUBLIC_KEY": "pk_sbox",
        "CHECKOUT_API_BASE": server.base_url_for("checkout_com"),
        "HYPERPAY_ACCESS_TOKEN": "tok", "HYPERPAY_ENTITY_ID": "ent",
        "HYPERPAY_API_BASE": server.base_url_for("hyperpay"),
        "MOYASAR_SECRET_KEY": "sk_test", "MOYASAR_PUBLISHABLE_KEY": "pk_test",
        "MOYASAR_API_BASE": server.base_url_for("moyasar"),
        "PUBLIC_BASE_URL": "https://busrapay.test",
    }


class EnvMixin:
    """Applies mock credentials for the duration of a test, then restores them."""

    def use_env(self, **overrides):
        saved = dict(os.environ)
        values = mock_env(self.server)
        values.update(overrides)
        os.environ.update(values)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))


# --------------------------------------------------------------------------- #
# Configuration and HTTPS policy
# --------------------------------------------------------------------------- #

class TestSettings(unittest.TestCase):

    def _settings(self, **env):
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            from app.config import Settings
            return Settings()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_missing_base_url_is_reported(self):
        settings = self._settings(PUBLIC_BASE_URL="")
        self.assertIn("PUBLIC_BASE_URL is not set", settings.base_url_problem())

    def test_http_base_url_is_rejected(self):
        settings = self._settings(PUBLIC_BASE_URL="http://busrapay.com")
        problem = settings.base_url_problem()
        self.assertIn("not https", problem)
        self.assertFalse(settings.https_ready())

    def test_https_base_url_is_accepted(self):
        settings = self._settings(PUBLIC_BASE_URL="https://busrapay.com")
        self.assertIsNone(settings.base_url_problem())
        self.assertTrue(settings.https_ready())

    def test_url_for_joins_without_double_slash(self):
        settings = self._settings(PUBLIC_BASE_URL="https://busrapay.com/")
        self.assertEqual(settings.url_for("/return/geidea"),
                         "https://busrapay.com/return/geidea")


# --------------------------------------------------------------------------- #
# Adapter flows against the mock
# --------------------------------------------------------------------------- #

#: Expected number of *timed* calls per gateway and flow.
EXPECTED_CALLS = {
    "geidea": {"hosted_start": 1, "hosted_confirm": 1, "direct_api": 4,
               "capture": 1, "refund": 1, "void": 1, "mit": 2},
    "stripe": {"hosted_start": 1, "hosted_confirm": 1, "direct_api": 2,
               "capture": 1, "refund": 1, "void": 1, "mit": 1},
    "adyen": {"hosted_start": 1, "hosted_confirm": 0, "direct_api": 2,
              "capture": 1, "refund": 1, "void": 1, "mit": 1},
    "checkout_com": {"hosted_start": 1, "hosted_confirm": 1, "direct_api": 1,
                     "capture": 1, "refund": 1, "void": 1, "mit": 1},
    "hyperpay": {"hosted_start": 1, "hosted_confirm": 1, "direct_api": 1,
                 "capture": 1, "refund": 1, "void": 1, "mit": 1},
    "moyasar": {"hosted_start": 1, "hosted_confirm": 1, "direct_api": 3,
                "capture": 1, "refund": 1, "void": 1, "mit": 1},
}

STANDALONE = ("direct_api", "capture", "refund", "void", "mit")


class TestAdapterFlows(EnvMixin, unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = MockGatewayServer()
        cls.server.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def _charge(self, gateway: str):
        from app.adapters.base import Charge
        from app.timing import new_reference
        return Charge(amount=10.0, currency="SAR", reference=new_reference("test"),
                      return_url=f"https://busrapay.test/return/{gateway}",
                      webhook_url=f"https://busrapay.test/webhook/{gateway}")

    def _card(self):
        from app.adapters.base import Card
        return Card(number="4111111111111111", month="12", year="2030", cvc="123")

    def _adapter(self, name: str):
        from app.adapters import get_adapter
        adapter = get_adapter(name)
        self.assertTrue(adapter.configured(),
                        f"{name} should be configured under the mock env")
        return adapter

    # -- hosted checkout, both legs -------------------------------------- #

    def _check_hosted(self, name: str):
        self.use_env()
        adapter = self._adapter(name)
        charge = self._charge(name)

        with adapter.session_for("hosted_checkout") as session:
            started = adapter.start_hosted(session, charge)
            self.assertTrue(started.redirect_url,
                            f"{name}: start_hosted returned no redirect target")
            self.assertTrue(started.gateway_ref)
            self.assertEqual(session.call_count, EXPECTED_CALLS[name]["hosted_start"],
                             f"{name}: unexpected timed call count on the start leg")
            context = started.context

        with adapter.session_for("hosted_checkout") as session:
            outcome = adapter.confirm_hosted(session, charge, context, {})
            self.assertIn(outcome.status, ("succeeded", "pending"),
                          f"{name}: confirm returned {outcome.status}: {outcome.detail}")
            self.assertEqual(session.call_count, EXPECTED_CALLS[name]["hosted_confirm"],
                             f"{name}: unexpected timed call count on the confirm leg")

    def _check_standalone(self, name: str, flow: str):
        self.use_env()
        adapter = self._adapter(name)
        with adapter.session_for(flow) as session:
            outcome = adapter.run_standalone(flow, session, self._charge(name), self._card())
        self.assertIn(outcome.status, ("succeeded", "pending"),
                      f"{name}/{flow} returned {outcome.status}: {outcome.detail}")
        self.assertEqual(session.call_count, EXPECTED_CALLS[name][flow],
                         f"{name}/{flow} made {session.call_count} timed calls, "
                         f"expected {EXPECTED_CALLS[name][flow]}")
        self.assertGreater(session.total_ms, 0)

    def test_geidea_hosted(self):
        self._check_hosted("geidea")

    def test_geidea_standalone(self):
        for flow in STANDALONE:
            with self.subTest(flow=flow):
                self._check_standalone("geidea", flow)

    def test_stripe_hosted(self):
        self._check_hosted("stripe")

    def test_stripe_standalone(self):
        for flow in STANDALONE:
            with self.subTest(flow=flow):
                self._check_standalone("stripe", flow)

    def test_adyen_hosted(self):
        self._check_hosted("adyen")

    def test_adyen_standalone(self):
        for flow in STANDALONE:
            with self.subTest(flow=flow):
                self._check_standalone("adyen", flow)

    def test_checkout_hosted(self):
        self._check_hosted("checkout_com")

    def test_checkout_standalone(self):
        for flow in STANDALONE:
            with self.subTest(flow=flow):
                self._check_standalone("checkout_com", flow)

    def test_hyperpay_hosted(self):
        self._check_hosted("hyperpay")

    def test_hyperpay_standalone(self):
        for flow in STANDALONE:
            with self.subTest(flow=flow):
                self._check_standalone("hyperpay", flow)

    def test_moyasar_hosted(self):
        self._check_hosted("moyasar")

    def test_moyasar_standalone(self):
        for flow in STANDALONE:
            with self.subTest(flow=flow):
                self._check_standalone("moyasar", flow)

    # -- Geidea specifics ------------------------------------------------- #

    def test_geidea_direct_drops_to_three_calls_when_frictionless(self):
        """The benchmark's headline question, exercised.

        The mock's initiate response says "Enrolled", so the default run makes all
        four calls. When it reports a card that is not enrolled, the adapter must
        complete in three — proving it measures the real sequence rather than
        asserting the documented one.
        """
        RESPONSE_OVERRIDES["geidea_initiate"] = (
            200, {"responseCode": "000", "enrollmentStatus": "Not Enrolled"})
        self.addCleanup(RESPONSE_OVERRIDES.pop, "geidea_initiate", None)
        self.use_env()

        adapter = self._adapter("geidea")
        with adapter.session_for("direct_api") as session:
            outcome = adapter.direct(session, self._charge("geidea"), self._card())
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(session.call_count, 3)

    def test_geidea_force_full_3ds_keeps_four_calls(self):
        self.use_env(GEIDEA_FORCE_FULL_3DS="1")
        adapter = self._adapter("geidea")
        with adapter.session_for("direct_api") as session:
            adapter.direct(session, self._charge("geidea"), self._card())
        self.assertEqual(session.call_count, 4)

    def test_geidea_region_selects_host(self):
        for region, host in (("ksa", "api.ksamerchant.geidea.net"),
                             ("eg", "api.merchant.geidea.net"),
                             ("uae", "api.geidea.ae")):
            with self.subTest(region=region):
                self.use_env(GEIDEA_REGION=region, GEIDEA_API_BASE="")
                self.assertIn(host, self._adapter("geidea").base)

    def test_geidea_signature_is_documented_hmac(self):
        import base64
        import hashlib
        import hmac

        self.use_env()
        signature = self._adapter("geidea").sign(
            amount=10.0, currency="SAR", reference="ref1", timestamp="2026-08-19T10:00:00")
        expected = base64.b64encode(hmac.new(
            b"pw_test", b"pk_test10.00SARref12026-08-19T10:00:00",
            hashlib.sha256).digest()).decode()
        self.assertEqual(signature, expected)

    def test_geidea_reports_gateway_response_code_failures(self):
        from app.timing import GatewayError

        RESPONSE_OVERRIDES["geidea_session"] = (
            200, {"responseCode": "100", "responseMessage": "Invalid signature",
                  "detailedResponseMessage": "signature mismatch"})
        self.addCleanup(RESPONSE_OVERRIDES.pop, "geidea_session", None)
        self.use_env()

        adapter = self._adapter("geidea")
        with adapter.session_for("direct_api") as session:
            with self.assertRaises(GatewayError) as caught:
                adapter.direct(session, self._charge("geidea"), self._card())
        # A 200 carrying a non-000 responseCode must not read as success.
        self.assertIn("Invalid signature", str(caught.exception))

    def test_geidea_mit_without_token_says_what_is_missing(self):
        from app.timing import NotConfigured

        self.use_env(GEIDEA_TOKEN_ID="")
        adapter = self._adapter("geidea")
        with adapter.session_for("mit") as session:
            with self.assertRaises(NotConfigured) as caught:
                adapter.mit(session, self._charge("geidea"), self._card())
        self.assertIn("GEIDEA_TOKEN_ID", str(caught.exception))

    def test_prep_calls_are_excluded_from_the_count(self):
        """Refund issues four HTTP calls but must report one."""
        self.use_env()
        adapter = self._adapter("geidea")
        with adapter.session_for("refund") as session:
            adapter.refund(session, self._charge("geidea"), self._card())
        self.assertEqual(len(session.calls), 5)   # 4 setup + 1 measured
        self.assertEqual(session.call_count, 1)
        self.assertEqual(session.timed_calls[0].name, "POST /pgw/api/v2/direct/refund")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class TestWebRoutes(EnvMixin, unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = MockGatewayServer()
        cls.server.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def client(self, **env):
        from fastapi.testclient import TestClient
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.use_env(DATABASE_PATH=path, **env)

        # Re-import so module-level SETTINGS and the store pick up this env.
        for module in [m for m in list(sys.modules) if m.startswith("app.")]:
            del sys.modules[module]
        import app.main
        # https base URL: the app redirects plaintext to https, so a http:// test
        # client would only ever see 307s. This exercises the real path.
        return TestClient(app.main.app, base_url="https://testserver"), app.main

    def test_checkout_page_renders(self):
        client, _ = self.client()
        response = client.get("/checkout")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Run a checkout", response.text)
        self.assertIn("Geidea", response.text)

    def test_security_headers_are_set(self):
        client, _ = self.client()
        response = client.get("/checkout")
        self.assertIn("max-age=", response.headers["strict-transport-security"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_direct_flow_records_an_attempt(self):
        client, main = self.client()
        response = client.post("/checkout", data={
            "gateway": "geidea", "flow": "direct_api",
            "amount": "10.00", "currency": "SAR"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Succeeded", response.text)

        attempts = main.store.recent_attempts(5)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["gateway"], "geidea")
        self.assertEqual(attempts[0]["call_count"], 4)
        self.assertGreater(attempts[0]["total_ms"], 0)

    def test_hosted_flow_redirects_and_confirm_completes_the_attempt(self):
        client, main = self.client()
        response = client.post("/checkout", data={
            "gateway": "geidea", "flow": "hosted_checkout",
            "amount": "10.00", "currency": "SAR"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("gateway.example/hosted/", response.headers["location"])

        attempts = main.store.recent_attempts(5)
        self.assertEqual(attempts[0]["status"], "pending")
        self.assertEqual(attempts[0]["call_count"], 1)

        attempt_id = attempts[0]["id"]
        confirm = client.get(f"/return/geidea?attempt={attempt_id}")
        self.assertEqual(confirm.status_code, 200)

        finished = main.store.get_attempt(attempt_id)
        self.assertEqual(finished["status"], "succeeded")
        # Both legs are counted together: 1 create + 1 confirm.
        self.assertEqual(finished["call_count"], 2)

    def test_hosted_flow_refused_without_https_base_url(self):
        client, main = self.client(PUBLIC_BASE_URL="http://busrapay.test")
        response = client.post("/checkout", data={
            "gateway": "geidea", "flow": "hosted_checkout",
            "amount": "10.00", "currency": "SAR"}, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("not https", response.text)
        # Nothing should have been sent to the gateway.
        self.assertEqual(main.store.recent_attempts(5), [])

    def test_unconfigured_gateway_is_explained_not_crashed(self):
        client, _ = self.client(GEIDEA_PUBLIC_KEY="", GEIDEA_API_PASSWORD="")
        response = client.post("/checkout", data={
            "gateway": "geidea", "flow": "direct_api",
            "amount": "10.00", "currency": "SAR"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("GEIDEA_PUBLIC_KEY", response.text)

    def test_unknown_gateway_is_rejected(self):
        client, _ = self.client()
        response = client.post("/checkout", data={
            "gateway": "nope", "flow": "direct_api",
            "amount": "10.00", "currency": "SAR"})
        self.assertEqual(response.status_code, 400)

    def test_results_page_and_summary_api(self):
        client, _ = self.client()
        client.post("/checkout", data={"gateway": "geidea", "flow": "capture",
                                       "amount": "10.00", "currency": "SAR"})
        self.assertEqual(client.get("/results").status_code, 200)
        summary = client.get("/api/summary").json()["summary"]
        self.assertEqual(summary[0]["gateway"], "geidea")
        self.assertEqual(summary[0]["call_count"], 1)

    def test_webhook_is_acknowledged(self):
        client, _ = self.client()
        response = client.post("/webhook/geidea", json={"event": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["received"])

    def test_return_without_attempt_id_is_explained(self):
        client, _ = self.client()
        response = client.get("/return/geidea")
        self.assertEqual(response.status_code, 200)
        self.assertIn("without an attempt id", response.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
