# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Fallback token estimate, used only when an upstream returns no usage.

This is an estimate, not a tokenizer: ceil(utf-8 bytes / 4) plus small fixed overheads.
"""

from __future__ import annotations

import json
from typing import Any

_PER_MESSAGE = 4
_PER_TOOL = 8


def estimate_text(text: str) -> int:
    """Estimate tokens of a string: ceil(len(utf-8 bytes) / 4); 0 for empty."""
    if not text:
        return 0
    return -(-len(text.encode("utf-8", "replace")) // 4)


def _json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(obj)


def _blocks(content: Any) -> int:
    if isinstance(content, str):
        return estimate_text(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for b in content:
        if isinstance(b, str):
            total += estimate_text(b)
        elif isinstance(b, dict):
            t = b.get("type")
            if t == "text":
                total += estimate_text(str(b.get("text", "")))
            elif t == "thinking":
                total += estimate_text(str(b.get("thinking", "")))
            elif t == "tool_use":
                total += estimate_text(str(b.get("name", ""))) + estimate_text(_json(b.get("input", {})))
            elif t == "tool_result":
                total += _blocks(b.get("content", ""))
    return total


def estimate_messages(body: dict[str, Any]) -> int:
    """Estimate input tokens of an Anthropic Messages request body."""
    total = _blocks(body.get("system", ""))
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            total += _PER_MESSAGE + _blocks(m.get("content", ""))
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            total += _PER_TOOL + estimate_text(str(t.get("name", "")))
            total += estimate_text(str(t.get("description", "")))
            total += estimate_text(_json(t.get("input_schema", {})))
    return total
