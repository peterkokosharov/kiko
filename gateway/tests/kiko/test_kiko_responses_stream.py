# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for POST /v1/responses streaming SSE (OpenAI Responses API).

stream=true path added in Step 11.  The route calls
litellm.aresponses(use_chat_completions_api=True, stream=True) which
translates to chat/completions internally; the mock upstream returns a plain
OpenAI-chat SSE stream.  litellm's LiteLLMCompletionStreamingIterator
converts those chunks into Responses API events and kiko wraps each as:

    event: {type}
    data: {json}

Tests:
  RS_S1 — stream=true → Content-Type: text/event-stream
  RS_S2 — all emitted events are valid JSON on the data lines
  RS_S3 — response.completed event is present as the last event
  RS_S4 — text delta events contain non-empty text
  RS_S5 — billing row written after stream consumed
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
    Parse SSE response body into a list of dicts with 'type' and 'data' keys.

    Each SSE block::

        event: <type>
        data: <json>

    Returns only blocks where data is valid JSON (skips [DONE] etc.).
    """
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = None
        data_line = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data_line = line[6:]
        if data_line is None or data_line == "[DONE]":
            continue
        try:
            data = json.loads(data_line)
            events.append({"type": event_type, "data": data})
        except json.JSONDecodeError:
            pass
    return events


_BODY = {
    "model": "mock-model",
    "input": [{"role": "user", "content": "Hello"}],
    "stream": True,
}


# ---------------------------------------------------------------------------
# RS_S1 — Content-Type: text/event-stream
# ---------------------------------------------------------------------------

async def test_responses_stream_content_type(mock_backend):
    """stream=true → Content-Type: text/event-stream"""
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/responses",
            json=_BODY,
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", ""), r.headers


# ---------------------------------------------------------------------------
# RS_S2 — all events have valid JSON on data lines
# ---------------------------------------------------------------------------

async def test_responses_stream_all_events_valid_json(mock_backend):
    """All emitted SSE events have valid JSON on their data lines."""
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/responses",
            json=_BODY,
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", ""), r.headers
    events = _parse_sse_events(r.text)
    assert events, f"No SSE events in response:\n{r.text!r}"
    for ev in events:
        assert isinstance(ev["data"], dict), f"Event data is not a dict: {ev}"


# ---------------------------------------------------------------------------
# RS_S3 — response.completed is the last event
# ---------------------------------------------------------------------------

async def test_responses_stream_completed_event_last(mock_backend):
    """response.completed event is present and is the last event in the stream."""
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/responses",
            json=_BODY,
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text
    events = _parse_sse_events(r.text)
    assert events, f"No SSE events:\n{r.text!r}"
    types = [ev["type"] for ev in events]
    assert "response.completed" in types, (
        f"response.completed not found in event types: {types}\n"
        f"Raw response:\n{r.text!r}"
    )
    assert types[-1] == "response.completed", (
        f"Last event is not response.completed. Event types: {types}"
    )


# ---------------------------------------------------------------------------
# RS_S4 — text delta events contain non-empty text
# ---------------------------------------------------------------------------

async def test_responses_stream_text_delta_non_empty(mock_backend):
    """response.output_text.delta events carry non-empty text."""
    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/responses",
            json=_BODY,
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text
    events = _parse_sse_events(r.text)
    delta_events = [ev for ev in events if ev["type"] == "response.output_text.delta"]
    all_types = [ev["type"] for ev in events]
    assert delta_events, (
        f"No response.output_text.delta events found.\n"
        f"Event types observed: {all_types}\n"
        f"Raw response:\n{r.text!r}"
    )
    for ev in delta_events:
        delta_text = ev["data"].get("delta", "")
        assert delta_text, f"response.output_text.delta has empty delta: {ev}"


# ---------------------------------------------------------------------------
# RS_S5 — billing row written after stream consumed
# ---------------------------------------------------------------------------

async def test_responses_stream_billing_row(mock_backend):
    """A billing row is inserted into SQLite after the streaming response is consumed."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    rows_before = len(_billing_rows(mock_backend["db"]))

    async with _client(mock_backend["app"]) as client:
        r = await client.post(
            "/v1/responses",
            json=_BODY,
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text

    # Two async hops after the stream is consumed:
    #   1. asyncio.create_task fires the billing callback
    #   2. GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue enqueues the write
    # sleep(0) yields for hop-1; flush() waits for queue.join() / task_done().
    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    assert len(rows) > rows_before, (
        f"No new billing row after streaming /v1/responses.\n"
        f"rows_before={rows_before}, rows_after={len(rows)}"
    )
    latest = rows[0]
    assert isinstance(latest["model"], str) and len(latest["model"]) > 0
    assert isinstance(latest["cost_usd"], float) and latest["cost_usd"] >= 0.0
    assert latest["tokens_in"] >= 0
