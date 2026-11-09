# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Step 7 tests: count_tokens endpoint, Anthropic error shape, claude-* model aliases.

Tests:
  S7-1  count_tokens returns {"input_tokens": <int>}
  S7-2  404 error body matches Anthropic error shape (not_found_error)
  S7-3  401 error body matches Anthropic error shape (authentication_error)
  S7-4  model_alias config glob routes claude-sonnet-* to mock-claude
  S7-5  ANTHROPIC_DEFAULT_SONNET_MODEL env var routes claude-sonnet-* to mock-claude
"""
import httpx
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# S7-1  count_tokens
# ---------------------------------------------------------------------------

async def test_count_tokens_returns_input_tokens(mock_backend):
    """POST /v1/messages/count_tokens returns {"input_tokens": <int>}."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "Hello, how many tokens?"}],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "input_tokens" in data, f"Missing input_tokens key: {data}"
    assert isinstance(data["input_tokens"], int), f"input_tokens not int: {data}"


# ---------------------------------------------------------------------------
# S7-2  Anthropic error shape — 404 (unknown model)
# ---------------------------------------------------------------------------

async def test_error_shape_404(mock_backend):
    """Unknown model returns Anthropic error shape with not_found_error."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "completely-unknown-model-xyz",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 404, r.text
    data = r.json()
    assert data.get("type") == "error", f"Missing type=error: {data}"
    err = data.get("error", {})
    assert err.get("type") == "not_found_error", f"Wrong error type: {err}"
    assert isinstance(err.get("message"), str), f"Missing message: {err}"


# ---------------------------------------------------------------------------
# S7-3  Anthropic error shape — 401 (bad auth key)
# ---------------------------------------------------------------------------

async def test_error_shape_401(mock_backend):
    """Wrong API key returns Anthropic error shape with authentication_error."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_headers("sk-WRONG-KEY"),
        )

    assert r.status_code == 401, r.text
    data = r.json()
    assert data.get("type") == "error", f"Missing type=error: {data}"
    err = data.get("error", {})
    assert err.get("type") == "authentication_error", f"Wrong error type: {err}"
    assert isinstance(err.get("message"), str), f"Missing message: {err}"


# ---------------------------------------------------------------------------
# S7-4  model_aliases config glob — claude-sonnet-* → mock-claude
# ---------------------------------------------------------------------------

async def test_model_alias_claude_sonnet(mock_backend, monkeypatch):
    """
    Setting _model_aliases to {"claude-sonnet-*": "mock-claude"} causes
    model="claude-sonnet-4-5" to be resolved to "mock-claude" and routed.
    """
    import litellm.proxy.kiko_server as ks

    monkeypatch.setattr(ks, "_model_aliases", {"claude-sonnet-*": "mock-claude"})
    # Reset the cached app so it picks up fresh state; fixture already did it,
    # but we patch _model_aliases after app creation — alias is resolved at
    # request time, not at startup, so no restart needed.

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "alias test"}],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, f"Expected 200 after alias resolution, got {r.status_code}: {r.text}"
    data = r.json()
    assert "choices" in data, data


# ---------------------------------------------------------------------------
# S7-5  ANTHROPIC_DEFAULT_SONNET_MODEL env var alias
# ---------------------------------------------------------------------------

async def test_model_alias_env_var(mock_backend, monkeypatch):
    """
    ANTHROPIC_DEFAULT_SONNET_MODEL=mock-claude causes model="claude-sonnet-4-5"
    to be resolved to "mock-claude" and routed successfully.
    """
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "mock-claude")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "env alias test"}],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, f"Expected 200 after env alias, got {r.status_code}: {r.text}"
    data = r.json()
    assert "choices" in data, data
