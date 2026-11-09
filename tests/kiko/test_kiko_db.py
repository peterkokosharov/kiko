# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Unit tests for KIKO models table operations.
Covers: _init_db (models table), seed_models, load_models, update_model_status.
No network calls — all operations are local SQLite only.
"""
import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure kiko package is on path when run directly
_KIKO_ROOT = Path(__file__).parents[2]
if str(_KIKO_ROOT) not in sys.path:
    sys.path.insert(0, str(_KIKO_ROOT))

from litellm.proxy.integrations.kiko_sqlite_logger import (
    _init_db,
    add_model,
    delete_model,
    get_billing_summary,
    load_models,
    seed_models,
    set_model_enabled,
    update_model_status,
)

_SAMPLE_MODELS = [
    {
        "model_name": "gemini-flash",
        "litellm_params": {
            "model": "gemini/gemini-3.8-flash",
            "api_key": "os.environ/GEMINI_API_KEY",
        },
    },
    {
        "model_name": "nemotron-super",
        "litellm_params": {
            "model": "openai/nvidia/nemotron-3-super-120b-a12b",
            "api_base": "https://integrate.api.nvidia.com/v1",
            "api_key": "os.environ/NVIDIA_API_KEY",
        },
    },
]


# ── DB init ────────────────────────────────────────────────────────────────────

class TestInitDb(unittest.TestCase):
    def _make_db(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name) / "test.db"

    def tearDown(self):
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_creates_billing_table(self):
        path = self._make_db()
        _init_db(path)
        con = sqlite3.connect(str(path))
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        con.close()
        self.assertIn("billing", tables)

    def test_creates_models_table(self):
        path = self._make_db()
        _init_db(path)
        con = sqlite3.connect(str(path))
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        con.close()
        self.assertIn("models", tables)

    def test_idempotent(self):
        path = self._make_db()
        _init_db(path)
        _init_db(path)  # second call must not raise


# ── seed_models ────────────────────────────────────────────────────────────────

class TestSeedModels(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_inserts_all_models(self):
        seed_models(self.path, _SAMPLE_MODELS)
        con = sqlite3.connect(str(self.path))
        count = con.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        con.close()
        self.assertEqual(count, 2)

    def test_correct_fields_google(self):
        seed_models(self.path, _SAMPLE_MODELS)
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM models WHERE alias='gemini-flash'"
        ).fetchone()
        con.close()
        self.assertEqual(row["provider"], "gemini/gemini-3.8-flash")
        self.assertEqual(row["api_key_env"], "GEMINI_API_KEY")
        self.assertIsNone(row["api_base"])
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["status"], "unknown")

    def test_api_base_stored_for_nvidia(self):
        seed_models(self.path, _SAMPLE_MODELS)
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM models WHERE alias='nemotron-super'"
        ).fetchone()
        con.close()
        self.assertEqual(row["api_base"], "https://integrate.api.nvidia.com/v1")

    def test_idempotent_upsert(self):
        seed_models(self.path, _SAMPLE_MODELS)
        seed_models(self.path, _SAMPLE_MODELS)  # must not raise or duplicate
        con = sqlite3.connect(str(self.path))
        count = con.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        con.close()
        self.assertEqual(count, 2)

    def test_empty_list_is_noop(self):
        seed_models(self.path, [])
        con = sqlite3.connect(str(self.path))
        count = con.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_returns_row_count(self):
        n = seed_models(self.path, _SAMPLE_MODELS)
        self.assertEqual(n, 2)


# ── load_models ────────────────────────────────────────────────────────────────

class TestLoadModels(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_all_enabled_models(self):
        seed_models(self.path, _SAMPLE_MODELS)
        models = load_models(self.path)
        self.assertEqual(len(models), 2)

    def test_empty_when_no_models(self):
        self.assertEqual(load_models(self.path), [])

    def test_nonexistent_db_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonexistent.db"
            self.assertEqual(load_models(path), [])

    def test_skips_disabled_models(self):
        seed_models(self.path, _SAMPLE_MODELS)
        con = sqlite3.connect(str(self.path))
        con.execute("UPDATE models SET enabled=0 WHERE alias='gemini-flash'")
        con.commit()
        con.close()
        models = load_models(self.path)
        aliases = [m["model_name"] for m in models]
        self.assertNotIn("gemini-flash", aliases)
        self.assertIn("nemotron-super", aliases)

    def test_returns_litellm_params_format(self):
        seed_models(self.path, _SAMPLE_MODELS)
        models = load_models(self.path)
        gf = next(m for m in models if m["model_name"] == "gemini-flash")
        self.assertIn("litellm_params", gf)
        params = gf["litellm_params"]
        self.assertEqual(params["model"], "gemini/gemini-3.8-flash")
        self.assertEqual(params["api_key"], "os.environ/GEMINI_API_KEY")

    def test_nvidia_includes_api_base(self):
        seed_models(self.path, _SAMPLE_MODELS)
        models = load_models(self.path)
        ns = next(m for m in models if m["model_name"] == "nemotron-super")
        self.assertEqual(
            ns["litellm_params"]["api_base"],
            "https://integrate.api.nvidia.com/v1",
        )


# ── update_model_status ────────────────────────────────────────────────────────

class TestUpdateModelStatus(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)
        seed_models(self.path, _SAMPLE_MODELS)

    def tearDown(self):
        self._tmp.cleanup()

    def test_updates_status_to_ok(self):
        update_model_status(self.path, "gemini-flash", "ok")
        con = sqlite3.connect(str(self.path))
        row = con.execute(
            "SELECT status, last_check FROM models WHERE alias='gemini-flash'"
        ).fetchone()
        con.close()
        self.assertEqual(row[0], "ok")
        self.assertIsNotNone(row[1])

    def test_updates_status_to_error(self):
        update_model_status(self.path, "gemini-flash", "error")
        con = sqlite3.connect(str(self.path))
        status = con.execute(
            "SELECT status FROM models WHERE alias='gemini-flash'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(status, "error")

    def test_unknown_alias_does_not_raise(self):
        update_model_status(self.path, "nonexistent-model", "ok")  # must not raise


# ── discover_models (via kiko_server) ─────────────────────────────────────────

class TestDiscoverModels(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)
        seed_models(self.path, _SAMPLE_MODELS)

    def tearDown(self):
        self._tmp.cleanup()

    def test_discover_marks_ok_on_success(self):
        from litellm.proxy.kiko_server import discover_models

        mock_resp = MagicMock()
        captured = {}
        async def _mock_ac(**kwargs):
            captured["max_tokens"] = kwargs.get("max_tokens")
            return mock_resp

        with patch("litellm.acompletion", side_effect=_mock_ac):
            results = asyncio.run(
                discover_models(self.path, load_models(self.path))
            )

        self.assertEqual(results.get("gemini-flash"), "ok")
        self.assertEqual(results.get("nemotron-super"), "ok")
        # Reasoning models need budget — must not be 1
        self.assertGreaterEqual(captured.get("max_tokens", 0), 50)

    def test_discover_marks_error_on_failure(self):
        from litellm.proxy.kiko_server import discover_models

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_ac:
            mock_ac.side_effect = Exception("API error")
            results = asyncio.run(
                discover_models(self.path, load_models(self.path))
            )

        self.assertEqual(results.get("gemini-flash"), "error")

    def test_discover_updates_db_status(self):
        from litellm.proxy.kiko_server import discover_models

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_ac:
            mock_ac.return_value = MagicMock()
            asyncio.run(discover_models(self.path, load_models(self.path)))

        con = sqlite3.connect(str(self.path))
        status = con.execute(
            "SELECT status FROM models WHERE alias='gemini-flash'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(status, "ok")


# ── add_model ──────────────────────────────────────────────────────────────────

class TestAddModel(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_new_model_returns_true(self):
        result = add_model(self.path, "flash", "gemini/flash", "GEMINI_KEY")
        self.assertTrue(result)

    def test_add_existing_alias_returns_false(self):
        add_model(self.path, "flash", "gemini/flash", "GEMINI_KEY")
        result = add_model(self.path, "flash", "gemini/flash-updated", "GEMINI_KEY")
        self.assertFalse(result)

    def test_add_model_visible_in_load(self):
        add_model(self.path, "flash", "gemini/flash", "GEMINI_KEY")
        models = load_models(self.path)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["model_name"], "flash")

    def test_add_model_with_api_base(self):
        add_model(self.path, "nim", "openai/nvidia/model", "NVIDIA_KEY",
                  api_base="https://integrate.api.nvidia.com/v1")
        models = load_models(self.path)
        self.assertEqual(models[0]["litellm_params"]["api_base"],
                         "https://integrate.api.nvidia.com/v1")

    def test_add_creates_db_if_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new.db"
            self.assertFalse(path.exists())
            add_model(path, "flash", "gemini/flash", "KEY")
            self.assertTrue(path.exists())

    def test_update_replaces_provider(self):
        add_model(self.path, "flash", "gemini/v1", "KEY")
        add_model(self.path, "flash", "gemini/v2", "KEY")
        models = load_models(self.path)
        self.assertEqual(models[0]["litellm_params"]["model"], "gemini/v2")


# ── delete_model ───────────────────────────────────────────────────────────────

class TestDeleteModel(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)
        seed_models(self.path, _SAMPLE_MODELS)

    def tearDown(self):
        self._tmp.cleanup()

    def test_delete_existing_returns_true(self):
        self.assertTrue(delete_model(self.path, "gemini-flash"))

    def test_delete_removes_from_db(self):
        delete_model(self.path, "gemini-flash")
        models = load_models(self.path)
        aliases = [m["model_name"] for m in models]
        self.assertNotIn("gemini-flash", aliases)

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(delete_model(self.path, "does-not-exist"))

    def test_delete_missing_db_returns_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.db"
            self.assertFalse(delete_model(path, "any"))


# ── set_model_enabled ──────────────────────────────────────────────────────────

class TestSetModelEnabled(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)
        seed_models(self.path, _SAMPLE_MODELS)

    def tearDown(self):
        self._tmp.cleanup()

    def test_disable_returns_true_if_found(self):
        self.assertTrue(set_model_enabled(self.path, "gemini-flash", False))

    def test_disable_hides_from_load(self):
        set_model_enabled(self.path, "gemini-flash", False)
        aliases = [m["model_name"] for m in load_models(self.path)]
        self.assertNotIn("gemini-flash", aliases)

    def test_enable_restores_visibility(self):
        set_model_enabled(self.path, "gemini-flash", False)
        set_model_enabled(self.path, "gemini-flash", True)
        aliases = [m["model_name"] for m in load_models(self.path)]
        self.assertIn("gemini-flash", aliases)

    def test_unknown_alias_returns_false(self):
        self.assertFalse(set_model_enabled(self.path, "ghost", True))

    def test_missing_db_returns_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.db"
            self.assertFalse(set_model_enabled(path, "any", True))


# ── get_billing_summary ────────────────────────────────────────────────────────

class TestGetBillingSummary(unittest.TestCase):
    def setUp(self):
        import tempfile, sqlite3
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        _init_db(self.path)
        # Seed some billing rows
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        con = sqlite3.connect(str(self.path))
        con.executemany(
            "INSERT INTO billing(model,tokens_in,tokens_out,cost_usd,ts) VALUES(?,?,?,?,?)",
            [
                ("gemini-flash", 100, 50, 0.001, now),
                ("gemini-flash", 200, 80, 0.002, now),
                ("nemotron",     300, 120, 0.010, now),
                ("old-model",    500, 200, 0.050, old),  # outside 30-day window
            ],
        )
        con.commit()
        con.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_recent_rows(self):
        rows = get_billing_summary(self.path, days=30)
        aliases = [r["model"] for r in rows]
        self.assertIn("gemini-flash", aliases)
        self.assertIn("nemotron", aliases)

    def test_excludes_old_rows(self):
        rows = get_billing_summary(self.path, days=30)
        aliases = [r["model"] for r in rows]
        self.assertNotIn("old-model", aliases)

    def test_aggregates_correctly(self):
        rows = get_billing_summary(self.path, days=30)
        gf = next(r for r in rows if r["model"] == "gemini-flash")
        self.assertEqual(gf["calls"], 2)
        self.assertEqual(gf["tokens_in"], 300)
        self.assertAlmostEqual(gf["cost_usd"], 0.003, places=6)

    def test_ordered_by_cost_desc(self):
        rows = get_billing_summary(self.path, days=30)
        costs = [r["cost_usd"] for r in rows]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_empty_db_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.db"
            self.assertEqual(get_billing_summary(path, days=30), [])

    def test_days_zero_returns_empty(self):
        rows = get_billing_summary(self.path, days=0)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
