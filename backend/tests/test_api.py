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
