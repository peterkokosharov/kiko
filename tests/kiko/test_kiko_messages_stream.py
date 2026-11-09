# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for POST /v1/messages streaming SSE (Anthropic Messages API).

All three tests run without a real API key or network connection.
The mock_backend fixture (conftest.py) intercepts litellm's upstream HTTP
calls via respx.  Because use_chat_completions_url_for_anthropic_messages=True
is set, anthropic_messages(stream=True) routes through chat/completions, gets
OpenAI SSE back (with tool_call chunks), and litellm's AnthropicStreamWrapper
transforms those into proper Anthropic SSE events before kiko streams them.

Tests:
  AS1 — event order: message_start → content_block_start(text) → delta(s)
         → content_block_stop → content_block_start(tool_use)
         → input_json_delta(s) → content_block_stop → message_delta
         → message_stop
  AS2 — tool JSON: all input_json_delta partial_json pieces concatenate to
         valid JSON
  AS3 — billing row written to DB after stream is consumed
"""
import asyncio
import json
import sqlite3

import httpx
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOOLS = [
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


def _parse_sse_events(text: str) -> list[dict]:
    """
    Parse SSE response body into a list of event data dicts.

    Each SSE block looks like::

        event: <type>\\ndata: <json>\\n\\n

    Returns only blocks where data is valid JSON (skips [DONE] etc.).
    """
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_line = None
        for line in block.split("\n"):
            if line.startswith("data: "):
                data_line = line[6:]
        if data_line is None or data_line == "[DONE]":
            continue
        try:
            events.append(json.loads(data_line))
        except json.JSONDecodeError:
            pass
    return events


# ---------------------------------------------------------------------------
# AS1 — event order
# ---------------------------------------------------------------------------

async def test_messages_stream_event_order(mock_backend):
    """
    POST /v1/messages stream=True returns Anthropic SSE events in the correct
    order:
      message_start
      content_block_start  (index 0, type text)
      content_block_delta  (type text_delta) — one or more
      content_block_stop   (index 0)
      content_block_start  (index 1, type tool_use)
      content_block_delta  (type input_json_delta) — one or more
      content_block_stop   (index 1)
      message_delta
      message_stop
    """
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "What is the weather in Sofia?"}],
                "max_tokens": 100,
                "stream": True,
                "tools": _TOOLS,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", ""), r.headers

    events = _parse_sse_events(r.text)
    assert events, f"No SSE events in response:\n{r.text!r}"

    types = [e.get("type") for e in events]

    # Must start with message_start
    assert types[0] == "message_start", f"First event must be message_start, got: {types}"

    # Must end with message_stop
    assert types[-1] == "message_stop", f"Last event must be message_stop, got: {types}"

    # message_delta must come just before message_stop
    assert types[-2] == "message_delta", f"Second-to-last must be message_delta, got: {types}"

    # Must contain at least two content_block_start events
    cb_starts = [e for e in events if e.get("type") == "content_block_start"]
    assert len(cb_starts) >= 2, f"Expected >=2 content_block_start events, got: {cb_starts}"

    # First content block must be text
    first_cb = cb_starts[0]
    cb0_type = (first_cb.get("content_block") or {}).get("type")
    assert cb0_type == "text", f"First content_block must be text, got: {first_cb}"

    # Second content block must be tool_use
    second_cb = cb_starts[1]
    cb1_type = (second_cb.get("content_block") or {}).get("type")
    assert cb1_type == "tool_use", f"Second content_block must be tool_use, got: {second_cb}"

    # Must have at least one text_delta and at least one input_json_delta
    text_deltas = [
        e for e in events
        if e.get("type") == "content_block_delta"
        and (e.get("delta") or {}).get("type") == "text_delta"
    ]
    json_deltas = [
        e for e in events
        if e.get("type") == "content_block_delta"
        and (e.get("delta") or {}).get("type") == "input_json_delta"
    ]
    assert text_deltas, f"No text_delta events found: {types}"
    assert json_deltas, f"No input_json_delta events found: {types}"

    # content_block_stop events must exist (at least 2: one per block)
    cb_stops = [e for e in events if e.get("type") == "content_block_stop"]
    assert len(cb_stops) >= 2, f"Expected >=2 content_block_stop events, got: {cb_stops}"


# ---------------------------------------------------------------------------
# AS2 — tool JSON valid
# ---------------------------------------------------------------------------

async def test_messages_stream_tool_json_valid(mock_backend):
    """
    Concatenating all input_json_delta partial_json pieces yields valid JSON.
    """
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "Weather in Sofia?"}],
                "max_tokens": 100,
                "stream": True,
                "tools": _TOOLS,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    events = _parse_sse_events(r.text)

    # Collect all input_json_delta pieces
    pieces = [
        (e.get("delta") or {}).get("partial_json", "")
        for e in events
        if e.get("type") == "content_block_delta"
        and (e.get("delta") or {}).get("type") == "input_json_delta"
    ]
    assert pieces, f"No input_json_delta events found in:\n{r.text!r}"

    concatenated = "".join(pieces)
    try:
        parsed = json.loads(concatenated)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"input_json_delta pieces do not concatenate to valid JSON.\n"
            f"Pieces: {pieces!r}\nConcatenated: {concatenated!r}\nError: {exc}"
        )
    assert isinstance(parsed, dict), f"Expected JSON object, got: {type(parsed)}"


# ---------------------------------------------------------------------------
# AS3 — billing row written after stream is consumed
# ---------------------------------------------------------------------------

async def test_messages_stream_billing_row(mock_backend):
    """
    After streaming POST /v1/messages is consumed, a billing row appears in DB.
    """
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    rows_before = len(_billing_rows(mock_backend["db"]))

    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "mock-claude",
                "messages": [{"role": "user", "content": "stream billing test"}],
                "max_tokens": 100,
                "stream": True,
                "tools": _TOOLS,
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    # Two async hops after the stream is consumed:
    #   1. asyncio.create_task(_client_async_logging_helper) fires
    #   2. GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue enqueues billing write
    # sleep(0) yields for hop-1; flush() waits for queue.join() / task_done().
    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    assert len(rows) > rows_before, (
        f"No new billing row after streaming /v1/messages request.\n"
        f"rows_before={rows_before}, rows_after={len(rows)}"
    )
    latest = rows[0]
    assert isinstance(latest["model"], str) and len(latest["model"]) > 0
    assert isinstance(latest["cost_usd"], float) and latest["cost_usd"] >= 0.0
    assert latest["tokens_in"] >= 0
