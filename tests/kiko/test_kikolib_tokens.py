# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.tokens: fallback estimates."""
from kikolib import tokens


def test_estimate_text():
    assert tokens.estimate_text("") == 0
    assert tokens.estimate_text("a") == 1
    assert tokens.estimate_text("abcd") == 1
    assert tokens.estimate_text("abcde") == 2
    assert tokens.estimate_text("é" * 4) == 2  # 8 bytes


def test_estimate_messages_shapes():
    assert tokens.estimate_messages({}) == 0
    simple = {"messages": [{"role": "user", "content": "x" * 40}]}
    assert tokens.estimate_messages(simple) == 10 + 4
    blocks = {
        "system": [{"type": "text", "text": "x" * 40}],
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "y" * 40},
                {"type": "tool_use", "id": "1", "name": "run", "input": {"cmd": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": [{"type": "text", "text": "z" * 40}]},
                {"type": "tool_result", "tool_use_id": "1", "content": "w" * 40},
            ]},
        ],
        "tools": [{"name": "run", "description": "d" * 40, "input_schema": {"type": "object"}}],
    }
    n = tokens.estimate_messages(blocks)
    assert n > 10 + 10 + 10 + 10 + 10  # all text parts counted
    bigger = dict(blocks, system="x" * 400)
    assert tokens.estimate_messages(bigger) > n


def test_estimate_messages_tolerates_junk():
    assert tokens.estimate_messages({"messages": [None, {"content": 5}], "tools": ["x"]}) >= 0
