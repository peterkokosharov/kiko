# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Tests for model_class / priority schema migration.
Verifies that load_models and get_models_by_class do not crash when the DB
was created without the model_class / priority columns (pre-migration schema).
"""
import sqlite3

import pytest


def test_load_models_migration_on_old_schema(tmp_path):
    """load_models must not crash on a DB without model_class/priority columns."""
    db_file = tmp_path / "old.db"
    with sqlite3.connect(db_file) as con:
        # Old schema without model_class / priority columns.
        con.execute("""CREATE TABLE models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE NOT NULL, provider TEXT, api_base TEXT,
            api_key_env TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'unknown', last_check TEXT)""")
        con.execute(
            "INSERT INTO models (alias, provider, api_key_env) VALUES ('test-model', 'openai/gpt-4o', 'TEST_KEY')"
        )
    from litellm.proxy.integrations.kiko_sqlite_logger import load_models
    result = load_models(db_file)
    assert len(result) == 1
    entry = result[0]
    assert entry["model_name"] == "test-model"
    assert entry["model_class"] == "SONNET"
    assert entry["priority"] == 100
