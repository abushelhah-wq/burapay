"""
Unit tests for the measurement, security and statistics primitives.

These are the pieces the whole platform's credibility rests on: if the percentile
maths or the sanitizer is wrong, every number and every log line downstream is wrong
too.
"""

from __future__ import annotations

import pytest

from app.core.crypto import decrypt_dict, encrypt_dict, mask
from app.core.errors import ErrorCategory, GatewayError, category_for_http, normalize
from app.core.sanitizer import sanitize, sanitize_headers, scrub_text
from app.core.security import (PasswordPolicyError, create_access_token,
                               decode_access_token, hash_password, validate_password,
                               verify_password)
from app.models import UserRole, UserStatus, normalize_role
from app.core.stats import percentile, rates, summarize


class TestPercentiles:
    def test_nearest_rank_returns_an_observed_value(self):
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        # Nearest rank, not interpolated: every reported figure actually occurred.
        assert percentile(values, 50) == 50
        assert percentile(values, 90) == 90
        assert percentile(values, 95) == 100
        assert percentile(values, 99) == 100
        assert percentile(values, 100) == 100

    def test_single_value(self):
        assert percentile([42.0], 95) == 42.0

    def test_empty_is_none_not_zero(self):
        # Zero would read as "instant"; None reads as "not measured", which is true.
        assert percentile([], 95) is None

    def test_summary_has_the_full_statistical_set(self):
        stats = summarize([100, 200, 300, 400, 500])
        assert stats["count"] == 5
        assert stats["min"] == 100 and stats["max"] == 500
        assert stats["mean"] == 300 and stats["median"] == 300
        assert stats["p50"] == 300 and stats["p90"] == 500
        assert stats["stdev"] > 0

    def test_small_samples_are_flagged_unreliable(self):
        assert summarize(list(range(5)))["reliable"] is False
        assert summarize(list(range(25)))["reliable"] is True

    def test_stdev_of_one_sample_is_zero_not_null(self):
        assert summarize([7.0])["stdev"] == 0.0

    def test_empty_summary_is_all_none(self):
        stats = summarize([])
        assert stats["count"] == 0
        assert stats["mean"] is None and stats["p95"] is None

    def test_rates_are_undefined_with_no_transactions(self):
        assert rates(0, 0)["success_rate"] is None
        assert rates(99, 100)["success_rate"] == 99.0
        assert rates(99, 100)["failure_rate"] == 1.0

    def test_rates_do_not_collide_with_a_latency_summary(self):
        """These keys get merged into rows that already hold a 'total' summary."""
        assert "total" not in rates(1, 2)
        assert rates(1, 2)["evaluated"] == 2


class TestSanitizer:
    def test_pan_is_reduced_to_last_four(self):
        assert sanitize({"cardNumber": "4111111111111111"}) == {"cardNumber": "****1111"}

    def test_cvv_is_dropped_entirely(self):
        assert "cvv" not in sanitize({"cvv": "123", "amount": 1})
        assert "cvc" not in sanitize({"cvc": "123"})

    def test_secrets_are_redacted_by_key(self):
        cleaned = sanitize({"Authorization": "Bearer abc", "api_key": "sk_live_x",
                            "webhook_secret": "whsec_x"})
        assert set(cleaned.values()) == {"[redacted]"}

    def test_nested_structures_are_walked(self):
        cleaned = sanitize({"a": [{"secret": "x", "cardNumber": "4111111111111111"}]})
        assert cleaned["a"][0]["secret"] == "[redacted]"
        assert cleaned["a"][0]["cardNumber"] == "****1111"

    def test_pan_in_free_text_is_masked(self):
        assert scrub_text("declined card 4111 1111 1111 1111") == "declined card ****1111"

    def test_long_non_card_numbers_survive(self):
        # A reference or timestamp must not be mangled just for being long.
        assert scrub_text("ref 20260819123456789") == "ref 20260819123456789"

    def test_headers_are_sanitized(self):
        assert sanitize_headers({"Authorization": "Bearer x",
                                 "Content-Type": "application/json"}) == {
            "Authorization": "[redacted]", "Content-Type": "application/json"}

    def test_deep_recursion_is_bounded(self):
        payload = current = {}
        for _ in range(30):
            current["next"] = {}
            current = current["next"]
        assert sanitize(payload) is not None      # does not blow the stack


class TestCrypto:
    def test_round_trip(self):
        values = {"secret_key": "sk_test_abc", "api_base": "https://example.test"}
        assert decrypt_dict(encrypt_dict(values)) == values

    def test_ciphertext_does_not_contain_the_plaintext(self):
        blob = encrypt_dict({"secret_key": "sk_test_supersecret"})
        assert "supersecret" not in blob

    def test_two_encryptions_differ(self):
        # Fernet includes a random IV, so identical input must not produce identical
        # ciphertext — otherwise equal credentials would be visible as equal blobs.
        assert encrypt_dict({"a": "b"}) != encrypt_dict({"a": "b"})

    def test_empty_blob_decrypts_to_empty(self):
        assert decrypt_dict("") == {}

    def test_mask_keeps_prefix_and_last_four(self):
        masked = mask("sk_test_51ABCDEFGHIJKLMNX92D")
        assert masked.startswith("sk_") and masked.endswith("X92D")
        assert "ABCDEFGH" not in masked

    def test_short_values_are_masked_completely(self):
        assert set(mask("abc123")) == {"*"}


