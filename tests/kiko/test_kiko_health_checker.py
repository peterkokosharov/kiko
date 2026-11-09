# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
Unit tests for Phase 2: background health checker + /health per-class endpoint.

H1 — health_check_interval is read from YAML general_settings (default 300)
H2 — GET /health returns {"status": "healthy", "classes": {...}} structure
H3 — background health loop logs exceptions and keeps running
"""
import asyncio
import sqlite3
import unittest.mock

import httpx
import pytest

_ASYNC_MARK = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_UPSTREAM = "http://mock-health-api/v1"


def _make_app(db_path, monkeypatch):
    """Create an isolated kiko app pointing to db_path."""
    import litellm
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger

    monkeypatch.setenv("MASTER_KEY", "sk-health-test")
    monkeypatch.setenv("KIKO_DB_PATH", str(db_path))
    monkeypatch.setattr(ks, "_billing_db_path", None)
    monkeypatch.setattr(
        ks,
        "_model_list",
        [
            {
                "model_name": "mock-model",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "sk-mock-key",
                    "api_base": _MOCK_UPSTREAM,
                },
            }
        ],
    )

    import litellm
    _orig_use_chat = litellm.use_chat_completions_url_for_anthropic_messages
    litellm.use_chat_completions_url_for_anthropic_messages = True

    logger = KikoSQLiteLogger(db_path=db_path)
    _orig_callbacks = list(litellm.callbacks)
    litellm.callbacks = [logger]

    app = ks.create_app()

    yield app

    litellm.callbacks[:] = _orig_callbacks
    litellm.use_chat_completions_url_for_anthropic_messages = _orig_use_chat


# ---------------------------------------------------------------------------
# H1 — health_check_interval is read from _config (default 300 when missing)
# ---------------------------------------------------------------------------

def test_interval_default(monkeypatch):
    """When general_settings has no health_check_interval, _get_health_interval returns 300."""
    import litellm.proxy.kiko_server as ks
    monkeypatch.setattr(ks, "_config", {})
    assert ks._get_health_interval() == 300


def test_interval_from_config(monkeypatch):
    """health_check_interval set in YAML general_settings is returned by _get_health_interval."""
    import litellm.proxy.kiko_server as ks
    monkeypatch.setattr(ks, "_config", {"general_settings": {"health_check_interval": 60}})
    assert ks._get_health_interval() == 60


def test_interval_inf_returns_fallback(monkeypatch):
    """health_check_interval: .inf (parsed as float('inf')) must return 300, not raise OverflowError."""
    import litellm.proxy.kiko_server as ks
    monkeypatch.setattr(ks, "_config", {"general_settings": {"health_check_interval": float("inf")}})
    assert ks._get_health_interval() == 300


# ---------------------------------------------------------------------------
# H2 — GET /health returns {"status": "healthy", "classes": {...}}
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_response_format(monkeypatch, tmp_path):
    """GET /health returns status + classes dict with alive/total per class."""
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db

    db_path = tmp_path / "health_test.db"
    _init_db(db_path)

    # Seed two models into the DB: one alive SONNET, one errored OPUS.
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT OR REPLACE INTO models(alias, provider, api_base, api_key_env, model_class, priority, enabled, status)"
        " VALUES ('m-sonnet', 'openai', 'http://x', '', 'SONNET', 0, 1, 'ok')"
    )
    con.execute(
        "INSERT OR REPLACE INTO models(alias, provider, api_base, api_key_env, model_class, priority, enabled, status)"
        " VALUES ('m-opus', 'openai', 'http://x', '', 'OPUS', 0, 1, 'error')"
    )
    con.commit()
    con.close()

    import litellm
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger

    monkeypatch.setenv("MASTER_KEY", "sk-health-test")
    monkeypatch.setenv("KIKO_DB_PATH", str(db_path))
    monkeypatch.setattr(ks, "_billing_db_path", None)
    monkeypatch.setattr(ks, "_model_list", [
        {
            "model_name": "mock-model",
            "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-k", "api_base": _MOCK_UPSTREAM},
        }
    ])
    _orig_use_chat = litellm.use_chat_completions_url_for_anthropic_messages
    litellm.use_chat_completions_url_for_anthropic_messages = True
    logger = KikoSQLiteLogger(db_path=db_path)
    _orig_callbacks = list(litellm.callbacks)
    litellm.callbacks = [logger]

    try:
        app = ks.create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r = await client.get("/health")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "healthy"
        assert "classes" in data
        classes = data["classes"]
        # SONNET: 1 alive of 1 total
        assert "SONNET" in classes
        assert classes["SONNET"]["alive"] == 1
        assert classes["SONNET"]["total"] == 1
        # OPUS: 0 alive of 1 total
        assert "OPUS" in classes
        assert classes["OPUS"]["alive"] == 0
        assert classes["OPUS"]["total"] == 1
    finally:
        litellm.callbacks[:] = _orig_callbacks
        litellm.use_chat_completions_url_for_anthropic_messages = _orig_use_chat


# ---------------------------------------------------------------------------
# H3 — background health loop logs exceptions and keeps running
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_loop_exception_handling(monkeypatch, tmp_path):
    """When discover_models raises, _run_health_check catches the exception and logs it."""
    import litellm.proxy.kiko_server as ks

    # Guard the module-global so this test cannot leak state.
    monkeypatch.setattr(ks, "_model_list", ks._model_list)

    call_count = 0
    logged_warnings = []

    async def _boom(_db, _ml):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated failure")

    # Patch discover_models to always raise.
    monkeypatch.setattr(ks, "discover_models", _boom)
    # Patch _kiko_log to capture warnings.
    monkeypatch.setattr(ks._kiko_log, "warning", lambda msg, exc: logged_warnings.append(str(exc)))

    db_path = tmp_path / "loop_test.db"
    monkeypatch.setenv("KIKO_DB_PATH", str(db_path))

    # _run_health_check must not propagate the exception.
    try:
        await ks._run_health_check(db_path)
    except RuntimeError:
        pytest.fail("exception escaped _run_health_check")

    assert call_count == 1, "discover_models should have been called once"
    assert len(logged_warnings) == 1
    assert "simulated failure" in logged_warnings[0]


# ---------------------------------------------------------------------------
# H4 — health check pings do NOT write billing rows (metadata flag)
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_check_ping_not_billed(tmp_path):
    """async_log_success_event must not insert a billing row when
    metadata["kiko_health_check"] is True — even after litellm's function_setup
    has copied the logger into litellm.success_callback permanently."""
    import sqlite3 as _sqlite3
    import litellm
    from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger

    db = tmp_path / "billing.db"
    logger = KikoSQLiteLogger(db_path=db)

    # Simulate post-first-request state: logger is registered in BOTH lists.
    orig_callbacks = list(litellm.callbacks)
    orig_success = list(litellm.success_callback)
    litellm.callbacks = [logger]
    litellm.success_callback = [logger]
    try:
        # Call the logger directly with health-check metadata.
        await logger.async_log_success_event(
            kwargs={
                "litellm_params": {"metadata": {"kiko_health_check": True}},
                "model": "test-model",
                "response_cost": 0.001,
            },
            response_obj=None,
            start_time=None,
            end_time=None,
        )

        # No billing row must have been written.
        con = _sqlite3.connect(str(db))
        rows = con.execute("SELECT COUNT(*) FROM billing").fetchone()
        con.close()
        assert rows[0] == 0, "health check ping must not write a billing row"
    finally:
        litellm.callbacks = orig_callbacks
        litellm.success_callback = orig_success


# ---------------------------------------------------------------------------
# H5 — health check re-pings models with status='error'
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_check_rechecks_error_model(monkeypatch, tmp_path):
    """get_all_enabled_aliases includes models with status='error', so _run_health_check re-pings them."""
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db

    db_path = tmp_path / "recheck_test.db"
    _init_db(db_path)

    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT OR REPLACE INTO models"
        "(alias, provider, api_base, api_key_env, model_class, priority, enabled, status)"
        " VALUES ('err-model', 'openai/gpt-4o', 'http://x', 'KEY', 'SONNET', 0, 1, 'error')"
    )
    con.commit()
    con.close()

    pinged = []

    async def _mock_discover(db, model_list):
        for m in model_list:
            pinged.append(m["model_name"])

    monkeypatch.setattr(ks, "discover_models", _mock_discover)
    monkeypatch.setattr(ks, "_model_list", ks._model_list)  # guard global leak

    await ks._run_health_check(db_path)

    assert "err-model" in pinged, "error model must be re-pinged by health check"


# ---------------------------------------------------------------------------
# H6 — health check passes metadata={"kiko_health_check": True} to litellm
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_check_passes_metadata_flag(monkeypatch, tmp_path):
    """discover_models passes metadata={"kiko_health_check": True} to litellm.acompletion."""
    import litellm
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db

    db_path = tmp_path / "metadata_test.db"
    _init_db(db_path)

    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT OR REPLACE INTO models"
        "(alias, provider, api_base, api_key_env, model_class, priority, enabled, status)"
        " VALUES ('meta-model', 'openai/gpt-4o', 'http://x', 'KEY', 'SONNET', 0, 1, 'ok')"
    )
    con.commit()
    con.close()

    captured_kwargs = []

    async def _mock_acompletion(**kwargs):
        captured_kwargs.append(kwargs)
        return unittest.mock.MagicMock()

    monkeypatch.setattr(litellm, "acompletion", _mock_acompletion)

    model_list = ks.get_all_enabled_aliases(db_path)
    await ks.discover_models(db_path, model_list)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("metadata") == {"kiko_health_check": True}


# ---------------------------------------------------------------------------
# H7 — health loop survives exception in load_models
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_loop_survives_load_models_exception(monkeypatch, tmp_path):
    """When load_models raises on the first call, _run_health_check catches it;
    a second call succeeds — the loop never dies."""
    import litellm.proxy.kiko_server as ks

    call_count = 0

    def _flaky_load_models(db):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("no such table: models")
        return []

    async def _noop_discover(db, ml):
        pass

    monkeypatch.setattr(ks, "load_models", _flaky_load_models)
    monkeypatch.setattr(ks, "discover_models", _noop_discover)
    monkeypatch.setattr(ks, "get_all_enabled_aliases", lambda _db: [])
    monkeypatch.setattr(ks, "_model_list", ks._model_list)  # guard global leak

    db_path = tmp_path / "loop_survive.db"

    try:
        await ks._run_health_check(db_path)
    except Exception:
        pytest.fail("first call must not propagate exception")

    try:
        await ks._run_health_check(db_path)
    except Exception:
        pytest.fail("second call must not propagate exception")

    assert call_count == 2, f"load_models must have been called twice, got {call_count}"


# ---------------------------------------------------------------------------
# H8 — cancelling _run_health_check stops it cleanly (no hang)
# ---------------------------------------------------------------------------

@_ASYNC_MARK
async def test_health_check_clean_cancel(monkeypatch, tmp_path):
    """Cancelling an in-progress _run_health_check task raises CancelledError cleanly."""
    import litellm.proxy.kiko_server as ks

    async def _hanging_discover(db, ml):
        await asyncio.sleep(60)  # will be cancelled before this completes

    monkeypatch.setattr(ks, "discover_models", _hanging_discover)
    monkeypatch.setattr(
        ks,
        "get_all_enabled_aliases",
        lambda _db: [{"model_name": "x", "litellm_params": {}}],
    )
    monkeypatch.setattr(ks, "_model_list", ks._model_list)  # guard global leak

    db_path = tmp_path / "cancel_test.db"

    task = asyncio.create_task(ks._run_health_check(db_path))
    await asyncio.sleep(0)  # yield so the task starts executing
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
