"""Tests for GET /kiko/models and POST /kiko/models/{name}/class."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch, MagicMock


@pytest.fixture
def _app(tmp_path, monkeypatch):
    """kiko app with a seeded DB and master key."""
    import os
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "kiko.db"))
    monkeypatch.setenv("MASTER_KEY", "sk-test")
    try:
        import litellm.proxy._types as _lt
        monkeypatch.setattr(_lt, "CommonProxyAppConfig", MagicMock(), raising=False)
    except Exception:
        pass
    from litellm.proxy.integrations.kiko_sqlite_logger import (
        _init_db, seed_models, Path,
    )
    db = Path(tmp_path / "kiko.db")
    _init_db(db)
    seed_models(db, [
        {"model_name": "claude-haiku-4-5", "model_class": "HAIKU", "priority": 1, "status": "ok", "enabled": 1},
        {"model_name": "claude-sonnet-4-5", "model_class": "SONNET", "priority": 1, "status": "ok", "enabled": 1},
    ])
    import litellm.proxy.kiko_server as ks
    monkeypatch.setattr(ks, "_model_list", [
        {"model_name": "claude-haiku-4-5", "litellm_params": {"model": "anthropic/claude-haiku-4-5", "api_key": "os.environ/ANTHROPIC_API_KEY"}},
        {"model_name": "claude-sonnet-4-5", "litellm_params": {"model": "anthropic/claude-sonnet-4-5", "api_key": "os.environ/ANTHROPIC_API_KEY"}},
    ])
    from litellm.proxy.kiko_server import create_app
    app = create_app()
    return app


@pytest_asyncio.fixture
async def client(_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://testclient") as c:
        yield c


@pytest.mark.asyncio
async def test_get_models_returns_list(client):
    r = await client.get("/kiko/models", headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200
    data = r.json()
    assert "models" in data
    names = [m["name"] for m in data["models"]]
    assert "claude-haiku-4-5" in names
    assert "claude-sonnet-4-5" in names


@pytest.mark.asyncio
async def test_get_models_has_selected_field(client):
    r = await client.get("/kiko/models", headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200
    for m in r.json()["models"]:
        assert "selected" in m
        assert "class" in m
        assert "status" in m


@pytest.mark.asyncio
async def test_get_models_auth_required(client):
    r = await client.get("/kiko/models")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_set_class_changes_model(client, _app, tmp_path, monkeypatch):
    r = await client.post(
        "/kiko/models/claude-haiku-4-5/class",
        json={"class": "SONNET"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["class"] == "SONNET"


@pytest.mark.asyncio
async def test_set_class_unknown_model_404(client):
    r = await client.post(
        "/kiko/models/does-not-exist/class",
        json={"class": "HAIKU"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_set_class_invalid_class_400(client):
    r = await client.post(
        "/kiko/models/claude-haiku-4-5/class",
        json={"class": "TURBO"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_set_class_auth_required(client):
    r = await client.post(
        "/kiko/models/claude-haiku-4-5/class",
        json={"class": "OPUS"},
    )
    assert r.status_code in (401, 403)