class TestSecurity:
    def test_password_hash_round_trip(self):
        hashed = hash_password("correct horse battery staple")
        assert hashed != "correct horse battery staple"
        assert verify_password("correct horse battery staple", hashed)
        assert not verify_password("wrong", hashed)

    def test_malformed_hash_reads_as_wrong_password(self):
        assert verify_password("anything", "not-a-bcrypt-hash") is False

    def test_token_round_trip(self):
        token = create_access_token("user-id", role="admin", email="a@b.test")
        payload = decode_access_token(token)
        assert payload["sub"] == "user-id" and payload["role"] == "admin"

    def test_tampered_token_is_rejected(self):
        import jwt
        token = create_access_token("user-id", role="USER", email="a@b.test")
        with pytest.raises(jwt.PyJWTError):
            decode_access_token(token[:-4] + "aaaa")

    def test_expired_token_is_rejected(self):
        import jwt
        token = create_access_token("user-id", role="USER", email="a@b.test",
                                    expires_minutes=-1)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_access_token(token)

    def test_a_password_hash_is_salted(self):
        """Two accounts with the same password must not share a hash."""
        assert hash_password("Same Password 9!") != hash_password("Same Password 9!")


class TestPasswordPolicy:
    """Specification section 10: password complexity is enforced, not suggested."""

    def test_a_strong_password_is_accepted(self):
        validate_password("Corr3ct-Horse-Battery!", username="alice",
                          email="alice@example.com")

    @pytest.mark.parametrize("password, missing", [
        ("Sh0rt!", "at least 12 characters"),
        ("alllowercase9!", "an upper-case letter"),
        ("ALLUPPERCASE9!", "a lower-case letter"),
        ("NoDigitsInHere!", "a digit"),
        ("NoSymbolsInHere9", "a symbol"),
    ])
    def test_weak_passwords_name_what_is_missing(self, password, missing):
        with pytest.raises(PasswordPolicyError) as exc:
            validate_password(password)
        assert missing in exc.value.problems

    def test_a_password_containing_the_username_is_refused(self):
        with pytest.raises(PasswordPolicyError) as exc:
            validate_password("Johnsmith-9-Aa!", username="johnsmith")
        assert "something other than the username or email address" in exc.value.problems

    def test_the_error_never_carries_the_password(self):
        with pytest.raises(PasswordPolicyError) as exc:
            validate_password("secret")
        assert "secret" not in str(exc.value)

    def test_over_length_passwords_are_refused_rather_than_truncated(self):
        """bcrypt ignores everything past 72 bytes, which would make two differ by nothing."""
        with pytest.raises(PasswordPolicyError):
            validate_password("Aa9!" + "x" * 100)


class TestAuditSanitizing:
    def test_secret_looking_keys_are_redacted(self):
        from app.services.audit import _safe_detail

        clean = _safe_detail({"role": "ADMIN", "password": "hunter2",
                              "nested": {"api_key": "sk_live_x", "status": "ACTIVE"}})
        assert clean["role"] == "ADMIN"
        assert clean["password"] == "[redacted]"
        assert clean["nested"] == {"api_key": "[redacted]", "status": "ACTIVE"}

    def test_nothing_is_lost_when_there_is_nothing_to_redact(self):
        from app.services.audit import _safe_detail

        assert _safe_detail({"from": "USER", "to": "ADMIN"}) == {"from": "USER", "to": "ADMIN"}


class TestRoleAndStatusVocabulary:
    def test_historical_role_values_still_read_back(self):
        """Rows written before the rename must not become unreadable."""
        assert normalize_role("admin") is UserRole.ADMIN
        assert normalize_role("viewer") is UserRole.USER
        assert normalize_role("USER") is UserRole.USER

    def test_an_unknown_role_is_refused_rather_than_downgraded(self):
        with pytest.raises(ValueError):
            normalize_role("superuser")

    def test_only_active_may_sign_in(self):
        assert UserStatus.ACTIVE.can_sign_in
        assert not UserStatus.INACTIVE.can_sign_in
        assert not UserStatus.LOCKED.can_sign_in


class TestLoginLimiter:
    def test_the_window_slides(self):
        from app.services.auth import SlidingWindowLimiter

        limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
        assert limiter.check("a")[0]
        limiter.hit("a")
        limiter.hit("a")
        allowed, retry_after = limiter.check("a")
        assert not allowed and retry_after > 0
        # Buckets are per key: one noisy address does not lock out everyone else.
        assert limiter.check("b")[0]
        limiter.clear("a")
        assert limiter.check("a")[0]


