# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for POST /v1/responses (OpenAI Responses API, non-streaming).

All tests run without a real API key or network connection.
The mock_backend fixture (conftest.py) intercepts litellm's upstream HTTP calls
via respx and routes them to in-memory response payloads.

Tests:
  RS1 — basic non-stream response → 200, output list non-empty
  RS2 — unknown model → 404 with Anthropic error shape
  RS3 — wrong API key → 401
  RS4 — web_search tool stripped before upstream call
  RS5 — billing row written after successful non-stream response
"""
import asyncio
import sqlite3

import httpx
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _billing_rows(db_path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM billing ORDER BY id DESC").fetchall()]
    con.close()
    return rows


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _responses_body(**overrides) -> dict:
    base = {
        "model": "mock-claude",
        "input": [{"role": "user", "content": "Hello"}],
        "stream": False,
        "tools": [],
        "previous_response_id": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# RS1 — basic non-stream response
# ---------------------------------------------------------------------------

async def test_responses_nonstream_basic(mock_backend):
    """POST /v1/responses (stream=False) returns 200 with a non-empty output list."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json=_responses_body(),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "output" in data, f"No 'output' key in response: {data}"
    assert isinstance(data["output"], list) and len(data["output"]) > 0, (
        f"'output' is empty: {data}"
    )


# ---------------------------------------------------------------------------
# RS2 — unknown model → 404 with Anthropic error shape
# ---------------------------------------------------------------------------

async def test_responses_unknown_model(mock_backend):
    """POST /v1/responses with an unknown model returns 404 in Anthropic error shape."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json=_responses_body(model="no-such-model-xyz"),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 404, r.text
    data = r.json()
    assert data.get("type") == "error", data
    assert "error" in data and "type" in data["error"], data


# ---------------------------------------------------------------------------
# RS3 — wrong API key → 401
# ---------------------------------------------------------------------------

async def test_responses_wrong_api_key(mock_backend):
    """POST /v1/responses with a wrong Bearer token returns 401."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json=_responses_body(),
            headers=_headers("sk-wrong-key-totally-invalid"),
        )

    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# RS4 — web_search tool is stripped before the upstream call
# ---------------------------------------------------------------------------

async def test_responses_web_search_tool_stripped(mock_backend):
    """
    A body with tools=[{"type": "web_search"}] must not pass the web_search
    tool to the upstream (it is stripped inside the route before aresponses).
    The upstream call captured by mock_backend["captured"] must contain no
    tool with type 'web_search'.
    """
    captured = mock_backend["captured"]
    before = len(captured)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json=_responses_body(tools=[{"type": "web_search", "name": "web_search"}]),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    # At least one new upstream request should have been made.
    new_requests = captured[before:]
    assert new_requests, "No upstream requests were captured"

    # None of the upstream requests should contain a web_search tool.
    for req_body in new_requests:
        upstream_tools = req_body.get("tools") or []
        for tool in upstream_tools:
            assert tool.get("type") != "web_search", (
                f"web_search tool leaked to upstream: {req_body}"
            )
            # Also check function.name for translated tools
            fn = tool.get("function", {})
            assert fn.get("name") != "web_search", (
                f"web_search tool leaked to upstream (function name): {req_body}"
            )


# ---------------------------------------------------------------------------
# RS5 — billing row written after successful non-stream response
# ---------------------------------------------------------------------------

async def test_responses_billing_row_written(mock_backend):
    """A billing row appears in the DB after a successful /v1/responses call."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    rows_before = len(_billing_rows(mock_backend["db"]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json=_responses_body(input=[{"role": "user", "content": "billing test"}]),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    # Two async hops after aresponses() returns before billing row is committed:
    #   1. asyncio.create_task(_client_async_logging_helper)
    #   2. GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(...)
    # sleep(0) lets hop-1 run; flush() waits for queue.join().
    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    assert len(rows) > rows_before, "No new billing row after /v1/responses call"
    latest = rows[0]
    assert isinstance(latest["model"], str) and len(latest["model"]) > 0
    assert isinstance(latest["cost_usd"], float) and latest["cost_usd"] >= 0.0
    assert latest["tokens_in"] >= 0
