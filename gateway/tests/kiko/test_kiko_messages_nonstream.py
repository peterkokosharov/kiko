# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for the POST /v1/messages route (Anthropic Messages API, non-streaming).

All five tests run without a real API key or network connection.
The mock_backend fixture (conftest.py) intercepts litellm's upstream HTTP calls
via respx. Because use_chat_completions_url_for_anthropic_messages=True is set at
startup, anthropic_messages() routes through chat/completions to the same mock
upstream that the chat route tests use.

Tests:
  AM1 — text response: 200 + content[0].type == "text"
  AM2 — tool_use response: content has a tool_use block + stop_reason == "tool_use"
  AM3 — system as block list: accepted without error (200)
  AM4 — 2nd-turn tool_result body: forwarded without crash (200)
  AM5 — billing row written to DB after a successful /v1/messages call
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


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


# ---------------------------------------------------------------------------
# AM1 — text response
# ---------------------------------------------------------------------------

async def test_messages_text_response(mock_backend):
    """POST /v1/messages returns 200 with a text content block."""
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("type") == "message", data
    assert data.get("role") == "assistant", data
    content = data.get("content") or []
    assert len(content) > 0, f"Expected at least one content block: {data}"
    assert content[0]["type"] == "text", content[0]


# ---------------------------------------------------------------------------
# AM2 — tool_use response
# ---------------------------------------------------------------------------

async def test_messages_tool_use_response(mock_backend):
    """POST /v1/messages with tools mock returns a tool_use block + stop_reason tool_use."""
    tools = [
        {
            "name": "get_weather",
            "description": "Get weather for a city.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]

    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "What's the weather in Sofia?"}],
                "max_tokens": 100,
                "tools": tools,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    content = data.get("content") or []
    tool_blocks = [b for b in content if b.get("type") == "tool_use"]
    assert len(tool_blocks) > 0, f"No tool_use block in content: {data}"
    assert data.get("stop_reason") == "tool_use", data


# ---------------------------------------------------------------------------
# AM3 — system as block list
# ---------------------------------------------------------------------------

async def test_messages_system_as_block_list(mock_backend):
    """POST /v1/messages with system as a list of content blocks is accepted (200)."""
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 100,
                "system": [{"type": "text", "text": "You are a helpful assistant."}],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("type") == "message", data


# ---------------------------------------------------------------------------
# AM4 — 2nd-turn tool_result body forwarded correctly
# ---------------------------------------------------------------------------

async def test_messages_second_turn_tool_result(mock_backend):
    """
    2nd-turn body with a role:user content block of type tool_result is forwarded
    without error — verifies kiko translates multi-turn tool history correctly.
    """
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": "Use the calculator"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_01",
                                "name": "calculator",
                                "input": {"expression": "1+1"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "2",
                            }
                        ],
                    },
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("type") == "message", data


# ---------------------------------------------------------------------------
# AM5 — billing row written after a successful /v1/messages call
# ---------------------------------------------------------------------------

async def test_messages_billing_row(mock_backend):
    """After a successful POST /v1/messages a billing row appears in the DB."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    rows_before = len(_billing_rows(mock_backend["db"]))

    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "billing test"}],
                "max_tokens": 100,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    # Two async hops after anthropic_messages() returns:
    #   1. asyncio.create_task(_client_async_logging_helper) fires from the @client wrapper
    #   2. GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue enqueues the billing write
    # sleep(0) yields so hop-1 runs; flush() waits for queue.join() after task_done().
    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    assert len(rows) > rows_before, "No new billing row after successful /v1/messages request"
    latest = rows[0]
    assert isinstance(latest["model"], str) and len(latest["model"]) > 0
    assert isinstance(latest["cost_usd"], float) and latest["cost_usd"] >= 0.0
    assert latest["tokens_in"] >= 0
