# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
Integration tests for KIKO proxy.
Requires kircho-proxy running (fixture in conftest.py).
Uses Gemini only — key reloadable from aistudio.google.com.
"""
import json
import sqlite3
import time
import urllib.error
import urllib.request

import pytest


def _chat(url: str, key: str, message: str = "say: ok") -> dict:
    body = json.dumps({
        "model":    "gemini-flash-lite",
        "messages": [{"role": "user", "content": message}],
        "stream":   False,
        "max_tokens": 400,
    }).encode()
    req = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def _billing_rows(db_path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM billing ORDER BY id DESC").fetchall()]
    con.close()
    return rows


# ---------------------------------------------------------------------------
# I1 — proxy is healthy
# ---------------------------------------------------------------------------
def test_health(kiko_proxy):
    urllib.request.urlopen(f"{kiko_proxy['url']}/health/liveliness", timeout=5)


# ---------------------------------------------------------------------------
# I2 — /v1/models returns Gemini model list
# ---------------------------------------------------------------------------
def test_models_list(kiko_proxy):
    req = urllib.request.Request(
        f"{kiko_proxy['url']}/v1/models",
        headers={"Authorization": f"Bearer {kiko_proxy['key']}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    assert "data" in data
    assert len(data["data"]) > 0


# ---------------------------------------------------------------------------
# I3 — chat completion returns a non-empty response
# ---------------------------------------------------------------------------
def test_chat_completion_response(kiko_proxy):
    resp = _chat(kiko_proxy["url"], kiko_proxy["key"])
    assert resp.get("choices")
    content = resp["choices"][0]["message"]["content"]
    assert isinstance(content, str) and len(content) > 0


# ---------------------------------------------------------------------------
# I4 — billing row is written after a successful call
# ---------------------------------------------------------------------------
def test_billing_row_written(kiko_proxy):
    time.sleep(13)  # respect 5 RPM free-tier limit
    rows_before = len(_billing_rows(kiko_proxy["db"]))
    _chat(kiko_proxy["url"], kiko_proxy["key"])
    time.sleep(2)  # logger is async — give it a moment
    rows_after = _billing_rows(kiko_proxy["db"])
    assert len(rows_after) > rows_before
    latest = rows_after[0]
    assert isinstance(latest["model"], str) and len(latest["model"]) > 0
    assert isinstance(latest["cost_usd"], float) and latest["cost_usd"] >= 0.0
    assert latest["tokens_in"] > 0


# ---------------------------------------------------------------------------
# I5 — invalid key returns 401
# ---------------------------------------------------------------------------
def test_invalid_key_returns_401(kiko_proxy):
    req = urllib.request.Request(
        f"{kiko_proxy['url']}/v1/chat/completions",
        data=json.dumps({"model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Authorization": "Bearer sk-wrong", "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 401


# ---------------------------------------------------------------------------
# I6 — 3 sequential calls → 3 billing rows
# ---------------------------------------------------------------------------
def test_three_calls_three_rows(kiko_proxy):
    rows_before = len(_billing_rows(kiko_proxy["db"]))
    for _ in range(3):
        time.sleep(13)  # respect 5 RPM free-tier limit
        _chat(kiko_proxy["url"], kiko_proxy["key"], "one word answer: yes")
    time.sleep(1.0)
    rows_after = _billing_rows(kiko_proxy["db"])
    assert len(rows_after) - rows_before == 3
