# SPDX-License-Identifier: MIT
"""Tests for resolver module."""
import os
import tempfile
import unittest
from pathlib import Path
from kiko import db
from kiko import resolver
from kiko import providers as ptable


class TestKeyNameProblem(unittest.TestCase):
    def test_valid_names(self):
        """Test valid key names."""
        self.assertIsNone(resolver.key_name_problem("OPENAI_API_KEY"))
        self.assertIsNone(resolver.key_name_problem("MY_CUSTOM_KEY"))
        self.assertIsNone(resolver.key_name_problem("key_name"))
        self.assertIsNone(resolver.key_name_problem("KEY123"))
        self.assertIsNone(resolver.key_name_problem("hf_token"))
        self.assertIsNone(resolver.key_name_problem("sk_api_key"))
        self.assertIsNone(resolver.key_name_problem("gsk_name"))
        self.assertIsNone(resolver.key_name_problem("ghp_x"))

    def test_invalid_empty(self):
        """Test empty name."""
        self.assertEqual(resolver.key_name_problem(""), "name")
        self.assertEqual(resolver.key_name_problem(None), "name")

    def test_invalid_spaces(self):
        """Test names with spaces."""
        self.assertEqual(resolver.key_name_problem("MY KEY"), "name")

    def test_key_value_detection_gsk(self):
        """Test detecting gsk_ key values."""
        # gsk_ followed by long token-like string
        self.assertEqual(resolver.key_name_problem("gsk_" + "a" * 52), "value")
        # gsk_ with underscore in suffix is still a name (not a value)
        self.assertEqual(resolver.key_name_problem("gsk_token"), None)

    def test_key_value_detection_sk(self):
        """Test detecting sk- key values."""
        self.assertEqual(resolver.key_name_problem("sk-" + "a" * 52), "value")
        self.assertEqual(resolver.key_name_problem("sk-" + "a" * 20), "value")
        self.assertEqual(resolver.key_name_problem("sk-api_key"), "name")

    def test_key_value_detection_nvapi(self):
        """Test detecting nvapi- key values."""
        self.assertEqual(resolver.key_name_problem("nvapi-" + "a" * 52), "value")

    def test_key_value_detection_AIza(self):
        """Test detecting AIza key values."""
        self.assertEqual(resolver.key_name_problem("AIza" + "a" * 52), "value")

    def test_key_value_long_no_underscore(self):
        """Test long string with digit and few underscores."""
        self.assertEqual(resolver.key_name_problem("abc123" + "x" * 26), "value")

    def test_valid_key_name(self):
        """Test valid_key_name function."""
        self.assertTrue(resolver.valid_key_name("OPENAI_API_KEY"))
        self.assertTrue(resolver.valid_key_name("MY_KEY"))
        self.assertFalse(resolver.valid_key_name(""))
        self.assertFalse(resolver.valid_key_name("MY KEY"))
        self.assertFalse(resolver.valid_key_name("sk-" + "a" * 52))

    def test_show_name(self):
        """Test show_name function."""
        self.assertEqual(resolver.show_name("OPENAI_API_KEY"), "OPENAI_API_KEY")
        self.assertEqual(resolver.show_name("sk-" + "a" * 52), resolver.HIDDEN)
        self.assertEqual(resolver.show_name(""), resolver.HIDDEN)
        self.assertEqual(resolver.show_name(None), resolver.HIDDEN)


