# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Security regression tests: _prepare() bypass via completions/embeddings/fallbacks.

Tests:
  SEC1 — /v1/completions: client-supplied api_base must not reach the upstream
  SEC2 — /v1/embeddings: client-supplied api_base must not reach the upstream
  SEC3 — /v1/chat/completions: fallbacks key must not reach the upstream
  SEC4 — /v1/completions: unknown model returns 404

All tests are offline (no real API calls or keys required).
"""
import unittest.mock

import httpx
import pytest
import litellm

pytestmark = [pytest.mark.asyncio(loop_scope="session")]

_ATTACKER_URL = "http://evil-attacker.example.com"


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# SEC1 — /v1/completions must strip client-supplied api_base
# ---------------------------------------------------------------------------

async def test_completions_strips_api_base(mock_backend, monkeypatch):
    """
    Client sends api_base pointing to an attacker URL in the completions body.
    _prepare() must strip it; the server-configured api_base must win.
    """
    captured_kwargs: list[dict] = []

    async def _fake_atext_completion(**kwargs):
        captured_kwargs.append(dict(kwargs))
        # Return a minimal mock response
        resp = unittest.mock.MagicMock()
        resp.model_dump.return_value = {
            "id": "cmpl-mock",
            "object": "text_completion",
            "choices": [{"text": "ok", "finish_reason": "stop"}],
        }
        return resp

    monkeypatch.setattr(litellm, "atext_completion", _fake_atext_completion)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/completions",
            headers=_headers(mock_backend["key"]),
            json={
                "model": "mock-model",
                "prompt": "hi",
                "api_base": _ATTACKER_URL,
            },
        )

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert captured_kwargs, "atext_completion was never called"
    kw = captured_kwargs[0]
    assert kw.get("api_base") != _ATTACKER_URL, (
        f"api_base leaked to upstream: got {kw.get('api_base')!r}, "
        f"expected NOT {_ATTACKER_URL!r}"
    )


# ---------------------------------------------------------------------------
# SEC2 — /v1/embeddings must strip client-supplied api_base
# ---------------------------------------------------------------------------

async def test_embeddings_strips_api_base(mock_backend, monkeypatch):
    """
    Client sends api_base pointing to an attacker URL in the embeddings body.
    _prepare() must strip it; the server-configured api_base must win.
    """
    captured_kwargs: list[dict] = []

    async def _fake_aembedding(**kwargs):
        captured_kwargs.append(dict(kwargs))
        resp = unittest.mock.MagicMock()
        resp.model_dump.return_value = {
            "object": "list",
            "data": [{"embedding": [0.1, 0.2], "index": 0}],
        }
        return resp

    monkeypatch.setattr(litellm, "aembedding", _fake_aembedding)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/embeddings",
            headers=_headers(mock_backend["key"]),
            json={
                "model": "mock-model",
                "input": "hello",
                "api_base": _ATTACKER_URL,
            },
        )

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert captured_kwargs, "aembedding was never called"
    kw = captured_kwargs[0]
    assert kw.get("api_base") != _ATTACKER_URL, (
        f"api_base leaked to upstream: got {kw.get('api_base')!r}, "
        f"expected NOT {_ATTACKER_URL!r}"
    )


# ---------------------------------------------------------------------------
# SEC3 — /v1/chat/completions must strip fallbacks from client body
# ---------------------------------------------------------------------------

async def test_fallbacks_stripped(mock_backend):
    """
    Client sends fallbacks=[{"model":"other","api_base":"http://attacker"}]
    in a chat completion request.  The upstream must NOT see the fallbacks key.
    """
    # Clear previous captures
    mock_backend["captured"].clear()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers=_headers(mock_backend["key"]),
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "hi"}],
                "fallbacks": [
                    {"model": "other", "api_base": _ATTACKER_URL}
                ],
            },
        )

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert mock_backend["captured"], "No request reached the mock upstream"
    upstream_body = mock_backend["captured"][-1]
    assert "fallbacks" not in upstream_body, (
        "fallbacks key was forwarded to the upstream — security bypass!"
    )


# ---------------------------------------------------------------------------
# SEC4 — /v1/completions with unknown model must return 404
# ---------------------------------------------------------------------------

async def test_completions_unknown_model(mock_backend):
    """
    POST /v1/completions with a model not in model_list must return 404.
    The 404 guard in _prepare() must apply to the completions route.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/completions",
            headers=_headers(mock_backend["key"]),
            json={
                "model": "does-not-exist",
                "prompt": "hi",
            },
        )

    assert r.status_code == 404, (
        f"Expected 404 for unknown model, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# SEC5 — empty model list → 404 "No models configured"
# ---------------------------------------------------------------------------

async def test_empty_model_list_returns_404(monkeypatch, tmp_path):
    """
    When _model_list is empty and no models exist in the DB (_any_models_exist=False),
    _prepare() must raise 404 with detail "No models configured".
    """
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger, _init_db

    _init_db(tmp_path / "empty.db")
    monkeypatch.setenv("MASTER_KEY", "sk-mock-test")
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setattr(ks, "_model_list", [])
    monkeypatch.setattr(ks, "_any_models_exist", False)
    monkeypatch.setattr(ks, "_billing_db_path", None)

    ks._cached_app = None
    app = ks.app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers=_headers("sk-mock-test"),
            json={"model": "any-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    ks._cached_app = None
    assert r.status_code == 404, (
        f"Expected 404 for empty model list, got {r.status_code}: {r.text}"
    )
    assert "No models configured" in r.text, (
        f"Expected 'No models configured' in detail, got: {r.text}"
    )


# ---------------------------------------------------------------------------
# SEC6 — all models disabled → 503 "No models available"
# ---------------------------------------------------------------------------

async def test_all_models_disabled_returns_503(monkeypatch, tmp_path):
    """
    When _model_list is empty but _any_models_exist=True (models exist in DB
    but all are disabled), _prepare() must raise 503 "No models available".
    """
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db

    _init_db(tmp_path / "disabled.db")
    monkeypatch.setenv("MASTER_KEY", "sk-mock-test")
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "disabled.db"))
    monkeypatch.setattr(ks, "_model_list", [])
    monkeypatch.setattr(ks, "_any_models_exist", True)
    monkeypatch.setattr(ks, "_billing_db_path", None)

    ks._cached_app = None
    app = ks.app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers=_headers("sk-mock-test"),
            json={"model": "any-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    ks._cached_app = None
    assert r.status_code == 503, (
        f"Expected 503 for all-disabled models, got {r.status_code}: {r.text}"
    )
    assert "No models available" in r.text, (
        f"Expected 'No models available' in detail, got: {r.text}"
    )


# ---------------------------------------------------------------------------
# SEC7 — valid model in populated list → passes through (not blocked)
# ---------------------------------------------------------------------------

async def test_valid_model_passes_allowlist(mock_backend, monkeypatch):
    """
    When _model_list is populated and the requested model is present,
    _prepare() must NOT raise; the request must reach the upstream.
    """
    import unittest.mock
    import litellm

    captured_kwargs: list[dict] = []

    async def _fake_acompletion(**kwargs):
        captured_kwargs.append(dict(kwargs))
        resp = unittest.mock.MagicMock()
        resp.model_dump.return_value = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        return resp

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            headers=_headers(mock_backend["key"]),
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert r.status_code == 200, (
        f"Expected 200 for valid model, got {r.status_code}: {r.text}"
    )
    assert captured_kwargs, "acompletion was never called — request was blocked"
