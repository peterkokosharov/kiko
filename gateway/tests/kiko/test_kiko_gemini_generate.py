# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Offline tests for the Gemini generateContent and countTokens routes.

All tests run without a real API key or network connection.
The mock_backend fixture intercepts litellm's upstream HTTP calls via respx
and returns OpenAI-format responses; the google_genai adapter translates
Gemini ↔ OpenAI on the way in and out.

Tests:
  G1 — generateContent returns text in candidates[0].content.parts
  G2 — generateContent with tools: mock returns tool_calls → functionCall part
  G3 — generateContent with functionResponse: adapter sends tool message upstream
  G4 — generateContent returns usageMetadata with totalTokenCount > 0
  G5 — countTokens returns totalTokens > 0 (local, no upstream call)
  G6 — /models/{model}:generateContent (without /v1beta prefix) also works
"""
import httpx
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# G1 — generateContent returns text part
# ---------------------------------------------------------------------------

async def test_generate_content_text(mock_backend):
    """POST /v1beta/models/mock-gemini:generateContent returns text in parts."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:generateContent",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Hello"}]}
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "candidates" in data, data
    parts = data["candidates"][0]["content"]["parts"]
    assert isinstance(parts, list) and len(parts) > 0
    texts = [p["text"] for p in parts if "text" in p]
    assert any(isinstance(t, str) and len(t) > 0 for t in texts)


# ---------------------------------------------------------------------------
# G2 — mock returns tool_calls → functionCall part in response
# ---------------------------------------------------------------------------

async def test_generate_content_function_call(mock_backend):
    """When mock returns tool_calls, adapter translates them to functionCall parts."""
    # Sending Gemini-format tools triggers the mock to return _TOOL_CALL_RESP
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:generateContent",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "What is the weather in Sofia?"}]}
                ],
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "get_weather",
                                "description": "Get the weather for a city",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "city": {"type": "string"}
                                    },
                                    "required": ["city"],
                                },
                            }
                        ]
                    }
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "candidates" in data, data
    parts = data["candidates"][0]["content"]["parts"]
    func_calls = [p for p in parts if "functionCall" in p]
    assert len(func_calls) > 0, f"Expected functionCall part, got: {parts}"
    fc = func_calls[0]["functionCall"]
    assert fc["name"] == "get_weather"
    assert "city" in fc["args"]


# ---------------------------------------------------------------------------
# G3 — functionResponse in request → tool message at upstream
# ---------------------------------------------------------------------------

async def test_generate_content_function_response(mock_backend):
    """functionResponse in contents is translated to OpenAI tool message upstream."""
    captured = mock_backend["captured"]
    before = len(captured)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:generateContent",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "What is the weather?"}]},
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "get_weather",
                                    "args": {"city": "Sofia"},
                                }
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": "get_weather",
                                    "response": {"temperature": "22C", "condition": "sunny"},
                                }
                            }
                        ],
                    },
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    # Verify the upstream received a "tool" role message
    new_calls = captured[before:]
    assert len(new_calls) == 1, f"Expected 1 upstream call, got {len(new_calls)}"
    upstream_messages = new_calls[0]["messages"]
    tool_msgs = [m for m in upstream_messages if m.get("role") == "tool"]
    assert len(tool_msgs) > 0, f"No tool message in upstream request: {upstream_messages}"


# ---------------------------------------------------------------------------
# G4 — usageMetadata.totalTokenCount > 0
# ---------------------------------------------------------------------------

async def test_generate_content_usage_metadata(mock_backend):
    """generateContent response includes usageMetadata with totalTokenCount > 0."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:generateContent",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Count my tokens"}]}
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "usageMetadata" in data, data
    usage = data["usageMetadata"]
    total = usage.get("totalTokenCount", 0)
    assert total > 0, f"Expected totalTokenCount > 0, got: {usage}"


# ---------------------------------------------------------------------------
# G5 — countTokens returns totalTokens > 0 (local, no upstream)
# ---------------------------------------------------------------------------

async def test_count_tokens_gemini(mock_backend):
    """POST /v1beta/models/mock-gemini:countTokens returns totalTokens > 0."""
    captured = mock_backend["captured"]
    before = len(captured)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/v1beta/models/mock-gemini:countTokens",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Hello, count my tokens please!"}]}
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "totalTokens" in data, data
    assert data["totalTokens"] > 0, f"Expected totalTokens > 0, got: {data}"

    # countTokens must NOT call the upstream
    new_calls = captured[before:]
    assert len(new_calls) == 0, f"countTokens made {len(new_calls)} unexpected upstream call(s)"


# ---------------------------------------------------------------------------
# G6 — /models/{model}:generateContent (without /v1beta prefix) also works
# ---------------------------------------------------------------------------

async def test_generate_content_without_v1beta(mock_backend):
    """POST /models/mock-gemini:generateContent (no v1beta prefix) returns 200."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_backend["app"]),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/models/mock-gemini:generateContent",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Test without prefix"}]}
                ],
            },
            headers=_headers(mock_backend["key"]),
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert "candidates" in data, data
    parts = data["candidates"][0]["content"]["parts"]
    assert isinstance(parts, list) and len(parts) > 0