class TestErrorNormalization:
    def test_http_status_mapping(self):
        assert category_for_http(200) is ErrorCategory.SUCCESS
        assert category_for_http(401) is ErrorCategory.AUTHENTICATION_ERROR
        assert category_for_http(402) is ErrorCategory.GATEWAY_DECLINE
        assert category_for_http(422) is ErrorCategory.INVALID_REQUEST
        assert category_for_http(503) is ErrorCategory.GATEWAY_5XX
        assert category_for_http(None) is ErrorCategory.NETWORK_ERROR

    def test_gateway_code_is_preserved_alongside_the_category(self):
        error = normalize(GatewayError("declined", gateway_code="123", http_status=200),
                          gateway="geidea", operation="authorization")
        # Section 37: the platform's category and the gateway's own code, side by side.
        assert error.gateway_code == "123"
        assert error.to_dict()["gateway"] == "geidea"

    def test_timeout_is_its_own_category(self):
        import httpx
        error = normalize(httpx.ReadTimeout("slow"), gateway="stripe", operation="pay")
        assert error.category is ErrorCategory.TIMEOUT


class TestSettingsParsing:
    """The env-var shapes documented in .env.example must actually load."""

    def test_cors_origins_accepts_a_bare_comma_separated_list(self, monkeypatch):
        # pydantic-settings JSON-decodes complex types by default, which made the
        # documented `CORS_ORIGINS=https://busrapay.com` fail at start-up.
        from app.core.config import Settings

        monkeypatch.setenv("CORS_ORIGINS", "https://busrapay.com,http://localhost:5173")
        assert Settings().cors_origins == ["https://busrapay.com", "http://localhost:5173"]

    def test_cors_origins_accepts_a_single_origin(self, monkeypatch):
        from app.core.config import Settings

        monkeypatch.setenv("CORS_ORIGINS", "https://busrapay.com")
        assert Settings().cors_origins == ["https://busrapay.com"]

    def test_production_refuses_development_secrets(self, monkeypatch):
        from app.core.config import DEV_SECRET_KEY, Settings

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", DEV_SECRET_KEY)
        settings = Settings()
        assert "APP_SECRET_KEY" in settings.insecure_defaults()


class TestClientSideValues:
    """Values a gateway mints for its own browser SDK must survive sanitization."""

    def test_adyen_session_data_is_not_redacted(self):
        # sessionData is opaque, useless without its session, and designed to reach the
        # browser. Redacting it would break Drop-in while protecting nothing.
        cleaned = sanitize({"session_data": "Ab02b4c0!BQABAg", "client_key": "test_ABC"})
        assert cleaned["session_data"] == "Ab02b4c0!BQABAg"
        assert cleaned["client_key"] == "test_ABC"

    def test_real_secrets_beside_them_are_still_redacted(self):
        cleaned = sanitize({"session_data": "blob", "api_key": "live_key",
                            "access_token": "tok"})
        assert cleaned["session_data"] == "blob"
        assert cleaned["api_key"] == "[redacted]"
        assert cleaned["access_token"] == "[redacted]"


class TestFailedRequestSnippet:
    """The request body kept for a rejected call, and what must never be in it."""

    async def _measure(self, status: int, body: dict):
        import httpx
        from app.gateways.http import InstrumentedClient
        from app.models.enums import NormalizedOperation

        client = InstrumentedClient(
            gateway_code="test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={"ok": False}))),
            known_secrets=["sk_super_secret_value"])
        await client.call("POST /x", "POST", "https://gateway.test/x",
                          normalized=NormalizedOperation.AUTHORIZATION, json=body)
        await client.client.aclose()
        return client.measurements[-1]

    async def test_a_successful_call_keeps_its_request_too(self):
        """A working call is the reference a broken one is compared against."""
        record = await self._measure(200, {"sessionId": "s1"})
        assert record.request_snippet is not None
        assert "s1" in record.request_snippet

    async def test_a_rejected_call_keeps_what_was_sent(self):
        record = await self._measure(400, {"sessionId": "s1", "source": "DirectAPI"})
        assert record.request_snippet is not None
        assert '"source": "DirectAPI"' in record.request_snippet
        assert "s1" in record.request_snippet

    async def test_the_kept_request_never_contains_card_data(self):
        """Section 25 holds here too — this is the one place a PAN could have leaked."""
        record = await self._measure(400, {
            "cardNumber": "4111111111111111",
            "paymentMethod": {"cardNumber": "4111111111111111", "cvv": "123"},
            "apiPassword": "sk_super_secret_value",
        })
        snippet = record.request_snippet or ""
        assert "4111111111111111" not in snippet
        assert "****1111" in snippet
        # The CVV is dropped by name, not masked, so the key is gone entirely.
        assert "cvv" not in snippet
        assert "sk_super_secret_value" not in snippet
