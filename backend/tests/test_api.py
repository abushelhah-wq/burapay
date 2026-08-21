"""
End-to-end API tests.

These drive the real application through its HTTP surface, against MockPay so the
whole lifecycle — start a transaction, record measurements, aggregate, rank, export —
can be exercised without a sandbox credential.
"""

from __future__ import annotations

import json

from httpx import AsyncClient


async def configure_mockpay(client: AsyncClient, headers: dict, *, scenario: str = "success",
                            latency_ms: str = "5") -> None:
    response = await client.put("/v1/gateways/mockpay/credentials", headers=headers, json={
        "environment": "sandbox",
        "values": {"scenario": scenario, "latency_ms": latency_ms, "jitter_ms": "1"}})
    assert response.status_code == 200, response.text


async def run_transaction(client: AsyncClient, headers: dict, **overrides) -> dict:
    payload = {"gateway_code": "mockpay", "integration_type": "direct",
               "amount": 1.00, "currency": "SAR"}
    payload.update(overrides)
    response = await client.post("/v1/transactions/start", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


class TestHealth:
    async def test_health_reports_the_database_and_test_mode(self, client: AsyncClient):
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["database"] == "connected"
        assert body["version"]
        # Section 26: production must be off unless explicitly unlocked.
        assert body["test_mode"] is True


class TestAuthentication:
    async def test_login_returns_a_token_and_the_user(self, client: AsyncClient):
        response = await client.post("/v1/auth/login", json={
            "email": "admin@busrapay.com", "password": "TestAdminPassword123"})
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["user"]["role"] == "admin"

    async def test_wrong_password_is_rejected(self, client: AsyncClient):
        response = await client.post("/v1/auth/login", json={
            "email": "admin@busrapay.com", "password": "wrong"})
        assert response.status_code == 401

    async def test_unknown_and_wrong_password_are_indistinguishable(self, client: AsyncClient):
        """Different messages would tell an attacker which addresses exist."""
        unknown = await client.post("/v1/auth/login", json={
            "email": "nobody@busrapay.com", "password": "x"})
        wrong = await client.post("/v1/auth/login", json={
            "email": "admin@busrapay.com", "password": "x"})
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    async def test_protected_routes_require_authentication(self, client: AsyncClient):
        assert (await client.get("/v1/gateways")).status_code == 401
        assert (await client.get("/v1/transactions")).status_code == 401

    async def test_viewer_cannot_configure_credentials(self, client: AsyncClient,
                                                       auth_headers: dict):
        created = await client.post("/v1/auth/users", headers=auth_headers, json={
            "email": "viewer@busrapay.com", "password": "ViewerPassword123",
            "role": "viewer"})
        assert created.status_code == 201

        login = await client.post("/v1/auth/login", json={
            "email": "viewer@busrapay.com", "password": "ViewerPassword123"})
        viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # A viewer reads dashboards; only an admin holds credentials and runs tests.
        forbidden = await client.put("/v1/gateways/mockpay/credentials",
                                     headers=viewer_headers,
                                     json={"environment": "sandbox", "values": {}})
        assert forbidden.status_code == 403
        assert (await client.get("/v1/comparison/dashboard",
                                 headers=viewer_headers)).status_code == 200


class TestGateways:
    async def test_catalogue_lists_every_gateway_with_its_capabilities(
            self, client: AsyncClient, auth_headers: dict):
        response = await client.get("/v1/gateways", headers=auth_headers)
        assert response.status_code == 200
        codes = {row["code"] for row in response.json()}
        assert {"geidea", "stripe", "adyen", "checkout", "hyperpay", "moyasar",
                "mockpay"} <= codes

    async def test_unconfigured_gateways_are_labelled_not_hidden(
            self, client: AsyncClient, auth_headers: dict):
        rows = (await client.get("/v1/gateways", headers=auth_headers)).json()
        geidea = next(row for row in rows if row["code"] == "geidea")
        assert geidea["configured"] is False
        assert geidea["status"] == "Not Configured"
        assert "merchant_public_key" in geidea["missing_fields"]

    async def test_credential_form_is_declared_by_the_adapter(
            self, client: AsyncClient, auth_headers: dict):
        rows = (await client.get("/v1/gateways", headers=auth_headers)).json()
        geidea = next(row for row in rows if row["code"] == "geidea")
        keys = {field["key"] for field in geidea["credential_fields"]}
        assert {"merchant_public_key", "api_password", "api_base"} <= keys
        secret = next(f for f in geidea["credential_fields"]
                      if f["key"] == "merchant_public_key")
        assert secret["secret"] is True


class TestCredentialSecurity:
    async def test_secrets_are_returned_masked_only(self, client: AsyncClient,
                                                    auth_headers: dict):
        await client.put("/v1/gateways/stripe/credentials", headers=auth_headers, json={
            "environment": "sandbox",
            "values": {"secret_key": "sk_test_51ABCDEFGHIJKLMNX92D"}})

        response = await client.get("/v1/gateways/stripe/credentials", headers=auth_headers)
        assert response.status_code == 200
        stored = response.json()["values"]["secret_key"]
        # Section 5: never expose the secret; show the shape, not the value.
        assert "ABCDEFGHIJKLMN" not in stored
        assert stored.endswith("X92D") and "*" in stored

    async def test_no_endpoint_anywhere_returns_a_secret_in_full(
            self, client: AsyncClient, auth_headers: dict):
        secret = "sk_test_51ABCDEFGHIJKLMNX92D"
        await client.put("/v1/gateways/stripe/credentials", headers=auth_headers, json={
            "environment": "sandbox", "values": {"secret_key": secret}})
        for path in ("/v1/gateways", "/v1/gateways/stripe",
                     "/v1/gateways/stripe/credentials", "/v1/settings"):
            body = (await client.get(path, headers=auth_headers)).text
            assert secret not in body, f"{path} leaked a stored secret"

    async def test_saving_is_a_merge_so_untouched_secrets_survive(
            self, client: AsyncClient, auth_headers: dict):
        await client.put("/v1/gateways/stripe/credentials", headers=auth_headers, json={
            "environment": "sandbox",
            "values": {"secret_key": "sk_test_original_value_123456"}})
        # The UI sends the mask back for a field the user did not retype.
        masked = (await client.get("/v1/gateways/stripe/credentials",
                                   headers=auth_headers)).json()["values"]["secret_key"]
        await client.put("/v1/gateways/stripe/credentials", headers=auth_headers, json={
            "environment": "sandbox",
            "values": {"secret_key": masked, "api_version": "2024-06-20"}})

        status = (await client.get("/v1/gateways/stripe", headers=auth_headers)).json()
        assert status["configured"] is True     # the original secret was not destroyed

    async def test_credentials_can_be_deleted(self, client: AsyncClient, auth_headers: dict):
        await client.put("/v1/gateways/stripe/credentials", headers=auth_headers, json={
            "environment": "sandbox", "values": {"secret_key": "sk_test_x"}})
        response = await client.delete("/v1/gateways/stripe/credentials?environment=sandbox",
                                       headers=auth_headers)
        assert response.status_code == 200
        after = (await client.get("/v1/gateways/stripe", headers=auth_headers)).json()
        assert after["configured"] is False


class TestTransactionLifecycle:
    async def test_direct_transaction_records_measurements_and_a_timeline(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        assert started["status"] == "SUCCESS"

        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        transaction = detail["transaction"]
        assert transaction["api_call_count"] == 2
        assert transaction["gateway_api_time_ms"] > 0
        assert transaction["total_duration_ms"] >= transaction["gateway_api_time_ms"]
        assert len(detail["measurements"]) == 2
        assert {"BENCHMARK_STARTED", "FINAL_STATUS_CONFIRMED"} <= {
            event["event_type"] for event in detail["events"]}

    async def test_application_overhead_is_recorded_separately(
            self, client: AsyncClient, auth_headers: dict):
        """Section 51: the platform's own cost must never be charged to a gateway."""
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        transaction = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                        headers=auth_headers)).json()["transaction"]
        assert transaction["app_overhead_ms"] is not None
        assert transaction["app_overhead_ms"] >= 0

    async def test_declines_are_recorded_with_the_gateway_code(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers, scenario="decline")
        started = await run_transaction(client, auth_headers)
        assert started["status"] == "DECLINED"
        transaction = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                        headers=auth_headers)).json()["transaction"]
        assert transaction["error_category"] == "gateway_decline"
        assert transaction["gateway_response_code"] == "05"

    async def test_timeouts_are_recorded_as_timeouts(self, client: AsyncClient,
                                                     auth_headers: dict):
        await configure_mockpay(client, auth_headers, scenario="timeout")
        started = await run_transaction(client, auth_headers)
        assert started["status"] == "TIMEOUT"

    async def test_gateway_500_is_categorised_as_a_gateway_error(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers, scenario="server_error")
        started = await run_transaction(client, auth_headers)
        transaction = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                        headers=auth_headers)).json()["transaction"]
        assert transaction["error_category"] == "gateway_5xx"

    async def test_hpp_returns_a_redirect_and_stays_pending(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        # The customer's time on the hosted page is theirs; nothing is final yet.
        assert started["status"] == "PENDING"
        assert started["redirect_url"]

    async def test_hpp_return_leg_completes_the_transaction(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        transaction_id = started["transaction_id"]

        response = await client.get(f"/v1/transactions/{transaction_id}/return",
                                    follow_redirects=False)
        assert response.status_code == 303

        transaction = (await client.get(f"/v1/transactions/{transaction_id}",
                                        headers=auth_headers)).json()["transaction"]
        assert transaction["status"] == "SUCCESS"
        assert transaction["total_duration_ms"] > 0
        # Both legs' calls count toward the gateway's API time.
        assert transaction["api_call_count"] == 2

    async def test_unconfigured_gateway_is_refused_with_an_explanation(
            self, client: AsyncClient, auth_headers: dict):
        response = await client.post("/v1/transactions/start", headers=auth_headers, json={
            "gateway_code": "geidea", "integration_type": "direct",
            "amount": 1.0, "currency": "SAR"})
        assert response.status_code == 400
        assert "not configured" in response.json()["message"].lower()

    async def test_typing_a_card_is_refused_until_it_is_turned_on(
            self, client: AsyncClient, auth_headers: dict):
        """Section 25: a PAN typed into this application is a deliberate choice."""
        await configure_mockpay(client, auth_headers)
        response = await client.post("/v1/transactions/start", headers=auth_headers, json={
            "gateway_code": "mockpay", "integration_type": "direct",
            "amount": 1.0, "currency": "SAR",
            "card": {"number": "4111 1111 1111 1111", "month": "12", "year": "30",
                     "cvc": "123"}})
        assert response.status_code == 400
        assert "ALLOW_DIRECT_CARD_ENTRY" in response.json()["message"]

    async def test_a_card_is_accepted_once_it_is_turned_on(
            self, client: AsyncClient, auth_headers: dict, monkeypatch):
        from app.core.config import settings as app_settings
        monkeypatch.setattr(app_settings, "allow_direct_card_entry", True)
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(
            client, auth_headers,
            card={"number": "4111111111111111", "month": "12", "year": "2030",
                  "cvc": "123", "holder": "Test Person"})

        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        # Whatever else happens, the card must not be anywhere in what was stored.
        assert "4111111111111111" not in json.dumps(detail)
        assert "123" not in json.dumps(detail.get("transaction", {}).get("context") or {})

    async def test_a_card_sent_with_hosted_checkout_is_refused_not_ignored(
            self, client: AsyncClient, auth_headers: dict, monkeypatch):
        """The provider's page collects it. Accepting one here would be card data for nothing."""
        from app.core.config import settings as app_settings
        monkeypatch.setattr(app_settings, "allow_direct_card_entry", True)
        await configure_mockpay(client, auth_headers)
        response = await client.post("/v1/transactions/start", headers=auth_headers, json={
            "gateway_code": "mockpay", "integration_type": "hpp",
            "amount": 1.0, "currency": "SAR",
            "card": {"number": "4111111111111111", "month": "12", "year": "30",
                     "cvc": "123"}})
        assert response.status_code == 400
        assert "provider's own page" in response.json()["message"]

    async def test_a_token_id_needs_no_permission(self, client: AsyncClient,
                                                  auth_headers: dict):
        """A token is not card data, so it works without turning card entry on."""
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, token_id="tok_abc")
        assert started["status"] in ("SUCCESS", "PENDING")

    async def test_the_transaction_page_carries_the_token_and_pause_fields(
            self, client: AsyncClient, auth_headers: dict):
        """The response model has to declare them, or they are filtered out on the way past."""
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        assert "stored_token" in detail
        assert "stored_token_hint" in detail
        assert detail["awaiting_customer_action"] is False

    async def test_a_3ds_challenge_pauses_the_payment_and_resumes_on_return(
            self, client: AsyncClient, auth_headers: dict):
        """The whole two-legged Direct flow: pause for the cardholder, then finish.

        What matters is where the waiting lands. The customer's time must show up as
        customer interaction and must not be added to the gateway's API time.
        """
        await configure_mockpay(client, auth_headers, scenario="three_ds_challenge")
        started = await run_transaction(client, auth_headers)
        assert started["status"] == "PENDING"
        assert started["requires_customer_action"] is True
        assert started["redirect_url"]

        paused = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        assert paused["awaiting_customer_action"] is True
        assert paused["transaction"]["total_duration_ms"] is None

        leg1_api_ms = paused["transaction"]["gateway_api_time_ms"]

        # The issuer sends the browser back. That leg is unauthenticated by design.
        response = await client.get(
            f"/v1/transactions/{started['transaction_id']}/return",
            follow_redirects=False)
        assert response.status_code == 303

        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        transaction = detail["transaction"]
        assert transaction["status"] == "SUCCESS"
        assert transaction["total_duration_ms"] is not None
        # Both legs' calls count toward the gateway; the wait between them does not.
        assert transaction["gateway_api_time_ms"] > leg1_api_ms
        assert (transaction["gateway_api_time_ms"]
                < transaction["total_duration_ms"])
        assert transaction["api_call_count"] == 3
        # The pause is over: nothing should keep offering a way back into it.
        assert detail["awaiting_customer_action"] is False

    async def test_the_challenge_document_is_served_in_its_own_opaque_origin(
            self, client: AsyncClient, auth_headers: dict):
        """The issuer's markup must not get this application's origin, or its frame.

        Framing the challenge was the bug: the issuer ends by returning the browser to
        our return URL, and inside a frame that return is refused by our own
        X-Frame-Options. At the top level it is an ordinary navigation — but then the
        markup would be running on our host, so CSP sandbox puts it in an opaque origin
        instead.
        """
        from app.db.session import get_sessionmaker
        from app.models import Transaction

        await configure_mockpay(client, auth_headers, scenario="three_ds_challenge")
        started = await run_transaction(client, auth_headers)

        # Give it a form challenge, which is the case that needs a document of its own.
        async with get_sessionmaker()() as session:
            transaction = await session.get(Transaction, started["transaction_id"])
            context = dict(transaction.context or {})
            context["adapter_context"] = {
                "challenge_html": "<form action='https://issuer.test/acs'></form>"}
            transaction.context = context
            await session.commit()

        response = await client.get(
            f"/v1/transactions/{started['transaction_id']}/three-ds/challenge")
        assert response.status_code == 200
        assert "issuer.test" in response.text

        # A form of hidden inputs renders as nothing. The document has to submit it,
        # and has to say something while it does — a blank page is what this fixed.
        assert "<!doctype html>" in response.text.lower()
        assert "Taking you to your bank" in response.text
        assert "form.submit()" in response.text

        csp = response.headers["content-security-policy"]
        assert csp.startswith("sandbox ")
        # allow-same-origin would hand the issuer's markup our cookies and storage.
        assert "allow-same-origin" not in csp
        # ...and without this the issuer cannot send the cardholder back to us.
        assert "allow-top-navigation" in csp
        assert response.headers["cache-control"] == "no-store"

    async def test_a_challenge_with_no_form_says_so_instead_of_showing_nothing(
            self, client: AsyncClient, auth_headers: dict):
        """A gateway that sends something unexpected is a fact worth seeing."""
        from app.db.session import get_sessionmaker
        from app.models import Transaction

        await configure_mockpay(client, auth_headers, scenario="three_ds_challenge")
        started = await run_transaction(client, auth_headers)
        async with get_sessionmaker()() as session:
            transaction = await session.get(Transaction, started["transaction_id"])
            context = dict(transaction.context or {})
            context["adapter_context"] = {"challenge_html": "<p>nothing useful</p>"}
            transaction.context = context
            await session.commit()

        response = await client.get(
            f"/v1/transactions/{started['transaction_id']}/three-ds/challenge")
        assert response.status_code == 200
        assert "No challenge to show" in response.text
        # Shown escaped, not injected: it is not markup we are willing to run.
        assert "&lt;p&gt;nothing useful&lt;/p&gt;" in response.text

    async def test_the_challenge_document_refuses_when_nothing_is_waiting(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        response = await client.get(
            f"/v1/transactions/{started['transaction_id']}/three-ds/challenge")
        assert response.status_code == 400

    async def test_the_3ds_endpoint_refuses_a_transaction_that_is_not_waiting(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        response = await client.get(
            f"/v1/transactions/{started['transaction_id']}/three-ds", headers=auth_headers)
        assert response.status_code == 400
        assert "not waiting" in response.json()["message"].lower()

    async def test_unsupported_currency_is_refused(self, client: AsyncClient,
                                                   auth_headers: dict):
        await client.put("/v1/gateways/moyasar/credentials", headers=auth_headers, json={
            "environment": "sandbox", "values": {"secret_key": "sk_test_x"}})
        response = await client.post("/v1/transactions/start", headers=auth_headers, json={
            "gateway_code": "moyasar", "integration_type": "direct",
            "amount": 1.0, "currency": "GBP"})
        assert response.status_code == 400
        assert "GBP" in response.json()["message"]

    async def test_production_environment_is_refused_by_default(
            self, client: AsyncClient, auth_headers: dict):
        """Section 26: production needs a deliberate configuration change."""
        await configure_mockpay(client, auth_headers)
        response = await client.post("/v1/transactions/start", headers=auth_headers, json={
            "gateway_code": "mockpay", "integration_type": "direct", "amount": 1.0,
            "currency": "SAR", "environment": "production"})
        assert response.status_code == 400
        assert "sandbox" in response.json()["message"].lower()

    async def test_browser_metrics_are_stored_separately(self, client: AsyncClient,
                                                         auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        response = await client.post(
            f"/v1/transactions/{started['transaction_id']}/browser-metrics",
            json={"metrics": {"dns_ms": 12.5, "ttfb_ms": 210.0},
                  "origin_scope": "cross-origin", "page_load_time_ms": 850.0})
        assert response.status_code == 200

        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        names = {m["metric_name"] for m in detail["browser_measurements"]}
        assert names == {"dns_ms", "ttfb_ms"}
        assert detail["transaction"]["page_load_time_ms"] == 850.0
        # Browser numbers must never be folded into the gateway's API time.
        assert detail["transaction"]["gateway_api_time_ms"] < 850.0


class TestFiltersAndSearch:
    async def test_transactions_can_be_filtered(self, client: AsyncClient,
                                                auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers, currency="SAR")
        await run_transaction(client, auth_headers, currency="USD")

        both = (await client.get("/v1/transactions", headers=auth_headers)).json()
        assert both["total"] == 2

        sar = (await client.get("/v1/transactions?currency=SAR",
                                headers=auth_headers)).json()
        assert sar["total"] == 1
        assert sar["items"][0]["currency"] == "SAR"

    async def test_search_by_reference(self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, reference="order-abc-123")
        found = (await client.get("/v1/transactions?merchant_reference=abc",
                                  headers=auth_headers)).json()
        assert found["total"] == 1
        assert found["items"][0]["id"] == started["transaction_id"]


class TestBenchmarkRuns:
    async def test_run_executes_and_aggregates(self, client: AsyncClient,
                                               auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        created = await client.post("/v1/benchmarks/runs", headers=auth_headers, json={
            "name": "MockPay direct", "gateway_code": "mockpay",
            "integration_type": "direct", "transaction_count": 3,
            "amount": 1.0, "currency": "SAR", "interval_seconds": 0})
        assert created.status_code == 201
        run_id = created.json()["id"]

        import asyncio
        for _ in range(100):
            detail = (await client.get(f"/v1/benchmarks/runs/{run_id}",
                                       headers=auth_headers)).json()
            if detail["status"] in ("COMPLETED", "FAILED"):
                break
            await asyncio.sleep(0.05)

        assert detail["status"] == "COMPLETED"
        assert detail["progress"]["completed"] == 3
        assert detail["statistics"]["api"]["count"] == 3

    async def test_rate_limits_are_enforced_not_advisory(self, client: AsyncClient,
                                                         auth_headers: dict):
        """Section 16: a request for more than the maximum is clamped, not honoured."""
        await client.put("/v1/settings/benchmark_limits", headers=auth_headers, json={
            "min_interval_seconds": 1.5, "max_transactions_per_run": 5,
            "max_concurrency": 1})
        created = await client.post("/v1/benchmarks/runs", headers=auth_headers, json={
            "name": "too big", "gateway_code": "mockpay", "integration_type": "direct",
            "transaction_count": 900, "amount": 1.0, "currency": "SAR",
            "interval_seconds": 0})
        assert created.status_code == 201
        body = created.json()
        assert body["transaction_count"] == 5
        assert body["interval_seconds"] == 1.5
        # What was asked for is kept alongside what was applied, so the run is
        # reproducible from its own record.
        assert body["run_metadata"]["requested_transaction_count"] == 900

        await client.post(f"/v1/benchmarks/runs/{body['id']}/cancel", headers=auth_headers)

    async def test_run_metadata_records_the_environment(self, client: AsyncClient,
                                                        auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        created = await client.post("/v1/benchmarks/runs", headers=auth_headers, json={
            "name": "metadata check", "gateway_code": "mockpay",
            "integration_type": "direct", "transaction_count": 1, "amount": 1.0,
            "currency": "SAR", "interval_seconds": 0})
        metadata = created.json()["run_metadata"]
        # Section 42: enough to tell whether two runs are comparable.
        assert metadata["app_version"] and metadata["python_version"]
        assert "hostname" in metadata

    async def test_comparison_test_creates_one_run_per_gateway(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await client.put("/v1/gateways/stripe/credentials", headers=auth_headers, json={
            "environment": "sandbox", "values": {"secret_key": "sk_test_x"}})
        created = await client.post("/v1/benchmarks/comparison-tests", headers=auth_headers,
                                    json={"name": "Direct API comparison - SAR",
                                          "gateway_codes": ["mockpay", "stripe"],
                                          "integration_type": "direct",
                                          "transactions_per_gateway": 1, "amount": 1.0,
                                          "currency": "SAR", "interval_seconds": 0})
        assert created.status_code == 201
        detail = (await client.get(f"/v1/benchmarks/comparison-tests/{created.json()['id']}",
                                   headers=auth_headers)).json()
        assert len(detail["runs"]) == 2

    async def test_a_comparison_test_needs_at_least_two_gateways(
            self, client: AsyncClient, auth_headers: dict):
        response = await client.post("/v1/benchmarks/comparison-tests", headers=auth_headers,
                                     json={"name": "x", "gateway_codes": ["mockpay"],
                                           "integration_type": "direct",
                                           "transactions_per_gateway": 1, "amount": 1.0,
                                           "currency": "SAR"})
        assert response.status_code == 422


class TestComparisonAndReports:
    async def test_comparison_reports_the_full_statistical_set(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        for _ in range(3):
            await run_transaction(client, auth_headers)

        body = (await client.get("/v1/comparison", headers=auth_headers)).json()
        row = body["rows"][0]
        # Section 13: never an average on its own.
        for key in ("count", "min", "max", "mean", "median", "p50", "p90", "p95",
                    "p99", "stdev"):
            assert key in row["api"]
        assert row["api"]["count"] == 3
        assert row["success_rate"] == 100.0

    async def test_gateway_api_time_and_total_are_reported_separately(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        row = (await client.get("/v1/comparison", headers=auth_headers)).json()["rows"][0]
        assert row["api"]["mean"] is not None and row["total"]["mean"] is not None
        assert row["total"]["mean"] >= row["api"]["mean"]

    async def test_ranking_refuses_to_rank_thin_data(self, client: AsyncClient,
                                                     auth_headers: dict):
        """Section 60: one transaction is not evidence that a gateway is better."""
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        ranking = (await client.get("/v1/comparison/ranking", headers=auth_headers)).json()
        assert ranking["entries"] == []
        assert "Not enough" in ranking["note"]

    async def test_the_score_is_labelled_as_internal(self, client: AsyncClient,
                                                     auth_headers: dict):
        ranking = (await client.get("/v1/comparison/ranking", headers=auth_headers)).json()
        assert ranking["label"] == "Internal Benchmark Score"

    async def test_dashboard_summarises_without_claiming_a_winner_too_early(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        body = (await client.get("/v1/comparison/dashboard", headers=auth_headers)).json()
        summary = body["summary"]
        assert summary["total_transactions"] == 1
        assert summary["fastest_gateway"] is None
        assert "comparable transactions" in summary["fastest_gateway_note"]

    async def test_api_breakdown_keeps_the_gateway_operation_names(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        rows = (await client.get("/v1/reports/api-breakdown",
                                 headers=auth_headers)).json()["rows"]
        assert rows
        # Section 38: the real operation and its normalized category, side by side.
        assert all(row["operation_name"] and row["normalized_operation"] for row in rows)

    async def test_timeseries_buckets_results(self, client: AsyncClient,
                                              auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        body = (await client.get("/v1/comparison/timeseries?bucket=hour",
                                 headers=auth_headers)).json()
        assert body["bucket"] == "hour" and len(body["series"]) == 1


class TestExports:
    async def test_csv_export(self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        response = await client.get("/v1/reports/export?format=csv&dataset=transactions",
                                    headers=auth_headers)
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "gateway_api_time_ms" in response.text

    async def test_json_export_carries_its_filters(self, client: AsyncClient,
                                                   auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        response = await client.get(
            "/v1/reports/export?format=json&dataset=all&currency=SAR", headers=auth_headers)
        body = json.loads(response.content)
        assert body["filters"]["currency"] == "SAR"
        assert {"transactions", "summary", "api_breakdown"} == set(body["data"])

    async def test_excel_export(self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        response = await client.get("/v1/reports/export?format=excel&dataset=all",
                                    headers=auth_headers)
        assert response.status_code == 200
        assert response.content[:2] == b"PK"        # a zip container, i.e. a real xlsx

    async def test_csv_refuses_the_all_dataset_with_an_explanation(
            self, client: AsyncClient, auth_headers: dict):
        response = await client.get("/v1/reports/export?format=csv&dataset=all",
                                    headers=auth_headers)
        assert response.status_code == 400


class TestWebhooks:
    async def test_webhook_is_recorded_and_matched_to_its_transaction(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        reference = detail["transaction"]["merchant_reference"]

        response = await client.post("/webhooks/mockpay", json={
            "event": "mock.payment.completed", "reference": reference, "status": "paid"})
        assert response.status_code == 200
        assert response.json()["matched"] is True

        after = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                  headers=auth_headers)).json()["transaction"]
        # The arrival time is the measurement: it is what asynchronous completion
        # latency is computed from.
        assert after["webhook_received_at"] is not None
        assert after["webhook_latency_ms"] > 0

    async def test_unmatched_webhook_is_still_accepted(self, client: AsyncClient):
        response = await client.post("/webhooks/stripe", json={"type": "ping"})
        assert response.status_code == 200
        assert response.json()["matched"] is False

    async def test_unknown_gateway_webhook_does_not_error(self, client: AsyncClient):
        # A 200 keeps a retrying gateway from redelivering forever.
        assert (await client.post("/webhooks/nonsense", json={})).status_code == 200

    async def test_malformed_body_is_accepted_and_recorded(self, client: AsyncClient):
        response = await client.post("/webhooks/mockpay", content=b"not json",
                                     headers={"content-type": "application/json"})
        assert response.status_code == 200


class TestSettings:
    async def test_scoring_weights_are_normalised(self, client: AsyncClient,
                                                  auth_headers: dict):
        response = await client.put("/v1/settings/scoring_weights", headers=auth_headers,
                                    json={"api_response_time": 70, "p95_latency": 30,
                                          "transaction_completion": 0, "success_rate": 0})
        assert response.status_code == 200
        weights = response.json()["value"]
        assert abs(sum(weights.values()) - 1.0) < 0.001
        assert weights["api_response_time"] == 0.7

    async def test_the_rate_limit_floor_cannot_be_removed(self, client: AsyncClient,
                                                          auth_headers: dict):
        response = await client.put("/v1/settings/benchmark_limits", headers=auth_headers,
                                    json={"min_interval_seconds": 0,
                                          "max_transactions_per_run": 999999,
                                          "max_concurrency": 99})
        value = response.json()["value"]
        assert value["min_interval_seconds"] >= 0.25
        assert value["max_transactions_per_run"] <= 5000

    async def test_encryption_settings_are_not_editable_over_http(
            self, client: AsyncClient, auth_headers: dict):
        response = await client.put("/v1/settings/encryption_key", headers=auth_headers,
                                    json={"value": "x"})
        assert response.status_code == 400

    async def test_test_card_number_is_never_returned_in_full(self, client: AsyncClient,
                                                              auth_headers: dict):
        body = (await client.get("/v1/settings", headers=auth_headers)).json()
        card = body["settings"]["test_card"]
        assert card["number"].startswith("****")
        assert "cvc" not in card

    async def test_settings_expose_the_test_mode_banner_state(self, client: AsyncClient,
                                                              auth_headers: dict):
        body = (await client.get("/v1/settings", headers=auth_headers)).json()
        assert body["environment"]["test_mode"] is True


class TestTimelineIntegrity:
    async def test_the_return_event_is_recorded_once_at_its_real_offset(
            self, client: AsyncClient, auth_headers: dict):
        """A duplicate at offset zero would put the customer's return at the start."""
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        await client.get(f"/v1/transactions/{started['transaction_id']}/return",
                         follow_redirects=False)

        detail = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()
        returns = [event for event in detail["events"]
                   if event["event_type"] == "RETURN_URL_RECEIVED"]
        assert len(returns) == 1
        assert returns[0]["offset_ms"] > 0

    async def test_timeline_offsets_never_go_backwards(self, client: AsyncClient,
                                                       auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        events = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()["events"]
        offsets = [event["offset_ms"] for event in events]
        assert offsets == sorted(offsets)


class TestHppHandoff:
    async def test_handoff_returns_the_client_side_parameters(self, client: AsyncClient,
                                                              auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        response = await client.get(f"/v1/transactions/{started['transaction_id']}/hpp",
                                    headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["gateway_code"] == "mockpay"
        assert body["mode"] == "redirect"
        assert body["return_url"].endswith("/return")

    async def test_handoff_never_carries_a_credential(self, client: AsyncClient,
                                                      auth_headers: dict):
        """The widget parameters go to the browser, so they must hold nothing secret."""
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        body = (await client.get(f"/v1/transactions/{started['transaction_id']}/hpp",
                                 headers=auth_headers)).json()
        blob = json.dumps(body).lower()
        for forbidden in ("secret", "api_key", "password", "access_token"):
            assert forbidden not in blob

    async def test_direct_transactions_have_no_handoff(self, client: AsyncClient,
                                                       auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        response = await client.get(f"/v1/transactions/{started['transaction_id']}/hpp",
                                    headers=auth_headers)
        assert response.status_code == 400

    async def test_handoff_requires_authentication(self, client: AsyncClient,
                                                   auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers, integration_type="hpp")
        assert (await client.get(
            f"/v1/transactions/{started['transaction_id']}/hpp")).status_code == 401


class TestPaymentModes:
    """The admin-configured tokenisation flow, end to end through the API."""

    async def test_gateways_advertise_their_payment_modes(self, client: AsyncClient,
                                                          auth_headers: dict):
        rows = (await client.get("/v1/gateways", headers=auth_headers)).json()
        geidea = next(row for row in rows if row["code"] == "geidea")
        assert set(geidea["supported_payment_modes"]) == {"standard", "store_card", "token"}
        stripe = next(row for row in rows if row["code"] == "stripe")
        assert stripe["supported_payment_modes"] == ["standard"]

    async def test_the_agreement_fields_are_offered_on_the_settings_form(
            self, client: AsyncClient, auth_headers: dict):
        rows = (await client.get("/v1/gateways", headers=auth_headers)).json()
        geidea = next(row for row in rows if row["code"] == "geidea")
        fields = {f["key"]: f for f in geidea["credential_fields"]}
        assert {"agreement_id", "agreement_type", "token_id"} <= set(fields)
        # Optional: Geidea must still work for ordinary payments without them.
        assert fields["agreement_id"]["required"] is False
        assert fields["token_id"]["required"] is False
        assert fields["agreement_type"]["choices"]

    async def test_the_agreement_can_be_saved_and_is_reported_as_configured(
            self, client: AsyncClient, auth_headers: dict):
        await client.put("/v1/gateways/geidea/credentials", headers=auth_headers, json={
            "environment": "sandbox",
            "values": {"merchant_public_key": "pk_test", "api_password": "pw_test",
                       "agreement_id": "agr_1", "token_id": "tok_9"}})
        detail = (await client.get("/v1/gateways/geidea", headers=auth_headers)).json()
        assert detail["configured"] is True
        # Key names only — the UI uses these to know tokenisation is ready.
        assert {"agreement_id", "token_id"} <= set(detail["configured_fields"])
        assert "pk_test" not in (await client.get("/v1/gateways/geidea",
                                                  headers=auth_headers)).text

    async def test_a_mode_the_gateway_does_not_support_is_refused(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        response = await client.post("/v1/transactions/start", headers=auth_headers, json={
            "gateway_code": "mockpay", "integration_type": "direct", "amount": 1.0,
            "currency": "SAR", "payment_mode": "token"})
        assert response.status_code == 400
        assert "payment mode" in response.json()["message"].lower()

    async def test_transactions_record_and_filter_by_mode(self, client: AsyncClient,
                                                          auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        listed = (await client.get("/v1/transactions", headers=auth_headers)).json()
        assert listed["items"][0]["payment_mode"] == "standard"

        filtered = (await client.get("/v1/transactions?payment_mode=token",
                                     headers=auth_headers)).json()
        assert filtered["total"] == 0

    async def test_comparison_groups_by_mode(self, client: AsyncClient,
                                             auth_headers: dict):
        """A two-call token charge must never be averaged with a four-call one."""
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        rows = (await client.get("/v1/comparison?include_simulated=true",
                                 headers=auth_headers)).json()["rows"]
        assert all("payment_mode" in row for row in rows)
        assert rows[0]["payment_mode"] == "standard"

    async def test_benchmark_runs_carry_the_mode(self, client: AsyncClient,
                                                 auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        created = await client.post("/v1/benchmarks/runs", headers=auth_headers, json={
            "name": "mode run", "gateway_code": "mockpay", "integration_type": "direct",
            "transaction_count": 1, "amount": 1.0, "currency": "SAR",
            "interval_seconds": 0})
        assert created.json()["payment_mode"] == "standard"


class TestApiLog:
    """The call log: every request this platform made, and what came back."""

    async def test_the_log_carries_both_halves_of_every_call(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)

        response = await client.get("/v1/logs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 2
        entry = body["items"][0]
        assert entry["transaction_id"] == started["transaction_id"]
        assert entry["gateway_code"] == "mockpay"
        assert entry["duration_ms"] >= 0
        assert entry["request_snippet"] is not None
        assert entry["response_snippet"] is not None

    async def test_the_log_is_newest_first(self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        await run_transaction(client, auth_headers)
        items = (await client.get("/v1/logs", headers=auth_headers)).json()["items"]
        stamps = [item["started_at"] for item in items]
        assert stamps == sorted(stamps, reverse=True)

    async def test_filtering_by_outcome_and_operation(self, client: AsyncClient,
                                                      auth_headers: dict):
        await configure_mockpay(client, auth_headers, scenario="decline")
        await run_transaction(client, auth_headers)

        failed = (await client.get("/v1/logs?outcome=failed",
                                   headers=auth_headers)).json()
        assert all(item["success"] is False for item in failed["items"])

        authorizations = (await client.get(
            "/v1/logs?normalized_operation=AUTHORIZATION", headers=auth_headers)).json()
        assert authorizations["total"] >= 1
        assert all(item["normalized_operation"] == "AUTHORIZATION"
                   for item in authorizations["items"])

    async def test_searching_matches_the_endpoint(self, client: AsyncClient,
                                                  auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        found = (await client.get("/v1/logs?search=authorize",
                                  headers=auth_headers)).json()
        assert found["total"] >= 1
        assert all("authorize" in item["endpoint"].lower()
                   or "authorize" in item["operation_name"].lower()
                   for item in found["items"])

    async def test_the_summary_is_drawn_from_the_data_not_hard_coded(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        await run_transaction(client, auth_headers)
        summary = (await client.get("/v1/logs/summary", headers=auth_headers)).json()
        assert summary["total"] >= 2
        assert {"code": "mockpay", "calls": summary["gateways"][0]["calls"]} in [
            {"code": g["code"], "calls": g["calls"]} for g in summary["gateways"]]
        assert "AUTHORIZATION" in summary["operations"]

    async def test_the_log_never_holds_a_card_number(self, client: AsyncClient,
                                                     auth_headers: dict, monkeypatch):
        """Section 25 again, at the one place the whole payload is on display."""
        from app.core.config import settings as app_settings
        monkeypatch.setattr(app_settings, "allow_direct_card_entry", True)
        await configure_mockpay(client, auth_headers)
        await run_transaction(
            client, auth_headers,
            card={"number": "4111111111111111", "month": "12", "year": "2030",
                  "cvc": "123", "holder": "Test Person"})
        body = (await client.get("/v1/logs", headers=auth_headers)).text
        assert "4111111111111111" not in body

    async def test_the_log_needs_a_session(self, client: AsyncClient):
        assert (await client.get("/v1/logs")).status_code == 401

    async def test_clearing_the_log_is_admin_only_and_leaves_timings_alone(
            self, client: AsyncClient, auth_headers: dict):
        await configure_mockpay(client, auth_headers)
        started = await run_transaction(client, auth_headers)
        before = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                   headers=auth_headers)).json()["transaction"]

        cleared = await client.delete("/v1/logs", headers=auth_headers)
        assert cleared.status_code == 200
        assert (await client.get("/v1/logs", headers=auth_headers)).json()["total"] == 0

        after = (await client.get(f"/v1/transactions/{started['transaction_id']}",
                                  headers=auth_headers)).json()["transaction"]
        # The measurements are gone; the numbers already reported are not.
        assert after["gateway_api_time_ms"] == before["gateway_api_time_ms"]
        assert after["total_duration_ms"] == before["total_duration_ms"]
