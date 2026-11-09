# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Unit tests for KikoSQLiteLogger.
No proxy needed — tests the logger class in isolation.
Uses a temp SQLite DB per test (not the real ~/.kircho/kircho.db).
"""
import asyncio
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger, get_models_by_class


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test_kircho.db"


@pytest.fixture
def logger(tmp_db):
    return KikoSQLiteLogger(db_path=tmp_db)


def _rows(db_path: Path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM billing").fetchall()]
    con.close()
    return rows


def _make_response(prompt_tokens=10, completion_tokens=20):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(usage=usage)


def _make_kwargs(model="gemini/gemini-2.0-flash-lite", cost=0.001):
    return {"model": model, "response_cost": cost}


# ---------------------------------------------------------------------------
# T1 — success event writes exactly one billing row
# ---------------------------------------------------------------------------
def test_billing_row_on_success(logger, tmp_db):
    kwargs = _make_kwargs()
    resp = _make_response()
    t = datetime.now(timezone.utc)

    asyncio.run(logger.async_log_success_event(kwargs, resp, t, t))

    rows = _rows(tmp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "gemini/gemini-2.0-flash-lite"
    assert r["tokens_in"] == 10
    assert r["tokens_out"] == 20


# ---------------------------------------------------------------------------
# T2 — failure event writes NO row
# ---------------------------------------------------------------------------
def test_no_row_on_failure(logger, tmp_db):
    t = datetime.now(timezone.utc)
    asyncio.run(logger.async_log_failure_event({}, None, t, t))
    assert _rows(tmp_db) == []


# ---------------------------------------------------------------------------
# T3 — cost_usd is always float >= 0
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cost", [None, 0, "0.0005", 0.001])
def test_cost_is_non_negative_float(logger, tmp_db, cost):
    kwargs = _make_kwargs(cost=cost)
    t = datetime.now(timezone.utc)
    asyncio.run(logger.async_log_success_event(kwargs, _make_response(), t, t))
    row = _rows(tmp_db)[0]
    assert isinstance(row["cost_usd"], float)
    assert row["cost_usd"] >= 0.0


# ---------------------------------------------------------------------------
# T4 — missing usage (None) writes zeros, not crash
# ---------------------------------------------------------------------------
def test_missing_usage_writes_zeros(logger, tmp_db):
    kwargs = _make_kwargs()
    resp = SimpleNamespace(usage=None)
    t = datetime.now(timezone.utc)
    asyncio.run(logger.async_log_success_event(kwargs, resp, t, t))
    row = _rows(tmp_db)[0]
    assert row["tokens_in"] == 0
    assert row["tokens_out"] == 0


# ---------------------------------------------------------------------------
# T5 — 10 concurrent async calls → exactly 10 rows, no corruption
# ---------------------------------------------------------------------------
def test_concurrent_writes_no_corruption(logger, tmp_db):
    async def _run_all():
        t = datetime.now(timezone.utc)
        tasks = [
            logger.async_log_success_event(
                _make_kwargs(model=f"gemini/model-{i}", cost=0.0001 * i),
                _make_response(prompt_tokens=i, completion_tokens=i * 2),
                t, t,
            )
            for i in range(10)
        ]
        await asyncio.gather(*tasks)

    asyncio.run(_run_all())
    rows = _rows(tmp_db)
    assert len(rows) == 10
    models = {r["model"] for r in rows}
    assert len(models) == 10  # each write landed as a distinct row


# ---------------------------------------------------------------------------
# T6 — DB and table are created automatically if they don't exist
# ---------------------------------------------------------------------------
def test_db_created_on_init(tmp_path):
    db = tmp_path / "new" / "kircho.db"
    assert not db.exists()
    KikoSQLiteLogger(db_path=db)
    assert db.exists()
    con = sqlite3.connect(str(db))
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    con.close()
    assert "billing" in tables


# ---------------------------------------------------------------------------
# T7–T13 — get_models_by_class routing logic
# ---------------------------------------------------------------------------

def _make_models_db(tmp_path, rows, db_name="models_test.db"):
    """Create a temp DB with models table.
    rows: list of (alias, model_class, priority, enabled, status)
    """
    db = tmp_path / db_name
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS models (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alias       TEXT UNIQUE NOT NULL,
            provider    TEXT NOT NULL DEFAULT 'test-provider',
            api_base    TEXT,
            api_key_env TEXT NOT NULL DEFAULT 'TEST_KEY',
            enabled     INTEGER NOT NULL DEFAULT 1,
            status      TEXT NOT NULL DEFAULT 'unknown',
            last_check  TEXT,
            model_class TEXT NOT NULL DEFAULT 'SONNET',
            priority    INTEGER NOT NULL DEFAULT 100
        );
    """)
    for row in rows:
        con.execute(
            "INSERT INTO models (alias, model_class, priority, enabled, status) VALUES (?,?,?,?,?)",
            row,
        )
    con.commit()
    con.close()
    return db


