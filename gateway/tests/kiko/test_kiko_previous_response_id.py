# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for previous_response_id history chaining in /v1/responses.

Test:
  PRI1 — Two chained /v1/responses calls: second request carries first-turn history.
         The upstream (mocked) chat/completions request for the second call must
         include messages from both the first and second turns.
"""
import json
import httpx
import pytest
from unittest.mock import MagicMock, patch

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# PRI1 — second call with previous_response_id carries history
# ---------------------------------------------------------------------------

async def test_previous_response_id_carries_history(mock_backend):
    """
    Two chained /v1/responses calls:
    1. First call: input=[user: "What is 2+2?"]
    2. Second call: input=[user: "And what is that times 3?"],
                    previous_response_id=<first_response_id>

    The upstream request for the second call must include the first-turn messages
    (the first user message and the assistant reply), not just the second user message.
    """
    captured = mock_backend["captured"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        # --- First call ---
        r1 = await client.post(
            "/v1/responses",
            json={
                "model": "mock-claude",
                "input": [{"role": "user", "content": "What is 2+2?"}],
                "stream": False,
            },
            headers=_headers(mock_backend["key"]),
        )
        assert r1.status_code == 200, f"First call failed: {r1.text}"
        data1 = r1.json()
        assert "id" in data1, f"No 'id' in first response: {data1}"
        assert "output" in data1, f"No 'output' in first response: {data1}"
        response_id = data1["id"]

        before = len(captured)

        # --- Second call with previous_response_id ---
        r2 = await client.post(
            "/v1/responses",
            json={
                "model": "mock-claude",
                "input": [{"role": "user", "content": "And what is that times 3?"}],
                "stream": False,
                "previous_response_id": response_id,
            },
            headers=_headers(mock_backend["key"]),
        )
        assert r2.status_code == 200, f"Second call failed: {r2.text}"

    # The upstream request for the second call should contain history
    new_requests = captured[before:]
    assert new_requests, "No upstream request captured for second call"

    last_req = new_requests[-1]
    messages = last_req.get("messages", [])

    # Expect at least 3 messages: first-turn user, first-turn assistant, second-turn user
    assert len(messages) >= 3, (
        f"Expected >= 3 messages (history + new question), got {len(messages)}: {messages}"
    )

    # The history from the first turn must be present (user question "2+2")
    all_content = " ".join(
        str(m.get("content") or "") for m in messages
    )
    assert "2+2" in all_content, (
        f"First-turn user message ('2+2') not found in upstream messages: {messages}"
    )
    # Second-turn question must also be present
    assert "times 3" in all_content, (
        f"Second-turn user message ('times 3') not found in upstream messages: {messages}"
    )

    # Messages must be in order: prior user → prior assistant → new user
    assert messages[0].get("role") == "user", (
        f"Expected messages[0] role=user, got: {messages[0]}"
    )
    assert messages[1].get("role") == "assistant", (
        f"Expected messages[1] role=assistant, got: {messages[1]}"
    )
    assert messages[-1].get("role") == "user", (
        f"Expected last message role=user, got: {messages[-1]}"
    )

    # previous_response_id must NOT be forwarded to the upstream
    assert last_req.get("previous_response_id") is None, (
        f"previous_response_id was unexpectedly forwarded upstream: "
        f"{last_req.get('previous_response_id')}"
    )


# ---------------------------------------------------------------------------
# PRI3 — three-turn chain: turn 3 upstream contains all three turns
# ---------------------------------------------------------------------------

async def test_three_turn_chain_preserves_full_history(mock_backend):
    """
    Three chained /v1/responses calls.  Turn 3's upstream request must contain
    content from all three turns (turn 1 must not be lost).
    This verifies Fix 1: store input_data (full snapshot) not original_input.
    """
    captured = mock_backend["captured"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        # Turn 1
        r1 = await client.post(
            "/v1/responses",
            json={
                "model": "mock-claude",
                "input": [{"role": "user", "content": "What is 2+2?"}],
                "stream": False,
            },
            headers=_headers(mock_backend["key"]),
        )
        assert r1.status_code == 200, f"Turn 1 failed: {r1.text}"
        resp_id_1 = r1.json()["id"]

        # Turn 2
        r2 = await client.post(
            "/v1/responses",
            json={
                "model": "mock-claude",
                "input": [{"role": "user", "content": "And what is that times 3?"}],
                "stream": False,
                "previous_response_id": resp_id_1,
            },
            headers=_headers(mock_backend["key"]),
        )
        assert r2.status_code == 200, f"Turn 2 failed: {r2.text}"
        resp_id_2 = r2.json()["id"]

        before = len(captured)

        # Turn 3
        r3 = await client.post(
            "/v1/responses",
            json={
                "model": "mock-claude",
                "input": [{"role": "user", "content": "What is the final number?"}],
                "stream": False,
                "previous_response_id": resp_id_2,
            },
            headers=_headers(mock_backend["key"]),
        )
        assert r3.status_code == 200, f"Turn 3 failed: {r3.text}"

    new_requests = captured[before:]
    assert new_requests, "No upstream request captured for turn 3"

    last_req = new_requests[-1]
    messages = last_req.get("messages", [])

    all_content = " ".join(str(m.get("content") or "") for m in messages)
    assert "2+2" in all_content, (
        f"Turn 1 content ('2+2') missing from turn 3 upstream messages: {messages}"
    )
    assert "times 3" in all_content, (
        f"Turn 2 content ('times 3') missing from turn 3 upstream messages: {messages}"
    )
    assert "final number" in all_content, (
        f"Turn 3 content ('final number') missing from turn 3 upstream messages: {messages}"
    )


# ---------------------------------------------------------------------------
# PRI2 — unknown previous_response_id → 404
# ---------------------------------------------------------------------------

async def test_previous_response_id_unknown_returns_404(mock_backend):
    """
    A request with a previous_response_id that was never stored must return 404.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1/responses",
            json={
                "model": "mock-claude",
                "input": [{"role": "user", "content": "Hello"}],
                "stream": False,
                "previous_response_id": "resp-nonexistent-id-xyz",
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# PRI4 — streaming turn stores its real id; chained non-stream call carries history
# ---------------------------------------------------------------------------

async def test_pri4_stream_then_chain(mock_litellm_key, mock_db_path, client):
    """A streamed turn stores its real id; a chained call carries that turn as history."""
    import types as _types

    stream_resp_id = "resp_stream_001"
    stream_output = [{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "stream reply"}]}]
    completed_payload = {
        "type": "response.completed",
        "response": {"id": stream_resp_id, "output": stream_output},
    }

    class _FakeSSEStream:
        async def __aiter__(self):
            obj = _types.SimpleNamespace()
            raw = json.dumps(completed_payload)
            obj.model_dump_json = lambda: raw
            obj.type = "response.completed"
            yield obj

    second_resp = {
        "id": "resp_chain_001",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "chain reply"}]}],
    }
    call_inputs = []

    async def _fake_aresponses(**kwargs):
        call_inputs.append(list(kwargs.get("input", [])))
        if kwargs.get("stream"):
            return _FakeSSEStream()
        m = MagicMock()
        m.model_dump.return_value = second_resp
        return m

    with patch("litellm.aresponses", side_effect=_fake_aresponses):
        r1 = await client.post(
            "/v1/responses",
            json={"model": "claude-haiku-4-5", "input": "stream question", "stream": True},
            headers={"Authorization": "Bearer sk-test"},
        )
        assert r1.status_code == 200
        _ = r1.text  # drain so the generator runs to completion

        r2 = await client.post(
            "/v1/responses",
            json={"model": "claude-haiku-4-5", "input": "follow up", "previous_response_id": stream_resp_id},
            headers={"Authorization": "Bearer sk-test"},
        )
        assert r2.status_code == 200, r2.text

        assert len(call_inputs) == 2, f"Expected 2 upstream calls, got {len(call_inputs)}"
        second_upstream = call_inputs[1]
        assert any(
            "stream reply" in json.dumps(item)
            for item in second_upstream
        ), f"Stream history not found in second call: {second_upstream}"
