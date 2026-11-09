# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Test that NIM (nvidia/nemotron) requests log cost > 0 in the billing DB.

Root cause: the model strings used in kiko-config.yaml
  openai/nvidia/nemotron-3-super-120b-a12b
  openai/nvidia/nemotron-3-ultra-550b-a55b
were absent from the bundled price map, so completion_cost raised,
response_cost became None, and the logger wrote 0.0.
"""
import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import litellm
from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger


def _billing_rows(db_path):
    con = sqlite3.connect(str(db_path))
    rows = con.execute("SELECT cost_usd FROM billing").fetchall()
    con.close()
    return rows


def _run_logger(logger, model, cost, prompt_tokens=100, completion_tokens=50):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    response = SimpleNamespace(usage=usage)
    kwargs = {"model": model, "response_cost": cost}
    t = datetime.now(timezone.utc)
    asyncio.run(logger.async_log_success_event(kwargs, response, t, t))


@pytest.mark.parametrize("model", [
    "openai/nvidia/nemotron-3-super-120b-a12b",
    "openai/nvidia/nemotron-3-ultra-550b-a55b",
])
def test_nim_cost_per_token_is_positive(model):
    """litellm.cost_per_token must return positive costs for NIM models."""
    prompt_cost, completion_cost = litellm.cost_per_token(
        model=model,
        prompt_tokens=100,
        completion_tokens=50,
    )
    total = prompt_cost + completion_cost
    assert total > 0, f"Expected total cost > 0 for {model}, got {total}"


@pytest.mark.parametrize("model", [
    "openai/nvidia/nemotron-3-super-120b-a12b",
    "openai/nvidia/nemotron-3-ultra-550b-a55b",
])
def test_nim_cost_logged_in_db(tmp_path, model):
    """The billing DB row must have cost_usd > 0 for NIM models."""
    db = tmp_path / "kiko.db"
    logger = KikoSQLiteLogger(db_path=db)

    # Compute cost the same way litellm does before invoking the logger.
    # Before the fix cost_per_token raises → cost None → logger writes 0.
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
        )
        cost = prompt_cost + completion_cost
    except Exception:
        cost = None

    _run_logger(logger, model, cost)

    rows = _billing_rows(db)
    assert rows, "No billing row was written"
    assert rows[0][0] > 0, (
        f"cost_usd == {rows[0][0]} for {model}; "
        "model is likely missing from the price map"
    )
