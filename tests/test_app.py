# SPDX-License-Identifier: MIT
"""Tests for app module."""
import json
import unittest
from unittest.mock import Mock, MagicMock, patch
from kiko_lite import app as lite
from kiko_lite import providers as ptable
from kiko_lite.providers import openai_compat as oc
from kiko_lite.providers import gemini


class TestValidModelId(unittest.TestCase):
    def test_valid_model_id_openai(self):
        self.assertTrue(lite.valid_model_id("gpt-4o", "openai"))
        self.assertTrue(lite.valid_model_id("model-name", "openai"))
        self.assertTrue(lite.valid_model_id("model.name", "openai"))
        self.assertTrue(lite.valid_model_id("model:name", "openai"))

    def test_valid_model_id_gemini(self):
        self.assertTrue(lite.valid_model_id("gemini-1.5-pro", "gemini"))
        # "models/gemini-1.5-pro" has a slash, so it's invalid for Gemini

    def test_invalid_model_id_gemini_slash(self):
        self.assertFalse(lite.valid_model_id("gemini/model/extra", "gemini"))

    def test_invalid_model_id_traversal(self):
        self.assertFalse(lite.valid_model_id("../etc/passwd", "openai"))
        self.assertFalse(lite.valid_model_id("model/..", "openai"))
        self.assertFalse(lite.valid_model_id(".", "openai"))
        self.assertFalse(lite.valid_model_id("..", "openai"))

    def test_invalid_model_id_chars(self):
        self.assertFalse(lite.valid_model_id("model@name", "openai"))
        self.assertFalse(lite.valid_model_id("model name", "openai"))


class TestSplitTarget(unittest.TestCase):
    def test_split_target_valid(self):
        result = lite.split_target("nvidia/nemotron-3-ultra")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "nvidia")
        self.assertEqual(result[1], "nemotron-3-ultra")

    def test_split_target_unknown_provider(self):
        result = lite.split_target("unknown/model")
        self.assertIsNone(result)

    def test_split_target_no_slash(self):
        result = lite.split_target("model-name")
        self.assertIsNone(result)


class TestSimpleResolver(unittest.TestCase):
    def test_resolver_alias(self):
        """Test resolver with alias glob patterns."""
        resolver = lite.SimpleResolver(aliases={"my-*": "openai/gpt-4o"})
        result = resolver("my-model")
        self.assertEqual(result, ("openai", "gpt-4o"))

    def test_resolver_class_keyword(self):
        """Test resolver with class keywords."""
        resolver = lite.SimpleResolver(classes={"sonnet": "openai/gpt-4o"})
        result = resolver("sonnet")
        self.assertEqual(result, ("openai", "gpt-4o"))

    def test_resolver_class_name(self):
        """Test resolver with class name."""
        resolver = lite.SimpleResolver(classes={"SONNET": "openai/gpt-4o"})
        result = resolver("SONNET")
        self.assertEqual(result, ("openai", "gpt-4o"))

    def test_resolver_env_fallback(self):
        """Test resolver with env fallback."""
        env = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "openai/gpt-4o"}
        resolver = lite.SimpleResolver(env=env)
        result = resolver("sonnet")
        self.assertEqual(result, ("openai", "gpt-4o"))

    def test_resolver_direct(self):
        """Test resolver with direct name."""
        resolver = lite.SimpleResolver()
        result = resolver("openai/gpt-4o")
        self.assertEqual(result, ("openai", "gpt-4o"))

    def test_resolver_unknown(self):
        """Test resolver with unknown model."""
        resolver = lite.SimpleResolver()
        result = resolver("unknown-model")
        self.assertIsNone(result)


