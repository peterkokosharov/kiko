"""Smoke tests: kiko starts, /health responds, routing logs model class."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
import asyncio
import os
import sqlite3

import httpx
import psutil
import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "kiko.db"))
    monkeypatch.setenv("MASTER_KEY", "sk-smoke")

    try:
        import litellm.proxy._types as _lt
        monkeypatch.setattr(_lt, "CommonProxyAppConfig", MagicMock(), raising=False)
    except Exception:
        pass

    from litellm.proxy.integrations.kiko_sqlite_logger import (
        _init_db,
        seed_models,
        Path,
    )

    db = Path(tmp_path / "kiko.db")
    _init_db(db)
    seed_models(db, [
        {"model_name": "claude-haiku-4-5", "model_class": "HAIKU", "priority": 1, "status": "ok", "enabled": 1},
        {"model_name": "claude-sonnet-4-5", "model_class": "SONNET", "priority": 1, "status": "ok", "enabled": 1},
    ])

    import litellm.proxy.kiko_server as ks

    monkeypatch.setattr(ks, "_model_list", [
        {
            "model_name": "claude-haiku-4-5",
            "litellm_params": {
                "model": "anthropic/claude-haiku-4-5",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
        {
            "model_name": "claude-sonnet-4-5",
            "litellm_params": {
                "model": "anthropic/claude-sonnet-4-5",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
    ])
    monkeypatch.setattr(ks, "_billing_db_path", None)
    ks._cached_app = None

    from litellm.proxy.kiko_server import create_app

    app = create_app()
    yield app
    ks._cached_app = None


@pytest_asyncio.fixture
async def client(_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app), base_url="http://testclient"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# S1 — /health responds
# ---------------------------------------------------------------------------

async def test_health_responds(client):
    """/health returns 200."""
    r = await client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# S1b — RAM stays flat during a burst of /health requests
# ---------------------------------------------------------------------------

async def test_ram_stays_flat(client):
    """RSS growth must be < 20 MB after 20 /health requests (no /proc, Windows-compatible)."""
    proc = psutil.Process(os.getpid())
    baseline_mb = proc.memory_info().rss / 1_048_576

    for _ in range(20):
        r = await client.get("/health")
        assert r.status_code == 200

    after_mb = proc.memory_info().rss / 1_048_576
    growth_mb = after_mb - baseline_mb
    assert growth_mb < 20, (
        f"RSS grew {growth_mb:.1f} MB (baseline={baseline_mb:.1f} MB, after={after_mb:.1f} MB) "
        "— possible memory leak"
    )


# ---------------------------------------------------------------------------
# S2 — routing logs model class and writes a billing row
# Uses the mock_backend fixture (conftest.py) which wires up respx HTTP mocks
# and registers KikoSQLiteLogger in litellm callbacks — the correct way to
# exercise the full billing pipeline without real API calls.
# ---------------------------------------------------------------------------

async def test_routing_logs_model_class(mock_backend):
    """A /v1/chat/completions call is handled and a billing row is written to the DB."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
    from litellm.proxy.integrations.kiko_sqlite_logger import get_all_models, seed_models

    # Seed the underlying provider model as an alias so the alias lookup works.
    # billing.model stores the resolved model name (e.g. "gpt-4o-mini" after litellm
    # strips the "openai/" provider prefix from "openai/gpt-4o-mini").
    db_path = mock_backend["db"]
    seed_models(db_path, [
        {"model_name": "gpt-4o-mini", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-mock-key"}},
    ])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as cli:
        r = await cli.post(
            "/v1/chat/completions",
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "ping"}],
            },
            headers={"Authorization": f"Bearer {mock_backend['key']}"},
        )
    assert r.status_code == 200, r.text

    # Flush the async billing pipeline: two hops after acompletion() returns:
    #   1. asyncio.create_task(_client_async_logging_helper) — fires from wrapper_async
    #   2. GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(...)
    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    con = sqlite3.connect(str(mock_backend["db"]))
    rows = con.execute("SELECT model FROM billing").fetchall()
    con.close()
    assert len(rows) >= 1, "No billing row written after request"

    all_models = get_all_models(db_path)
    known_aliases = {m["name"] for m in all_models}
    assert rows[0][0] in known_aliases, (
        f"billing.model={rows[0][0]!r} not in configured aliases {known_aliases}"
    )


# ---------------------------------------------------------------------------
# S3 — /kiko/models lists seeded models
# ---------------------------------------------------------------------------

async def test_kiko_models_endpoint(client):
    """GET /kiko/models returns both seeded models."""
    r = await client.get("/kiko/models", headers={"Authorization": "Bearer sk-smoke"})
    assert r.status_code == 200
    names = [m["name"] for m in r.json()["models"]]
    assert "claude-haiku-4-5" in names
    assert "claude-sonnet-4-5" in names


# ---------------------------------------------------------------------------
# S4 — /kiko/shutdown endpoint exists and returns 200
# ---------------------------------------------------------------------------

async def test_shutdown_endpoint_exists(client):
    """POST /kiko/shutdown returns 200 (signal mocked)."""
    with patch("signal.raise_signal"):
        r = await client.post(
            "/kiko/shutdown", headers={"Authorization": "Bearer sk-smoke"}
        )
        import asyncio
        await asyncio.sleep(0.4)  # let the delayed raise_signal task fire while it is still patched
    assert r.status_code == 200
