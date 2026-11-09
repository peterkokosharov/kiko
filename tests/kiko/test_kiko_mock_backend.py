# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for the /v1/chat/completions route using the mock_backend fixture.

All four tests run without a real API key or network connection.
The mock_backend fixture (conftest.py) intercepts litellm's upstream HTTP calls
via respx and routes them to in-memory response payloads.

Tests:
  M1 — non-streaming response returns 200 + content
  M2 — streaming response returns SSE chunks + [DONE]
  M3 — tool-call response contains tool_calls in choices
  M4 — billing row is written to SQLite after a successful call
"""
import asyncio
import sqlite3

import httpx
import pytest

# All tests in this module use the session-scoped event loop so the
# litellm LoggingWorker (a global singleton) does not detect a loop change
# between tests.  Without this, billing callbacks scheduled via
# asyncio.create_task() from one test can end up as "pending" tasks that
# the next test's fresh event loop cannot inherit.
pytestmark = [pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _billing_rows(db_path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM billing ORDER BY id DESC").fetchall()]
    con.close()
    return rows


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# M1 — non-streaming chat completion
# ---------------------------------------------------------------------------

async def test_chat_completions_non_stream(mock_backend):
    """POST /v1/chat/completions (stream=False) returns 200 with content."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "choices" in data, data
    content = data["choices"][0]["message"]["content"]
    assert isinstance(content, str) and len(content) > 0


# ---------------------------------------------------------------------------
# M2 — streaming SSE response
# ---------------------------------------------------------------------------

async def test_chat_completions_stream(mock_backend):
    """POST /v1/chat/completions (stream=True) delivers SSE chunks ending with [DONE]."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "stream me"}],
                "stream": True,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    body = r.text
    assert "data:" in body, f"No SSE data in body: {body!r}"
    assert "[DONE]" in body, f"No [DONE] marker in body: {body!r}"


# ---------------------------------------------------------------------------
# M3 — tool-call response
# ---------------------------------------------------------------------------

async def test_chat_completions_tool_call(mock_backend):
    """POST /v1/chat/completions with tools returns tool_calls in the response."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "What is the weather in Sofia?"}],
                "tools": tools,
                "stream": False,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "choices" in data, data
    msg = data["choices"][0].get("message", {})
    tool_calls = msg.get("tool_calls") or []
    assert len(tool_calls) > 0, f"No tool_calls in response: {data}"
    assert tool_calls[0]["function"]["name"] == "get_weather"


# ---------------------------------------------------------------------------
# M4 — billing row written after a successful call
# ---------------------------------------------------------------------------

async def test_billing_row_written(mock_backend):
    """After a successful chat completion, a billing row appears in the DB."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    rows_before = len(_billing_rows(mock_backend["db"]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "billing test"}],
                "stream": False,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    # The billing path has two async hops after acompletion() returns:
    #   1. asyncio.create_task(_client_async_logging_helper) — fires from wrapper_async
    #   2. inside that helper: GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(
    #          logging_obj.async_success_handler(...))
    #
    # sleep(0) yields once so hop-1 runs and enqueues the callback into the worker.
    # flush() then waits for queue.join(), which unblocks only after task_done() is
    # called from _process_log_task — i.e. after the billing row is committed.
    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    assert len(rows) > rows_before, "No new billing row after successful request"
    latest = rows[0]
    assert isinstance(latest["model"], str) and len(latest["model"]) > 0
    assert isinstance(latest["cost_usd"], float) and latest["cost_usd"] >= 0.0
    assert latest["tokens_in"] >= 0