class TestRequestResult(unittest.TestCase):
    def test_request_result_defaults(self):
        """Test RequestResult default values."""
        res = lite.RequestResult(request_id="test123", client_model="model1")
        self.assertEqual(res.request_id, "test123")
        self.assertEqual(res.client_model, "model1")
        self.assertEqual(res.provider, "")
        self.assertEqual(res.upstream_model, "")
        self.assertFalse(res.stream)
        self.assertEqual(res.attempts, 0)
        self.assertEqual(res.status, 0)
        self.assertIsNone(res.stop_reason)
        self.assertIsNone(res.error)
        self.assertEqual(res.usage, {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(res.latency, 0.0)


class TestErrorMapping(unittest.TestCase):
    def test_map_upstream_error_429(self):
        """Test mapping 429 error."""
        status, err_type, msg, headers = lite.map_upstream_error(429, b'{"error": {"message": "rate limited"}}', "60", "openai", "key123")
        self.assertEqual(status, 429)
        self.assertEqual(err_type, "rate_limit_error")
        self.assertIn("rate limited", msg)
        self.assertEqual(headers["Retry-After"], "60")

    def test_map_upstream_error_500(self):
        """Test mapping 500 error."""
        status, err_type, msg, headers = lite.map_upstream_error(500, b'{"error": {"message": "server error"}}', None, "openai", "key123")
        # 500 is not in OVERLOADED set, falls through to 502 api_error
        self.assertEqual(status, 502)
        self.assertEqual(err_type, "api_error")

    def test_map_upstream_error_401(self):
        """Test mapping 401 error."""
        status, err_type, msg, headers = lite.map_upstream_error(401, b'{"error": {"message": "unauthorized"}}', None, "openai", "key123")
        self.assertEqual(status, 502)
        self.assertEqual(err_type, "api_error")
        self.assertIn("rejected the configured API key", msg)

    def test_map_upstream_error_404(self):
        """Test mapping 404 error."""
        status, err_type, msg, headers = lite.map_upstream_error(404, b'{"error": {"message": "not found"}}', None, "openai", "key123")
        self.assertEqual(status, 404)
        self.assertEqual(err_type, "not_found_error")

    def test_map_upstream_error_413(self):
        """Test mapping 413 error."""
        status, err_type, msg, headers = lite.map_upstream_error(413, b'{"error": {"message": "too large"}}', None, "openai", "key123")
        self.assertEqual(status, 413)
        self.assertEqual(err_type, "request_too_large")

    def test_map_upstream_error_scrubs_key(self):
        """Test that API key is scrubbed from error message."""
        status, err_type, msg, headers = lite.map_upstream_error(500, b'{"error": {"message": "failed with key key123"}}', None, "openai", "key123")
        self.assertNotIn("key123", msg)
        self.assertIn("***", msg)

    def test_exc_error_timeout(self):
        """Test mapping timeout exception."""
        exc = lite.khttp.HttpTimeout("timeout")
        status, err_type, msg, headers = lite._exc_error(exc)
        self.assertEqual(status, 529)
        self.assertEqual(err_type, "overloaded_error")

    def test_exc_error_connection(self):
        """Test mapping connection error."""
        exc = lite.khttp.ConnectError("connection refused")
        status, err_type, msg, headers = lite._exc_error(exc)
        self.assertEqual(status, 529)
        self.assertEqual(err_type, "overloaded_error")


class TestFailoverHelpers(unittest.TestCase):
    def test_failover_status(self):
        """Test failover status detection."""
        self.assertTrue(lite.failover_status(408))
        self.assertTrue(lite.failover_status(425))
        self.assertTrue(lite.failover_status(429))
        self.assertTrue(lite.failover_status(500))
        self.assertTrue(lite.failover_status(503))
        self.assertFalse(lite.failover_status(400))
        self.assertFalse(lite.failover_status(401))
        self.assertFalse(lite.failover_status(404))

    def test_retry_info_gemini(self):
        """Test extracting Gemini retry info."""
        raw = json.dumps([{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}]).encode()
        result = lite._retry_info(raw)
        self.assertEqual(result, 37.0)

    def test_retry_info_none(self):
        """Test no retry info returns None."""
        raw = json.dumps([{"error": {"message": "error"}}]).encode()
        result = lite._retry_info(raw)
        self.assertIsNone(result)

    def test_cooldown_seconds_retry_after(self):
        """Test cooldown from Retry-After."""
        self.assertEqual(lite.cooldown_seconds("60", b""), 60.0)
        self.assertEqual(lite.cooldown_seconds("10", b""), 10.0)

    def test_cooldown_seconds_gemini(self):
        """Test cooldown from Gemini retry info."""
        raw = json.dumps([{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}]}}]).encode()
        self.assertEqual(lite.cooldown_seconds(None, raw), 45.0)

    def test_cooldown_seconds_default(self):
        """Test default cooldown."""
        self.assertEqual(lite.cooldown_seconds(None, b""), 60.0)

    def test_cooldown_bounds(self):
        """Test cooldown bounds (1s - 1h)."""
        self.assertEqual(lite.cooldown_seconds("0", b""), 1.0)
        self.assertEqual(lite.cooldown_seconds("7200", b""), 3600.0)


