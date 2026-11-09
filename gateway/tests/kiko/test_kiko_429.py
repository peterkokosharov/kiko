# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Test that an upstream 429 (RateLimitError) is returned to the client as
HTTP 429 with a Retry-After header instead of being swallowed as 500.

Tests:
  R1 — upstream RateLimitError on /v1/messages → 429 + retry-after header
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

pytestmark = [pytest.mark.asyncio(loop_scope="session")]


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# R1 — upstream RateLimitError surfaces as 429 + Retry-After
# ---------------------------------------------------------------------------

async def test_rate_limit_returns_429(mock_backend):
    """POST /v1/messages when upstream raises RateLimitError → 429 + retry-after."""
    import litellm.exceptions as lx

    exc = lx.RateLimitError(
        message="rate limit",
        llm_provider="anthropic",
        model="claude-sonnet-4-6",
        response=httpx.Response(429, headers={"retry-after": "30"}),
    )

    with patch("litellm.anthropic_messages", new=AsyncMock(side_effect=exc)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_backend["app"]),
            base_url="http://test",
        ) as client:
            r = await client.post(
                "/v1/messages",
                json={
                    "model": "mock-claude",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 100,
                },
                headers=_headers(mock_backend["key"]),
            )

    assert r.status_code == 429, f"Expected 429, got {r.status_code}: {r.text}"
    retry_after = r.headers.get("retry-after")
    assert retry_after == "30", f"Expected retry-after: 30, got {retry_after!r}"