class TestGetModelsByClass:
    def test_ok_before_unknown(self, tmp_path):
        """T7: ok rows returned; unknown rows ignored when ok exists."""
        db = _make_models_db(tmp_path, [
            ("ok-model",      "SONNET", 1, 1, "ok"),
            ("unknown-model", "SONNET", 2, 1, "unknown"),
        ])
        result = get_models_by_class(db, "SONNET")
        assert result == ["ok-model"]

    def test_unknown_fallback(self, tmp_path):
        """T8: unknown returned when no ok rows exist."""
        db = _make_models_db(tmp_path, [
            ("unknown-model", "SONNET", 1, 1, "unknown"),
        ])
        result = get_models_by_class(db, "SONNET")
        assert result == ["unknown-model"]

    def test_error_excluded(self, tmp_path):
        """T9: error models are never returned."""
        db = _make_models_db(tmp_path, [
            ("error-model", "SONNET", 1, 1, "error"),
        ])
        result = get_models_by_class(db, "SONNET")
        assert result == []

    def test_mixed_error_and_ok(self, tmp_path):
        """T10: error rows ignored; ok rows returned."""
        db = _make_models_db(tmp_path, [
            ("ok-model",    "SONNET", 1, 1, "ok"),
            ("error-model", "SONNET", 2, 1, "error"),
        ])
        result = get_models_by_class(db, "SONNET")
        assert result == ["ok-model"]

    def test_missing_db_returns_empty(self, tmp_path):
        """T11a: non-existent DB path returns []."""
        db = tmp_path / "nonexistent.db"
        result = get_models_by_class(db, "SONNET")
        assert result == []

    def test_empty_db_returns_empty(self, tmp_path):
        """T11b: DB exists but has no rows → returns []."""
        db = _make_models_db(tmp_path, [], db_name="empty.db")
        result = get_models_by_class(db, "SONNET")
        assert result == []

    def test_priority_order(self, tmp_path):
        """T12: lower priority number first; ties broken by insertion order (id)."""
        db = _make_models_db(tmp_path, [
            ("prio-10a", "SONNET", 10, 1, "ok"),
            ("prio-5",   "SONNET",  5, 1, "ok"),
            ("prio-10b", "SONNET", 10, 1, "ok"),
        ])
        result = get_models_by_class(db, "SONNET")
        assert result == ["prio-5", "prio-10a", "prio-10b"]

    def test_class_filter(self, tmp_path):
        """T13: models of other class are not returned."""
        db = _make_models_db(tmp_path, [
            ("sonnet-model", "SONNET", 1, 1, "ok"),
            ("haiku-model",  "HAIKU",  1, 1, "ok"),
        ])
        assert get_models_by_class(db, "SONNET") == ["sonnet-model"]
        assert get_models_by_class(db, "HAIKU")  == ["haiku-model"]


class TestWALAndCache:
    def test_open_db_sets_wal(self, tmp_path):
        """_open_db enables WAL journal mode."""
        from litellm.proxy.integrations.kiko_sqlite_logger import _open_db
        con = _open_db(str(tmp_path / "test.db"))
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.close()
        assert mode == "wal"

    def test_open_db_sets_synchronous_normal(self, tmp_path):
        """_open_db sets synchronous=NORMAL (value 1)."""
        from litellm.proxy.integrations.kiko_sqlite_logger import _open_db
        con = _open_db(str(tmp_path / "test.db"))
        val = con.execute("PRAGMA synchronous").fetchone()[0]
        con.close()
        assert val == 1  # NORMAL=1

    def test_get_models_by_class_cached(self, tmp_path):
        """Second call within TTL uses cache (sqlite3.connect not called twice)."""
        import sqlite3 as _sq3
        from unittest.mock import patch as _patch
        from litellm.proxy.integrations.kiko_sqlite_logger import (
            _init_db, get_models_by_class, _models_cache, seed_models,
        )
        db = tmp_path / "c.db"
        _init_db(db)
        seed_models(db, [{"model_name": "m1", "model_class": "HAIKU", "priority": 1, "status": "ok", "enabled": 1}])
        _models_cache.clear()
        connect_calls = []
        real_connect = _sq3.connect
        def _counting_connect(*a, **kw):
            connect_calls.append(a)
            return real_connect(*a, **kw)
        with _patch.object(_sq3, "connect", side_effect=_counting_connect):
            r1 = get_models_by_class(db, "HAIKU")
            r2 = get_models_by_class(db, "HAIKU")
        assert r1 == ["m1"]
        assert r2 == ["m1"]
        assert len(connect_calls) == 1, f"Expected 1 connect, got {len(connect_calls)}"

    def test_cache_invalidated_on_status_update(self, tmp_path):
        """update_model_status clears the cache."""
        from litellm.proxy.integrations.kiko_sqlite_logger import (
            _init_db, get_models_by_class, _models_cache, seed_models, update_model_status,
        )
        db = tmp_path / "d.db"
        _init_db(db)
        seed_models(db, [{"model_name": "m2", "model_class": "SONNET", "priority": 1, "status": "ok", "enabled": 1}])
        _models_cache.clear()
        get_models_by_class(db, "SONNET")  # populate cache
        assert ("SONNET",) in [k[1:] for k in _models_cache]  # cache has an entry
        update_model_status(db, "m2", "error")
        assert len(_models_cache) == 0, "Cache should be cleared after status update"
