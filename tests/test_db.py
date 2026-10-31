# SPDX-License-Identifier: MIT
"""Tests for db module."""
import os
import tempfile
import unittest
from pathlib import Path
from kiko_lite import db


class TestDefaultPath(unittest.TestCase):
    def test_default_path_cli_value(self):
        """Test --db flag takes precedence."""
        with tempfile.TemporaryDirectory() as tmp:
            path = db.default_path(cli_value=Path(tmp) / "custom.db")
            self.assertEqual(path, Path(tmp) / "custom.db")

    def test_default_path_env(self):
        """Test KIKO_DB_PATH env var."""
        with tempfile.TemporaryDirectory() as tmp:
            path = db.default_path(env={"KIKO_DB_PATH": str(Path(tmp) / "env.db")})
            self.assertEqual(path, Path(tmp) / "env.db")

    def test_default_path_fallback(self):
        """Test fallback to ~/.kiko/kiko.db."""
        path = db.default_path(env={})
        self.assertEqual(path, Path.home() / ".kiko" / "kiko.db")


class TestDatabase(unittest.TestCase):
    def setUp(self):
        """Create a temporary database for each test."""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = db.Database(self.db_path)

    def tearDown(self):
        """Clean up."""
        self.tmpdir.cleanup()

    def test_schema_version(self):
        """Test schema is migrated to version 2."""
        self.assertEqual(self.db.schema_version(), 2)

    def test_models_table_exists(self):
        """Test models table exists."""
        models = self.db.models()
        self.assertEqual(models, [])

    def test_upsert_model_basic(self):
        """Test inserting a model."""
        new = self.db.upsert_model(
            alias="test-model",
            provider="openai/gpt-4o",
            api_key_env="OPENAI_API_KEY",
        )
        self.assertTrue(new)
        models = self.db.models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["alias"], "test-model")
        self.assertEqual(models[0]["provider"], "openai/gpt-4o")
        self.assertEqual(models[0]["api_key_env"], "OPENAI_API_KEY")
        self.assertEqual(models[0]["model_class"], "SONNET")
        self.assertEqual(models[0]["priority"], 100)

    def test_upsert_model_update(self):
        """Test updating a model."""
        self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY")
        new = self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY", priority=50)
        self.assertFalse(new)
        models = self.db.models()
        self.assertEqual(models[0]["priority"], 50)

    def test_upsert_model_with_prices(self):
        """Test inserting model with prices."""
        self.db.upsert_model(
            alias="test-model",
            provider="openai/gpt-4o",
            api_key_env="OPENAI_API_KEY",
            price_in=1.0,
            price_out=2.0,
            price_cache=0.5,
        )
        models = self.db.models()
        self.assertEqual(models[0]["price_in_per_mtok"], 1.0)
        self.assertEqual(models[0]["price_out_per_mtok"], 2.0)
        self.assertEqual(models[0]["price_cache_read_per_mtok"], 0.5)

    def test_upsert_model_with_class(self):
        """Test inserting with model class."""
        self.db.upsert_model(
            alias="test-model",
            provider="openai/gpt-4o",
            api_key_env="OPENAI_API_KEY",
            model_class="OPUS",
        )
        models = self.db.models()
        self.assertEqual(models[0]["model_class"], "OPUS")

    def test_get_model(self):
        """Test getting a specific model."""
        self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY")
        model = self.db.get_model("test-model")
        self.assertIsNotNone(model)
        self.assertEqual(model["alias"], "test-model")

    def test_get_model_not_found(self):
        """Test getting non-existent model."""
        model = self.db.get_model("nonexistent")
        self.assertIsNone(model)

    def test_delete_model(self):
        """Test deleting a model."""
        self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY")
        deleted = self.db.delete_model("test-model")
        self.assertTrue(deleted)
        models = self.db.models()
        self.assertEqual(models, [])

    def test_delete_model_not_found(self):
        """Test deleting non-existent model."""
        deleted = self.db.delete_model("nonexistent")
        self.assertFalse(deleted)

    def test_set_enabled(self):
        """Test enabling/disabling a model."""
        self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY")
        self.db.set_enabled("test-model", False)
        model = self.db.get_model("test-model")
        self.assertEqual(model["enabled"], 0)
        self.db.set_enabled("test-model", True)
        model = self.db.get_model("test-model")
        self.assertEqual(model["enabled"], 1)

    def test_set_status(self):
        """Test setting model status."""
        self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY")
        self.db.set_status("test-model", "ok")
        model = self.db.get_model("test-model")
        self.assertEqual(model["status"], "ok")
        self.assertIsNotNone(model["last_check"])

    def test_aliases(self):
        """Test alias operations."""
        self.db.set_alias("my-*", "test-model")
        aliases = self.db.aliases()
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0], ("my-*", "test-model"))

        self.db.set_alias("my-*", "other-model")
        aliases = self.db.aliases()
        self.assertEqual(aliases[0][1], "other-model")

        deleted = self.db.delete_alias("my-*")
        self.assertTrue(deleted)
        self.assertEqual(self.db.aliases(), [])

    def test_settings(self):
        """Test settings operations."""
        self.db.set_setting("monthly_cap_usd", "100.0")
        settings = self.db.settings()
        self.assertEqual(settings["monthly_cap_usd"], "100.0")

        self.db.set_setting("monthly_cap_usd", "200.0")
        settings = self.db.settings()
        self.assertEqual(settings["monthly_cap_usd"], "200.0")

        deleted = self.db.delete_setting("monthly_cap_usd")
        self.assertTrue(deleted)
        self.assertEqual(self.db.settings(), {})

    def test_billing(self):
        """Test billing operations."""
        self.db.add_billing("test-model", 100, 50, 0.001)
        summary = self.db.billing_summary()
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["model"], "test-model")
        self.assertEqual(summary[0]["tokens_in"], 100)
        self.assertEqual(summary[0]["tokens_out"], 50)
        self.assertEqual(summary[0]["cost_usd"], 0.001)

    def test_billing_with_extra_fields(self):
        """Test billing with extra fields."""
        self.db.add_billing(
            "test-model", 100, 50, 0.001,
            attempts=3, latency_ms=1500,
            upstream_model="openai/gpt-4o",
            error="rate_limit_error"
        )
        # billing_summary doesn't include extra fields, just verify they don't crash
        summary = self.db.billing_summary()
        self.assertEqual(summary[0]["model"], "test-model")
        self.assertEqual(summary[0]["cost_usd"], 0.001)

    def test_month_spent(self):
        """Test monthly spending calculation."""
        self.db.add_billing("test-model", 100, 50, 0.001)
        spent = self.db.month_spent()
        self.assertEqual(spent, 0.001)

    def test_model_classes_default(self):
        """Test default model classes."""
        classes = self.db.classes()
        self.assertEqual(len(classes), 4)
        names = [c["name"] for c in classes]
        self.assertEqual(names, ["OPUS", "SONNET", "HAIKU", "FABLE"])

    def test_add_class(self):
        """Test adding a model class."""
        self.db.add_class("VOICE", "voice", "voice models")
        classes = self.db.classes()
        self.assertEqual(len(classes), 5)
        voice_class = [c for c in classes if c["name"] == "VOICE"][0]
        self.assertEqual(voice_class["keyword"], "voice")
        self.assertEqual(voice_class["role"], "voice models")

    def test_add_class_invalid_name(self):
        """Test adding class with invalid name."""
        with self.assertRaises(ValueError):
            self.db.add_class("invalid", "voice")

    def test_add_class_invalid_keyword(self):
        """Test adding class with invalid keyword."""
        with self.assertRaises(ValueError):
            self.db.add_class("VOICE", "Vo")  # too short
        with self.assertRaises(ValueError):
            self.db.add_class("VOICE", "VOICE")  # not lowercase

    def test_add_class_duplicate_name(self):
        """Test adding class with duplicate name."""
        self.db.add_class("VOICE", "voice")
        with self.assertRaises(ValueError):
            self.db.add_class("VOICE", "voice2")

    def test_add_class_duplicate_keyword(self):
        """Test adding class with duplicate keyword."""
        self.db.add_class("VOICE", "voice")
        with self.assertRaises(ValueError):
            self.db.add_class("VOICE2", "voice")

    def test_add_class_overlapping_keyword(self):
        """Test adding class with overlapping keyword."""
        self.db.add_class("VOICE", "voice")
        with self.assertRaises(ValueError):
            self.db.add_class("VOICENEW", "voicenew")  # contains "voice"

    def test_alias_case_collision(self):
        """Test alias case collision detection."""
        self.db.upsert_model("Test-Model", "openai/gpt-4o", "OPENAI_API_KEY")
        with self.assertRaises(ValueError):
            self.db.upsert_model("test-model", "openai/gpt-4o", "OPENAI_API_KEY")

    def test_key_names_in_use(self):
        """Test getting key names in use."""
        self.db.upsert_model("model1", "openai/gpt-4o", "KEY1")
        self.db.upsert_model("model2", "openai/gpt-4o", "KEY2")
        names = self.db.key_names_in_use("model1")
        self.assertEqual(names, {"KEY2"})

    def test_next_model_id(self):
        """Test getting next model ID."""
        self.assertEqual(self.db.next_model_id("new-model"), 1)
        self.db.upsert_model("model1", "openai/gpt-4o", "OPENAI_API_KEY")
        self.assertEqual(self.db.next_model_id("model1"), 1)
        self.assertEqual(self.db.next_model_id("new-model"), 2)

    def test_readonly_mode(self):
        """Test readonly mode."""
        readonly_db = db.Database(self.db_path, readonly=True)
        models = readonly_db.models()
        self.assertEqual(models, [])


class TestRoUri(unittest.TestCase):
    def test_ro_uri_posix(self):
        """Test POSIX path to read-only URI."""
        uri = db.ro_uri("/tmp/test.db")
        self.assertEqual(uri, "file:///tmp/test.db?mode=ro")

    def test_ro_uri_windows_drive(self):
        """Test Windows drive letter path."""
        uri = db.ro_uri(r"C:\path\test.db")
        self.assertEqual(uri, "file:///C:/path/test.db?mode=ro")

    def test_ro_uri_unc(self):
        """Test UNC path."""
        uri = db.ro_uri(r"\\server\share\test.db")
        self.assertTrue(uri.startswith("file:////server/share/test.db"))

    def test_ro_uri_spaces(self):
        """Test path with spaces."""
        uri = db.ro_uri("/path with spaces/test.db")
        self.assertIn("%20", uri)


if __name__ == "__main__":
    unittest.main()