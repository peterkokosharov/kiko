# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Tests for monthly_cap_usd enforcement and billing route column (Step 12).

MC1: no cap set → requests go through normally
MC2: cap=0.01, spent=0.00 → request succeeds
MC3: cap=0.01, spent>=0.01 → /v1/chat/completions returns 429 with OpenAI error shape
MC4: cap=0.01, spent>=0.01 → /v1/messages returns 429 with Anthropic error shape
MC5: cap=0.01, spent>=0.01 → /v1/responses returns 429 with OpenAI error shape
MC6: billing row has route column populated after a successful /v1/chat/completions call
MC7: route column stored correctly for Gemini generateContent route
"""
import asyncio
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

import litellm.proxy.kiko_server as ks

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _chat_body(**overrides) -> dict:
    base = {"model": "mock-model", "messages": [{"role": "user", "content": "Hello"}]}
    base.update(overrides)
    return base


def _messages_body(**overrides) -> dict:
    base = {
        "model": "mock-claude",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }
    base.update(overrides)
    return base


def _responses_body(**overrides) -> dict:
    base = {
        "model": "mock-claude",
        "input": [{"role": "user", "content": "Hello"}],
        "stream": False,
    }
    base.update(overrides)
    return base


def _gemini_body(**overrides) -> dict:
    base = {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]}
    base.update(overrides)
    return base


def _billing_rows(db_path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM billing ORDER BY id DESC").fetchall()]
    con.close()
    return rows


def _insert_billing_row(db_path, cost_usd: float) -> None:
    """Insert a billing row for the current month."""
    ts = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO billing(model, tokens_in, tokens_out, cost_usd, ts) VALUES(?,?,?,?,?)",
        ("test-model", 10, 5, cost_usd, ts),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# MC1: no cap set → requests go through normally
# ---------------------------------------------------------------------------

async def test_mc1_no_cap_request_succeeds(mock_backend, monkeypatch):
    """With no monthly_cap_usd set, requests must pass through (no 429)."""
    monkeypatch.setattr(ks, "_monthly_cap_usd", None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# MC2: cap=0.01, spent=0.00 → request succeeds
# ---------------------------------------------------------------------------

async def test_mc2_cap_not_reached_request_succeeds(mock_backend, monkeypatch):
    """Cap is set but spending is zero — request must succeed."""
    monkeypatch.setattr(ks, "_monthly_cap_usd", 0.01)
    # DB is fresh (no billing rows), so spent = 0.00
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers=_headers(mock_backend["key"]),
        )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# MC3: cap=0.01, spent>=0.01 → /v1/chat/completions → 429 OpenAI shape
# ---------------------------------------------------------------------------

async def test_mc3_cap_exceeded_chat_openai_error(mock_backend, monkeypatch):
    """
    When monthly cap is exceeded, POST /v1/chat/completions returns 429
    with OpenAI error shape: error.type='insufficient_quota'.
    """
    monkeypatch.setattr(ks, "_monthly_cap_usd", 0.01)
    _insert_billing_row(mock_backend["db"], cost_usd=0.01)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 429, r.text
    data = r.json()
    assert "error" in data, f"Expected 'error' key, got: {data}"
    err = data["error"]
    assert err.get("type") == "insufficient_quota", f"Wrong error type: {err}"
    assert err.get("code") == "monthly_cap_exceeded", f"Wrong error code: {err}"
    assert "Monthly spending cap" in err.get("message", ""), f"Wrong message: {err}"


# ---------------------------------------------------------------------------
# MC4: cap=0.01, spent>=0.01 → /v1/messages → 429 Anthropic shape
# ---------------------------------------------------------------------------

async def test_mc4_cap_exceeded_messages_anthropic_error(mock_backend, monkeypatch):
    """
    When monthly cap is exceeded, POST /v1/messages returns 429
    with Anthropic error shape: type='error', error.type='rate_limit_error'.
    """
    monkeypatch.setattr(ks, "_monthly_cap_usd", 0.01)
    _insert_billing_row(mock_backend["db"], cost_usd=0.01)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/messages",
            json=_messages_body(),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 429, r.text
    data = r.json()
    assert data.get("type") == "error", f"Expected top-level 'type'='error', got: {data}"
    assert "error" in data, f"Expected 'error' key, got: {data}"
    err = data["error"]
    assert err.get("type") == "rate_limit_error", f"Wrong error type: {err}"
    assert "Monthly spending cap" in err.get("message", ""), f"Wrong message: {err}"


# ---------------------------------------------------------------------------
# MC5: cap=0.01, spent>=0.01 → /v1/responses → 429 OpenAI shape
# ---------------------------------------------------------------------------

async def test_mc5_cap_exceeded_responses_openai_error(mock_backend, monkeypatch):
    """
    When monthly cap is exceeded, POST /v1/responses returns 429
    with OpenAI error shape: error.type='insufficient_quota'.
    """
    monkeypatch.setattr(ks, "_monthly_cap_usd", 0.01)
    _insert_billing_row(mock_backend["db"], cost_usd=0.01)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json=_responses_body(),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 429, r.text
    data = r.json()
    assert "error" in data, f"Expected 'error' key, got: {data}"
    err = data["error"]
    assert err.get("type") == "insufficient_quota", f"Wrong error type: {err}"
    assert err.get("code") == "monthly_cap_exceeded", f"Wrong error code: {err}"
    assert "Monthly spending cap" in err.get("message", ""), f"Wrong message: {err}"


# ---------------------------------------------------------------------------
# MC6: billing row has route column after /v1/chat/completions
# ---------------------------------------------------------------------------

async def test_mc6_route_column_chat(mock_backend, monkeypatch):
    """
    After a successful /v1/chat/completions call, the billing row must have
    route='chat'.
    """
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    monkeypatch.setattr(ks, "_monthly_cap_usd", None)
    rows_before = len(_billing_rows(mock_backend["db"]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    new_rows = [row for row in rows if rows.index(row) < len(rows) - rows_before]
    assert len(rows) > rows_before, "No new billing row after /v1/chat/completions"

    latest = rows[0]
    assert latest.get("route") == "chat", (
        f"Expected route='chat', got route={latest.get('route')!r}. Row: {latest}"
    )


# ---------------------------------------------------------------------------
# MC7: route column stored correctly for Gemini generateContent route
# ---------------------------------------------------------------------------

async def test_mc7_route_column_gemini_generate(mock_backend, monkeypatch):
    """
    After a successful /v1beta/models/mock-gemini:generateContent call,
    the billing row must have route='gemini_generate'.
    """
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    monkeypatch.setattr(ks, "_monthly_cap_usd", None)
    rows_before = len(_billing_rows(mock_backend["db"]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:generateContent",
            json=_gemini_body(),
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text

    await asyncio.sleep(0)
    await GLOBAL_LOGGING_WORKER.flush()

    rows = _billing_rows(mock_backend["db"])
    assert len(rows) > rows_before, "No new billing row after Gemini generateContent call"

    latest = rows[0]
    assert latest.get("route") == "gemini_generate", (
        f"Expected route='gemini_generate', got route={latest.get('route')!r}. Row: {latest}"
    )
