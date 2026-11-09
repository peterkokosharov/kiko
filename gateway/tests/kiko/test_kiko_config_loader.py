# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Tests for kiko_server._load_config — Phase 3: config-loader integration.
Covers billing knobs in general_settings and 'kiko_sqlite' alias resolution.
"""
import textwrap
from pathlib import Path

import litellm
import pytest


@pytest.fixture(autouse=True)
def _reset_litellm_state(tmp_path, monkeypatch):
    """
    Restore litellm globals and kiko_server globals after each test.
    Points KIKO_DB_PATH at a fresh temp dir so tests never read the real
    ~/.kircho/kircho.db (which may contain seeded models from smoke tests).
    """
    import litellm.proxy.kiko_server as ks

    # Isolate DB from the real home DB — empty path means load_models returns []
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "isolated_test.db"))

    original_callbacks = list(litellm.callbacks)
    original_model_list = list(litellm.model_list or [])
    yield
    litellm.callbacks = original_callbacks
    litellm.model_list = original_model_list
    # Reset module-level billing state between tests
    ks._billing_enabled = True
    ks._billing_db_path = None
    ks._monthly_cap_usd = None
    from litellm.proxy.integrations.kiko_sqlite_logger import instance as _orig
    ks._billing_logger = _orig


def _write_yaml(tmp_path: Path, text: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return str(p)


# ── model_list ────────────────────────────────────────────────────────────────

def test_model_list_loaded(tmp_path, monkeypatch):
    """DB models are loaded into _model_list; YAML model_list is ignored."""
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db, seed_models

    db = tmp_path / "test_models.db"
    monkeypatch.setenv("KIKO_DB_PATH", str(db))
    _init_db(db)
    seed_models(db, [
        {"model_name": "test-model", "litellm_params": {"model": "gpt-4o", "api_key": "os.environ/X"}},
    ])

    # YAML model_list is present but must be ignored now that DB has models
    cfg = _write_yaml(tmp_path, """
        model_list:
          - model_name: yaml-only-model
            litellm_params:
              model: gpt-4o
    """)
    ks._load_config(cfg)
    aliases = [m["model_name"] for m in ks._model_list]
    assert "test-model" in aliases
    assert "yaml-only-model" not in aliases


# ── disable_billing ───────────────────────────────────────────────────────────

def test_disable_billing_flag(tmp_path):
    import litellm.proxy.kiko_server as ks

    cfg = _write_yaml(tmp_path, """
        model_list: []
        general_settings:
          disable_billing: true
    """)
    ks._load_config(cfg)
    assert ks._billing_enabled is False


def test_billing_enabled_by_default(tmp_path):
    import litellm.proxy.kiko_server as ks

    cfg = _write_yaml(tmp_path, """
        model_list: []
    """)
    ks._load_config(cfg)
    assert ks._billing_enabled is True


# ── custom billing_db ─────────────────────────────────────────────────────────

def test_custom_billing_db(tmp_path):
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger

    db_path = tmp_path / "custom.db"
    cfg = _write_yaml(tmp_path, f"""
        model_list: []
        general_settings:
          billing_db: "{str(db_path).replace(chr(92), '/')}"
    """)
    ks._load_config(cfg)
    assert isinstance(ks._billing_logger, KikoSQLiteLogger)
    assert ks._billing_logger.db_path == db_path
    assert db_path.exists()


# ── kiko_sqlite alias in success_callback ────────────────────────────────────

def test_kiko_sqlite_alias_enables_billing(tmp_path):
    import litellm.proxy.kiko_server as ks

    # Start with billing disabled, then re-enable via alias
    ks._billing_enabled = False
    cfg = _write_yaml(tmp_path, """
        model_list: []
        litellm_settings:
          success_callback:
            - kiko_sqlite
    """)
    ks._load_config(cfg)
    assert ks._billing_enabled is True


def test_other_callbacks_passed_through(tmp_path):
    import litellm.proxy.kiko_server as ks

    cfg = _write_yaml(tmp_path, """
        model_list: []
        litellm_settings:
          success_callback:
            - kiko_sqlite
            - langfuse
    """)
    ks._load_config(cfg)
    # "langfuse" should be set on litellm.success_callback; "kiko_sqlite" removed
    sc = getattr(litellm, "success_callback", []) or []
    assert "kiko_sqlite" not in sc
    assert "langfuse" in sc


# ── DB-first model loading ────────────────────────────────────────────────────

def test_db_models_take_priority_over_yaml(tmp_path, monkeypatch):
    """When DB has models, _load_config ignores YAML model_list."""
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db, seed_models

    db = tmp_path / "kiko_priority.db"
    monkeypatch.setenv("KIKO_DB_PATH", str(db))
    _init_db(db)
    seed_models(db, [
        {"model_name": "db-model", "litellm_params": {"model": "gemini/db", "api_key": "os.environ/X"}},
    ])

    cfg = _write_yaml(tmp_path, """
        model_list:
          - model_name: yaml-model
            litellm_params:
              model: gpt-4o
    """)
    ks._load_config(cfg)

    aliases = [m["model_name"] for m in ks._model_list]
    assert "db-model" in aliases
    assert "yaml-model" not in aliases


def test_yaml_model_list_ignored_when_db_empty(tmp_path, monkeypatch):
    """When DB is empty (or missing), _load_config does NOT fall back to YAML.
    The allowlist is enforced from the DB only; YAML model_list is ignored."""
    import litellm.proxy.kiko_server as ks

    # KIKO_DB_PATH points to a nonexistent file → load_models returns []
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "empty.db"))

    cfg = _write_yaml(tmp_path, """
        model_list:
          - model_name: yaml-only-model
            litellm_params:
              model: gpt-4o
    """)
    ks._load_config(cfg)

    # _model_list must be empty — YAML fallback is gone
    assert ks._model_list == []
    # _any_models_exist must be False — no DB rows at all
    assert ks._any_models_exist is False
