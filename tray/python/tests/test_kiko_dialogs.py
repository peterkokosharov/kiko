# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""
Unit tests for tray/dialogs — logic layer (D1–D10+).

No GTK, no display, no network required.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tray.config import Settings
from tray.control import KikoControl
from tray.dialogs.about import AboutData, get_about
from tray.dialogs.control import ControlResult, restart_kiko, start_kiko, stop_kiko
from tray.dialogs.costs import CostData, get_costs
from tray.dialogs.keys import KeyEntry, delete_key, list_keys, set_key
from tray.dialogs.roles import clear_role, get_env_lines, get_roles_data, set_role
from tray.dialogs.settings import get_settings, save_settings
from tray.dialogs.status import StatusData, get_status


def _make_settings(port: int = 14099) -> Settings:
    return Settings(
        config_path=Path("/nonexistent/kiko-config.yaml"),
        port=port,
        kiko_executable="kiko-nonexistent-binary",
    )


# ─── D1: get_status() returns running=False when port not listening ────────────

class TestStatusNotRunning(unittest.TestCase):

    def test_d1_not_running(self):
        settings = _make_settings()
        control = KikoControl()
        # Patch is_running to always return False
        with patch.object(control, "is_running", return_value=False):
            data = get_status(settings, control)
        self.assertIsInstance(data, StatusData)
        self.assertFalse(data.running)
        self.assertIsNone(data.uptime_seconds)
        self.assertIsNone(data.version)
        self.assertEqual(data.port, settings.port)


# ─── D2: get_status() returns running=True when port listening ─────────────────

class TestStatusRunning(unittest.TestCase):

    def test_d2_running(self):
        settings = _make_settings()
        control = KikoControl()
        control.started_at = time.monotonic() - 10.0  # pretend started 10 s ago
        with patch.object(control, "is_running", return_value=True):
            # Patch _get_version so subprocess is not actually called
            with patch("tray.dialogs.status._get_version", return_value="0.3.0"):
                data = get_status(settings, control)
        self.assertTrue(data.running)
        self.assertIsNotNone(data.uptime_seconds)
        self.assertGreaterEqual(data.uptime_seconds, 9.0)
        self.assertEqual(data.version, "0.3.0")


# ─── D3: start_kiko() with missing executable → ControlResult(success=False) ──

class TestControlStartMissing(unittest.TestCase):

    def test_d3_missing_executable(self):
        settings = _make_settings()
        control = KikoControl()
        # is_running returns False so start() proceeds; shutil.which returns None
        with patch.object(control, "is_running", return_value=False):
            with patch("tray.control.shutil.which", return_value=None):
                result = start_kiko(settings, control)
        self.assertIsInstance(result, ControlResult)
        self.assertFalse(result.success)
        self.assertIn("not found", result.message)


# ─── D4: restart_kiko() calls stop then start ─────────────────────────────────

class TestControlRestart(unittest.TestCase):

    def test_d4_restart_calls_stop_then_start(self):
        settings = _make_settings()
        control = KikoControl()
        call_order: list[str] = []

        def fake_stop():
            call_order.append("stop")

        def fake_start(s):
            call_order.append("start")

        with patch.object(control, "stop", side_effect=fake_stop):
            with patch.object(control, "start", side_effect=fake_start):
                with patch.object(control, "is_running", return_value=False):
                    result = restart_kiko(settings, control)

        self.assertEqual(call_order, ["stop", "start"])
        self.assertTrue(result.success)


# ─── D5: get_roles_data() includes all 5 roles ────────────────────────────────

class TestRolesData(unittest.TestCase):

    def test_d5_all_roles_present(self):
        with tempfile.TemporaryDirectory() as td:
            # Point roles_path to temp dir so no real file is modified
            fake_path = Path(td) / "roles.json"
            with patch("tray.roles.roles_path", return_value=fake_path):
                data = get_roles_data()
        self.assertIn("assignments", data)
        assignments = data["assignments"]
        for role in ("laborer", "workhorse", "thinker", "auditor", "subagent"):
            self.assertIn(role, assignments, f"Role '{role}' missing from assignments")
        self.assertEqual(len(data["roles"]), 5)


# ─── D6: set_role("workhorse", "my-model") → env line includes correct var ────

