# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""Tests that kiko forces the local bundled LiteLLM price table (no HTTP fetch on start)."""
import subprocess
import sys
import os
from unittest.mock import patch


def test_startup_no_network_fetch(tmp_path):
    """litellm import must not call httpx.get for the price table."""
    script = tmp_path / "check.py"
    script.write_text("""
import unittest.mock as mock
fetched = []
with mock.patch("httpx.get", side_effect=lambda url, **kw: fetched.append(url) or (_ for _ in ()).throw(ConnectionError("blocked"))):
    try:
        import litellm  # noqa: F401
    except Exception:
        pass
price_fetches = [u for u in fetched if "model_prices" in u or "BerriAI" in u]
if price_fetches:
    raise AssertionError(f"network fetch happened: {price_fetches}")
print("OK")
""")
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_LOCAL_MODEL_COST_MAP"}
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=30, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout, result.stdout + result.stderr


def test_get_model_cost_map_no_http_when_local():
    """With LITELLM_LOCAL_MODEL_COST_MAP=True, get_model_cost_map must not make HTTP calls."""
    import litellm.litellm_core_utils.get_model_cost_map as gcm

    with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
        with patch("httpx.get") as mock_get:
            result = gcm.get_model_cost_map(url="http://should-not-be-called")
            mock_get.assert_not_called()

    assert isinstance(result, dict) and len(result) > 50
