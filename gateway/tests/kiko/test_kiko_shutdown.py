"""Tests for POST /kiko/shutdown."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch, MagicMock


@pytest.fixture
def _app(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "kiko.db"))
    monkeypatch.setenv("MASTER_KEY", "sk-test")
    try:
        import litellm.proxy._types as _lt
        monkeypatch.setattr(_lt, "CommonProxyAppConfig", MagicMock(), raising=False)
    except Exception:
        pass
    import litellm.proxy.kiko_server as ks
    monkeypatch.setattr(ks, "_model_list", [])
    from litellm.proxy.kiko_server import create_app
    return create_app()


@pytest_asyncio.fixture
async def client(_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app), base_url="http://testclient"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_shutdown_returns_200(client):
    """POST /kiko/shutdown returns 200 and the shutdown task is created (signal mocked)."""
    with patch("signal.raise_signal") as mock_sig:
        r = await client.post(
            "/kiko/shutdown", headers={"Authorization": "Bearer sk-test"}
        )
        import asyncio
        await asyncio.sleep(0.4)  # let the delayed raise_signal task fire while it is still patched
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "shutting down"


@pytest.mark.asyncio
async def test_shutdown_auth_required(client):
    """POST /kiko/shutdown without auth returns 401 or 403."""
    r = await client.post("/kiko/shutdown")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_shutdown_triggers_sigint(client):
    """POST /kiko/shutdown schedules signal.raise_signal(SIGINT)."""
    import signal
    import asyncio
    raised = []

    def _capture(sig):
        raised.append(sig)

    with patch("signal.raise_signal", side_effect=_capture):
        r = await client.post(
            "/kiko/shutdown", headers={"Authorization": "Bearer sk-test"}
        )
        assert r.status_code == 200
        # Give the background task time to fire (it sleeps 0.2 s)
        await asyncio.sleep(0.4)

    assert signal.SIGINT in raised, f"SIGINT not raised; got: {raised}"
