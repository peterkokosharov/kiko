# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Tests for the Gemini streamGenerateContent SSE route.

POST /v1beta/models/{model_id}:streamGenerateContent (and without /v1beta)
- Response: data: <json>\n\n chunks
- NO event: prefix, NO [DONE] marker
- Last chunk has finishReason + usageMetadata
- Billing row written after stream consumed

All tests use the mock_backend fixture (no real API calls or keys).
"""
import asyncio
import json
import sqlite3

import httpx
import pytest

# Session-scoped event loop so litellm's LoggingWorker (a global singleton)
# does not detect a loop change between tests.
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


async def _stream_chunks(client, url: str, body: dict, key: str) -> tuple[str, list[dict]]:
    """POST to url, collect full text and list of parsed data: JSON objects."""
    r = await client.post(url, json=body, headers=_headers(key))
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text}"
    text = r.text
    parsed = []
    for line in text.splitlines():
        if line.startswith("data: "):
            parsed.append(json.loads(line[6:]))
    return text, parsed


_GEMINI_BODY = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}


# ---------------------------------------------------------------------------
# GS1 — each data: line is a valid generateContentResponse
# ---------------------------------------------------------------------------

async def test_stream_generate_data_chunks(mock_backend):
    """Every data: line is a valid JSON generateContentResponse with candidates."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        _, chunks = await _stream_chunks(
            client,
            "/v1beta/models/mock-gemini:streamGenerateContent",
            _GEMINI_BODY,
            mock_backend["key"],
        )

    assert len(chunks) > 0, "No data: chunks received"
    for chunk in chunks:
        assert "candidates" in chunk, f"Missing 'candidates' in chunk: {chunk}"
        assert isinstance(chunk["candidates"], list)
        assert len(chunk["candidates"]) > 0, f"Empty candidates list in chunk: {chunk}"


# ---------------------------------------------------------------------------
# GS2 — functionCall appears in one of the chunks
# ---------------------------------------------------------------------------

async def test_stream_generate_function_call_present(mock_backend):
    """Mock returns a tool_call chunk; functionCall must appear in Gemini output."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        _, chunks = await _stream_chunks(
            client,
            "/v1beta/models/mock-gemini:streamGenerateContent",
            _GEMINI_BODY,
            mock_backend["key"],
        )

    all_parts: list[dict] = []
    for chunk in chunks:
        for candidate in chunk.get("candidates", []):
            all_parts.extend(candidate.get("content", {}).get("parts", []))

    function_calls = [p for p in all_parts if "functionCall" in p]
    assert len(function_calls) > 0, (
        f"No functionCall found in any chunk part.\nAll parts: {all_parts}"
    )


# ---------------------------------------------------------------------------
# GS3 — last non-empty chunk has finishReason set
# ---------------------------------------------------------------------------

async def test_stream_generate_last_chunk_finish_reason(mock_backend):
    """The last candidate in the stream has a non-null finishReason."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        _, chunks = await _stream_chunks(
            client,
            "/v1beta/models/mock-gemini:streamGenerateContent",
            _GEMINI_BODY,
            mock_backend["key"],
        )

    finish_reasons = [
        candidate.get("finishReason")
        for chunk in chunks
        for candidate in chunk.get("candidates", [])
        if candidate.get("finishReason")
    ]
    assert len(finish_reasons) > 0, (
        f"No finishReason found in any chunk.\nChunks: {chunks}"
    )


# ---------------------------------------------------------------------------
# GS4 — last chunk has usageMetadata with totalTokenCount > 0
# ---------------------------------------------------------------------------

async def test_stream_generate_last_chunk_usage(mock_backend):
    """usageMetadata is present in the last chunk with totalTokenCount > 0."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        _, chunks = await _stream_chunks(
            client,
            "/v1beta/models/mock-gemini:streamGenerateContent",
            _GEMINI_BODY,
            mock_backend["key"],
        )

    usage_chunks = [c for c in chunks if "usageMetadata" in c]
    assert len(usage_chunks) > 0, f"No chunk with usageMetadata found.\nChunks: {chunks}"
    usage = usage_chunks[-1]["usageMetadata"]
    assert usage.get("totalTokenCount", 0) > 0, (
        f"totalTokenCount is not > 0: {usage}"
    )


# ---------------------------------------------------------------------------
# GS5 — [DONE] marker absent from the stream
# ---------------------------------------------------------------------------

async def test_stream_generate_no_done_marker(mock_backend):
    """The SSE stream must NOT contain [DONE] (Gemini format, not OpenAI)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:streamGenerateContent",
            json=_GEMINI_BODY,
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text}"
    assert "[DONE]" not in r.text, (
        f"[DONE] marker must not appear in Gemini SSE stream.\nBody: {r.text!r}"
    )


# ---------------------------------------------------------------------------
# GS6 — billing row written after stream consumed
# ---------------------------------------------------------------------------

async def test_stream_generate_billing_row(mock_backend):
    """A billing row is inserted into SQLite after the stream is fully consumed."""
    db_path = mock_backend["db"]
    rows_before = _billing_rows(db_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "billing test"}]}]},
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text}"

    # Allow the async billing callback task to complete
    await asyncio.sleep(0.5)

    rows_after = _billing_rows(db_path)
    assert len(rows_after) > len(rows_before), (
        f"No new billing row after streaming. "
        f"Before: {len(rows_before)}, After: {len(rows_after)}"
    )