class TestMapRow(unittest.TestCase):
    def test_map_row_openai_bare(self):
        """Test mapping bare OpenAI model."""
        row = {"provider": "gpt-4o", "api_base": "", "api_key_env": "OPENAI_API_KEY"}
        mapped = resolver.map_row(row)
        self.assertEqual(mapped.provider, "openai")
        self.assertEqual(mapped.model_id, "gpt-4o")

    def test_map_row_openai_with_base(self):
        """Test mapping OpenAI model with custom base."""
        row = {"provider": "gpt-4o", "api_base": "https://custom.openai.com/v1", "api_key_env": "OPENAI_API_KEY"}
        mapped = resolver.map_row(row)
        self.assertEqual(mapped.provider, "custom")
        self.assertEqual(mapped.model_id, "gpt-4o")
        self.assertEqual(mapped.entry["base_url"], "https://custom.openai.com/v1")

    def test_map_row_known_prefix(self):
        """Test mapping with known provider prefix."""
        row = {"provider": "nvidia/nemotron-3-ultra", "api_base": "", "api_key_env": "NVIDIA_API_KEY"}
        mapped = resolver.map_row(row)
        self.assertEqual(mapped.provider, "nvidia")
        self.assertEqual(mapped.model_id, "nemotron-3-ultra")

    def test_map_row_gemini(self):
        """Test mapping Gemini model."""
        row = {"provider": "gemini/gemini-1.5-pro", "api_base": "", "api_key_env": "GEMINI_API_KEY"}
        mapped = resolver.map_row(row)
        self.assertEqual(mapped.provider, "gemini")
        self.assertEqual(mapped.model_id, "gemini-1.5-pro")

    def test_map_row_unknown_prefix_no_base(self):
        """Test unknown prefix without api_base raises error."""
        row = {"provider": "anthropic/claude-3", "api_base": "", "api_key_env": "ANTHROPIC_API_KEY"}
        with self.assertRaises(resolver.MappingError):
            resolver.map_row(row)

    def test_map_row_unknown_prefix_with_base(self):
        """Test unknown prefix with api_base works as custom."""
        row = {"provider": "custom/llama-3", "api_base": "https://my-endpoint.com/v1", "api_key_env": "CUSTOM_KEY"}
        mapped = resolver.map_row(row)
        self.assertEqual(mapped.provider, "custom")
        self.assertEqual(mapped.model_id, "custom/llama-3")

    def test_map_row_bare_not_openai_no_base(self):
        """Test bare non-OpenAI model without base raises error."""
        row = {"provider": "llama-3-70b", "api_base": "", "api_key_env": "CUSTOM_KEY"}
        with self.assertRaises(resolver.MappingError):
            resolver.map_row(row)

    def test_map_row_bare_not_openai_wrong_key(self):
        """Test bare OpenAI model with non-OpenAI key name raises error."""
        row = {"provider": "gpt-4o", "api_base": "", "api_key_env": "CUSTOM_KEY"}
        with self.assertRaises(resolver.MappingError):
            resolver.map_row(row)

    def test_map_row_empty_model_id(self):
        """Test empty model id raises error."""
        row = {"provider": "nvidia/", "api_base": "", "api_key_env": "NVIDIA_API_KEY"}
        with self.assertRaises(resolver.MappingError):
            resolver.map_row(row)


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = db.Database(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_snapshot_empty(self):
        """Test snapshot with empty DB."""
        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()
        self.assertEqual(snap.aliases, [])
        self.assertEqual(snap.settings, {})
        self.assertEqual(snap.models, [])

    def test_snapshot_with_models(self):
        """Test snapshot with models."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", model_class="SONNET")
        self.db.upsert_model("model2", "gemini/gemini-1.5-pro", "GEMINI_API_KEY", model_class="HAIKU")

        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()

        self.assertEqual(len(snap.models), 2)
        self.assertEqual(len(snap.by_alias), 2)
        self.assertEqual(len(snap.by_pname), 2)

    def test_snapshot_aliases(self):
        """Test snapshot includes aliases."""
        self.db.set_alias("my-*", "model1")
        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()
        self.assertEqual(snap.aliases, [("my-*", "model1")])

    def test_snapshot_settings(self):
        """Test snapshot includes settings."""
        self.db.set_setting("monthly_cap_usd", "100.0")
        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()
        self.assertEqual(snap.settings["monthly_cap_usd"], "100.0")

    def test_snapshot_classes(self):
        """Test snapshot includes classes."""
        self.db.add_class("VOICE", "voice")
        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()
        self.assertEqual(len(snap.classes), 5)
        self.assertIn(("VOICE", "voice"), snap.classes)

    def test_best_model(self):
        """Test finding best model for a class."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", model_class="SONNET", priority=100, status="ok")
        self.db.upsert_model("model2", "openai/gpt-4o-mini", "OPENAI_API_KEY", model_class="SONNET", priority=50, status="ok")

        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()
        best = snap.best("SONNET")
        self.assertIsNotNone(best)
        self.assertEqual(best.row["alias"], "model2")  # lower priority wins

    def test_ranked_models(self):
        """Test ranked models for a class."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", model_class="SONNET", priority=100, status="ok")
        self.db.upsert_model("model2", "openai/gpt-4o-mini", "OPENAI_API_KEY", model_class="SONNET", priority=50, status="unknown")

        catalog = resolver.Catalog(self.db)
        snap = catalog.snapshot()
        ranked = snap.ranked("SONNET")
        self.assertEqual(len(ranked), 2)
        # ok comes before unknown
        self.assertEqual(ranked[0].row["alias"], "model1")
        self.assertEqual(ranked[1].row["alias"], "model2")

    def test_snapshot_caching(self):
        """Test snapshot caching with TTL."""
        catalog = resolver.Catalog(self.db, ttl=10.0)
        snap1 = catalog.snapshot()
        snap2 = catalog.snapshot()
        self.assertIs(snap1, snap2)  # same object due to caching


class TestDBResolver(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = db.Database(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_resolver_direct_name(self):
        """Test resolving direct provider/model name."""
        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog)
        result = r("openai/gpt-4o")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "openai")
        self.assertEqual(result[1], "gpt-4o")

    def test_resolver_alias(self):
        """Test resolving via alias glob."""
        self.db.set_alias("my-*", "model1")
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY")

        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog)
        result = r("my-model")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "db/model1")

    def test_resolver_class(self):
        """Test resolving via class keyword."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", model_class="SONNET", status="ok", priority=100)
        self.db.upsert_model("model2", "openai/gpt-4o-mini", "OPENAI_API_KEY", model_class="SONNET", status="ok", priority=50)

        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog)
        result = r("sonnet")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "db/model2")  # lower priority

    def test_resolver_env_fallback(self):
        """Test resolving via ANTHROPIC_DEFAULT_*_MODEL env var."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY")

        env = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "model1"}
        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog, env=env)
        result = r("sonnet")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "db/model1")

    def test_resolver_unknown_model(self):
        """Test resolving unknown model returns None."""
        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog)
        result = r("unknown-model")
        self.assertIsNone(result)

    def test_resolver_candidates_class(self):
        """Test candidates for class name."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", model_class="SONNET", status="ok", priority=100)
        self.db.upsert_model("model2", "openai/gpt-4o-mini", "OPENAI_API_KEY", model_class="SONNET", status="ok", priority=50)

        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog)
        candidates = r.candidates("sonnet")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0][0], "db/model2")  # lower priority first

    def test_resolver_candidates_direct(self):
        """Test candidates for direct name."""
        catalog = resolver.Catalog(self.db)
        r = resolver.DBResolver(catalog)
        candidates = r.candidates("openai/gpt-4o")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0], ("openai", "gpt-4o"))

    def test_resolver_disabled_model(self):
        """Test disabled models are not returned."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", model_class="SONNET", enabled=False)

        catalog = resolver.Catalog(self.db)
        # Use clean env to avoid env fallback
        r = resolver.DBResolver(catalog, env={})
        result = r("sonnet")
        self.assertIsNone(result)


class TestStatusHook(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = db.Database(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_status_hook_updates_model(self):
        """Test status hook updates model status."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", status="unknown")
        catalog = resolver.Catalog(self.db)
        hook = resolver.status_hook(self.db, catalog)

        # Call hook with ok=True
        hook("db/model1", True)

        # Check model status updated
        model = self.db.get_model("model1")
        self.assertEqual(model["status"], "ok")

    def test_status_hook_error(self):
        """Test status hook updates to error."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", status="ok")
        catalog = resolver.Catalog(self.db)
        hook = resolver.status_hook(self.db, catalog)

        hook("db/model1", False)

        model = self.db.get_model("model1")
        self.assertEqual(model["status"], "error")


class TestBillingWriter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = db.Database(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_billing_writer_basic(self):
        """Test billing writer records request."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY", price_in=1.0, price_out=2.0)
        catalog = resolver.Catalog(self.db)
        writer = resolver.BillingWriter(self.db, catalog)

        # Mock RequestResult
        from kiko import app as lite
        res = lite.RequestResult(
            request_id="test123",
            client_model="model1",
            provider="db/model1",
            upstream_model="gpt-4o",
            stream=False,
            attempts=1,
            status=200,
            stop_reason="end_turn",
            usage={"input_tokens": 100, "output_tokens": 50},
            latency=1.5,
        )
        writer(res)

        summary = self.db.billing_summary()
        self.assertEqual(len(summary), 1)
        # Model recorded is upstream_model or client_model
        self.assertEqual(summary[0]["model"], "gpt-4o")
        self.assertEqual(summary[0]["tokens_in"], 100)
        self.assertEqual(summary[0]["tokens_out"], 50)
        # cost = (100/1M * 1.0) + (50/1M * 2.0) = 0.0001 + 0.0001 = 0.0002
        self.assertAlmostEqual(summary[0]["cost_usd"], 0.0002, places=6)

    def test_billing_writer_error(self):
        """Test billing writer records error with cost 0."""
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY")
        catalog = resolver.Catalog(self.db)
        writer = resolver.BillingWriter(self.db, catalog)

        from kiko import app as lite
        res = lite.RequestResult(
            request_id="test123",
            client_model="model1",
            provider="db/model1",
            upstream_model="gpt-4o",
            stream=False,
            attempts=1,
            status=429,
            error="rate_limit_error",
            usage={},
            latency=0.5,
        )
        writer(res)

        summary = self.db.billing_summary()
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["cost_usd"], 0.0)
        # error is stored in billing but not returned by billing_summary
        # just verify the billing was recorded with cost 0

    def test_billing_writer_disabled(self):
        """Test billing writer respects disable_billing setting."""
        self.db.set_setting("disable_billing", "true")
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY")
        catalog = resolver.Catalog(self.db)
        writer = resolver.BillingWriter(self.db, catalog)

        from kiko import app as lite
        res = lite.RequestResult(
            request_id="test123",
            client_model="model1",
            provider="db/model1",
            upstream_model="gpt-4o",
            stream=False,
            attempts=1,
            status=200,
            usage={"input_tokens": 100, "output_tokens": 50},
            latency=1.5,
        )
        writer(res)

        summary = self.db.billing_summary()
        self.assertEqual(len(summary), 0)


class TestCapGate(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = db.Database(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_cap_gate_no_cap(self):
        """Test cap gate returns None when no cap set."""
        catalog = resolver.Catalog(self.db)
        gate = resolver.cap_gate(self.db, catalog)
        self.assertIsNone(gate())

    def test_cap_gate_under_cap(self):
        """Test cap gate returns None when under cap."""
        self.db.set_setting("monthly_cap_usd", "100.0")
        catalog = resolver.Catalog(self.db)
        gate = resolver.cap_gate(self.db, catalog)
        self.assertIsNone(gate())

    def test_cap_gate_over_cap(self):
        """Test cap gate returns message when over cap."""
        self.db.set_setting("monthly_cap_usd", "0.001")
        self.db.add_billing("model1", 1000000, 0, 0.01)  # spent 0.01 > 0.001 cap
        catalog = resolver.Catalog(self.db)
        gate = resolver.cap_gate(self.db, catalog)
        msg = gate()
        self.assertIsNotNone(msg)
        self.assertIn("Monthly spending cap", msg)


if __name__ == "__main__":
    unittest.main()