class TestRolesSetAndEnvLines(unittest.TestCase):

    def test_d6_set_role_env_line(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = Path(td) / "roles.json"
            with patch("tray.roles.roles_path", return_value=fake_path):
                set_role("workhorse", "my-model")
                lines = get_env_lines()
        self.assertTrue(
            any("ANTHROPIC_DEFAULT_SONNET_MODEL=my-model" in line for line in lines),
            f"Expected ANTHROPIC_DEFAULT_SONNET_MODEL=my-model in {lines}",
        )


# ─── D7: get_costs() with empty DB → today=0, month=0, cap_status="ok" ────────

class TestCostsEmptyDB(unittest.TestCase):

    def test_d7_empty_db(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "billing.db"
            data = get_costs(db_path=db, cap_usd=None)
        self.assertIsInstance(data, CostData)
        self.assertEqual(data.today_usd, 0.0)
        self.assertEqual(data.month_usd, 0.0)
        self.assertEqual(data.cap_status, "ok")
        self.assertEqual(data.per_model, [])


# ─── D8: get_costs() with month > 80% of cap → cap_status="warning" ──────────

class TestCostsWarning(unittest.TestCase):

    def _seed_db(self, db: Path, usd: float) -> None:
        today = __import__("datetime").date.today().isoformat()
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE IF NOT EXISTS billing "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, "
            "tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, "
            "cost_usd REAL NOT NULL DEFAULT 0.0, ts TEXT NOT NULL, route TEXT)"
        )
        con.execute(
            "INSERT INTO billing (model, tokens_in, tokens_out, cost_usd, ts, route) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("gpt-4", 100, 50, usd, f"{today}T12:00:00", None),
        )
        con.commit()
        con.close()

    def test_d8_warning(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "billing.db"
            self._seed_db(db, 85.0)
            data = get_costs(db_path=db, cap_usd=100.0)
        self.assertEqual(data.cap_status, "warning")
        self.assertAlmostEqual(data.month_usd, 85.0)


# ─── D9: get_costs() with month >= cap → cap_status="exceeded" ───────────────

class TestCostsExceeded(unittest.TestCase):

    def _seed_db(self, db: Path, usd: float) -> None:
        today = __import__("datetime").date.today().isoformat()
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE IF NOT EXISTS billing "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, "
            "tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, "
            "cost_usd REAL NOT NULL DEFAULT 0.0, ts TEXT NOT NULL, route TEXT)"
        )
        con.execute(
            "INSERT INTO billing (model, tokens_in, tokens_out, cost_usd, ts, route) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("gpt-4", 100, 50, usd, f"{today}T12:00:00", None),
        )
        con.commit()
        con.close()

    def test_d9_exceeded(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "billing.db"
            self._seed_db(db, 100.0)
            data = get_costs(db_path=db, cap_usd=100.0)
        self.assertEqual(data.cap_status, "exceeded")

    def test_d9b_exceeded_over_cap(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "billing.db"
            self._seed_db(db, 120.0)
            data = get_costs(db_path=db, cap_usd=100.0)
        self.assertEqual(data.cap_status, "exceeded")


# ─── D10: list_keys() returns entries with exists=True/False; no key values ────

class TestKeysList(unittest.TestCase):

    def test_d10_keys_exist_and_absent(self):
        with tempfile.TemporaryDirectory() as td:
            keys_dir = Path(td)
            # Write an anthropic key file
            set_key("anthropic", "sk-test-DEADBEEF", keys_dir=keys_dir)
            # gemini and openai are absent
            # Patch env vars so they are not picked up from the environment
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")}
            with patch.dict(os.environ, clean_env, clear=True):
                entries = list_keys(keys_dir=keys_dir)

        self.assertEqual(len(entries), 3)
        by_provider = {e.provider: e for e in entries}

        # anthropic key should exist
        self.assertTrue(by_provider["anthropic"].exists)
        self.assertEqual(by_provider["anthropic"].source, "file")

        # gemini and openai should not exist
        self.assertFalse(by_provider["gemini"].exists)
        self.assertFalse(by_provider["openai"].exists)

        # Verify no key values in any entry field
        for entry in entries:
            for attr in vars(entry).values():
                if isinstance(attr, str):
                    self.assertNotIn("sk-test", attr,
                                     "Key value must not appear in KeyEntry fields")

    def test_d10b_delete_key(self):
        with tempfile.TemporaryDirectory() as td:
            keys_dir = Path(td)
            set_key("openai", "sk-openai-test", keys_dir=keys_dir)
            # Confirm it exists
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")}
            with patch.dict(os.environ, clean_env, clear=True):
                before = list_keys(keys_dir=keys_dir)
            self.assertTrue({e.provider: e for e in before}["openai"].exists)

            delete_key("openai", keys_dir=keys_dir)
            with patch.dict(os.environ, clean_env, clear=True):
                after = list_keys(keys_dir=keys_dir)
            self.assertFalse({e.provider: e for e in after}["openai"].exists)


# ─── About ─────────────────────────────────────────────────────────────────────

class TestAbout(unittest.TestCase):

    def test_about_data(self):
        data = get_about()
        self.assertIsInstance(data, AboutData)
        self.assertEqual(data.version, "0.3.0")
        self.assertEqual(data.author, "Peter Kokosharov")
        self.assertIn("MIT", data.license_name)
        self.assertIn("LiteLLM", data.litellm_credit)


# ─── Settings save/load ────────────────────────────────────────────────────────

class TestSettings(unittest.TestCase):

    def test_save_settings_writes_json(self):
        with tempfile.TemporaryDirectory() as td:
            tray_json = Path(td) / "tray.json"
            s = Settings(
                config_path=Path("/tmp/cfg.yaml"),
                port=5000,
                kiko_executable="mykiko",
                pystray_backend="appindicator",
            )
            save_settings(s, tray_json=tray_json)
            data = json.loads(tray_json.read_text())
        self.assertEqual(data["port"], 5000)
        self.assertEqual(data["kiko_executable"], "mykiko")
        self.assertEqual(data["pystray_backend"], "appindicator")


if __name__ == "__main__":
    unittest.main()