class TestCooldowns(unittest.TestCase):
    def test_mark_and_remaining(self):
        """Test marking and checking cooldown."""
        cd = lite.Cooldowns(clock=lambda: 1000.0)
        cd.mark(("provider", "model"), 60.0)
        self.assertEqual(cd.remaining(("provider", "model")), 60.0)

    def test_clear(self):
        """Test clearing cooldown."""
        cd = lite.Cooldowns(clock=lambda: 1000.0)
        cd.mark(("provider", "model"), 60.0)
        cd.clear(("provider", "model"))
        self.assertEqual(cd.remaining(("provider", "model")), 0.0)

    def test_expired(self):
        """Test expired cooldown returns 0."""
        clock = [1000.0]
        def get_clock():
            return clock[0]
        cd = lite.Cooldowns(clock=get_clock)
        cd.mark(("provider", "model"), 60.0)
        clock[0] = 1061.0  # past cooldown
        self.assertEqual(cd.remaining(("provider", "model")), 0.0)

    def test_mark_ok(self):
        """Test marking successful request."""
        clock = [1000.0]
        def get_clock():
            return clock[0]
        cd = lite.Cooldowns(clock=get_clock)
        cd.mark_ok(("provider", "model"))
        self.assertEqual(cd.since_ok(("provider", "model")), 0.0)
        clock[0] = 1100.0
        self.assertEqual(cd.since_ok(("provider", "model")), 100.0)


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.resolver = Mock(return_value=("openai", "gpt-4o"))
        self.master_key = "test-master-key"
        self.providers = ptable.PROVIDERS
        self.secret = Mock(return_value="test-api-key")
        self.on_result = Mock()
        self.gateway = lite._Gateway(
            self.resolver, self.master_key, self.providers,
            http=None, policy=None, secret=self.secret,
            on_result=self.on_result, upstream_timeout=300.0,
            ping_interval=15.0, auth=True
        )

    def test_authenticate_valid(self):
        """Test authentication with valid key."""
        req = Mock()
        req.method = "POST"
        req.path = "/v1/messages"
        req.headers = {"x-api-key": "test-master-key"}
        self.gateway.authenticate(req)  # Should not raise

    def test_authenticate_bearer(self):
        """Test authentication with Bearer token."""
        req = Mock()
        req.method = "POST"
        req.path = "/v1/messages"
        req.headers = {"authorization": "Bearer test-master-key"}
        self.gateway.authenticate(req)  # Should not raise

    def test_authenticate_invalid(self):
        """Test authentication with invalid key."""
        req = Mock()
        req.method = "POST"
        req.path = "/v1/messages"
        req.headers = {"x-api-key": "wrong-key"}
        with self.assertRaises(lite.HTTPError) as cm:
            self.gateway.authenticate(req)
        self.assertEqual(cm.exception.status, 401)

    def test_authenticate_health_no_auth(self):
        """Test health endpoint doesn't require auth."""
        req = Mock()
        req.method = "GET"
        req.path = "/health"
        req.headers = {}
        self.gateway.authenticate(req)  # Should not raise

    def test_authenticate_no_auth_mode(self):
        """Test auth=False mode skips auth."""
        gw = lite._Gateway(
            self.resolver, self.master_key, self.providers,
            http=None, policy=None, secret=self.secret,
            on_result=self.on_result, upstream_timeout=300.0,
            ping_interval=15.0, auth=False
        )
        req = Mock()
        req.method = "POST"
        req.path = "/v1/messages"
        req.headers = {}
        gw.authenticate(req)  # Should not raise


class TestBuildApp(unittest.TestCase):
    def test_build_app_requires_master_key(self):
        """Test build_app requires master_key when auth=True."""
        with self.assertRaises(ValueError):
            lite.build_app(Mock(), None, auth=True)

    def test_build_app_no_auth(self):
        """Test build_app with auth=False."""
        app = lite.build_app(Mock(), None, auth=False)
        self.assertIsNotNone(app)


class TestSSE(unittest.TestCase):
    def test_sse_encode(self):
        """Test SSE encoding."""
        data = {"type": "test", "value": 123}
        encoded = lite.sse.encode("event_name", data)
        self.assertIn(b"event: event_name", encoded)
        self.assertIn(b'"type":"test"', encoded)
        self.assertIn(b'"value":123', encoded)

    def test_sse_ping(self):
        """Test SSE ping."""
        ping = lite.sse.ping()
        self.assertIn(b"event: ping", ping)

    def test_error_event(self):
        """Test error event encoding."""
        event = lite._error_event("rate_limit_error", "rate limited")
        self.assertIn(b"event: error", event)
        self.assertIn(b"rate_limit_error", event)
        self.assertIn(b"rate limited", event)


if __name__ == "__main__":
    unittest.main()