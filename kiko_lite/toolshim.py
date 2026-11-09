# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Toolshim: turn a tool call that a weak model wrote as TEXT into a real Anthropic ``tool_use`` block.

Free models (Nemotron, some Mistral / Llama ...) often answer with the call in the text instead of the structured
``tool_calls`` field, so the harness sees plain text and the agent stalls. Deterministic parsing, no extra model call.

Active only for a model whose ``models.toolshim`` is "on", when the request carries tools and the upstream answer
has no structured tool call. Recognised forms (all with ``{"name": ..., "arguments"|"parameters": {...}}``):

* ``<tool_call>{json}</tool_call>``            (Hermes / Qwen)
* ``<TOOLCALL>[json list]</TOOLCALL>``         (Nemotron)
* ``[TOOL_CALLS][json list]``                  (Mistral)
* ``<|python_tag|>{json}``                     (Llama)
* a fenced ```json block, or a bare JSON object / list, that is the whole answer

A call is accepted only when its name is one of the request's tools and its arguments are a JSON object (or a
string holding one); anything else is left as text, nothing is ever invented. The rest of the text stays as text.
Reasoning (``thinking`` blocks) is not parsed.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Collection
from typing import Any

from kikolib import server

MAX_BUFFER = server.DEFAULT_MAX_BODY  # streamed text buffered per response; above it the answer passes through
_MARKER = re.compile(r"<tool_call>|<TOOLCALL>|\[TOOL_CALLS\]|<\|python_tag\|>")
_CLOSE = {"<tool_call>": "</tool_call>", "<TOOLCALL>": "</TOOLCALL>"}
_FENCE = re.compile(r"\A```(?:json|JSON)?[ \t]*\r?\n?(.*?)\r?\n?```\Z", re.DOTALL)
_DEC = json.JSONDecoder()


def tool_names(body: Any) -> frozenset[str]:
    """Names of the tools of an Anthropic request body (empty when it has none)."""
    tools = body.get("tools") if isinstance(body, dict) else None
    out = set()
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]:
                out.add(t["name"])
    return frozenset(out)


def _call(obj: Any, names: Collection[str]) -> dict | None:
    """One parsed JSON value -> {"name", "input"} when it is a valid call of a known tool, else None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # OpenAI style {"type": "function", "function": {...}}
        obj = obj["function"]
    name = obj.get("name")
    if not isinstance(name, str) or name not in names:
        return None
    args = obj["arguments"] if "arguments" in obj else obj.get("parameters")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except (ValueError, RecursionError):
            return None
    return {"name": name, "input": args} if isinstance(args, dict) else None


def _calls(value: Any, names: Collection[str]) -> list[dict] | None:
    """A JSON value (one call or a list of calls) -> the calls; None unless EVERY item is a valid call."""
    items = value if isinstance(value, list) else [value]
    out = [_call(i, names) for i in items]
    if not out or any(c is None for c in out):
        return None
    return out  # type: ignore[return-value]


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def extract(text: str, names: Collection[str]) -> tuple[str, list[dict]] | None:
    """Find the tool calls in ``text``: (the text left over, trimmed; the calls) or None when there is no call."""
    if not text or not names:
        return None
    found: list[dict] = []
    keep: list[str] = []
    pos = last = 0
    while True:
        m = _MARKER.search(text, pos)
        if m is None:
            break
        tag = m.group(0)
        start, calls, end = m.end(), None, m.end()
        if tag in _CLOSE:
            rest = text[start:]  # decode the JSON first: a close tag inside a JSON string is not the end
            lead = len(rest) - len(rest.lstrip())
            try:
                value, used = _DEC.raw_decode(rest[lead:])
            except (ValueError, RecursionError):
                value, used = None, 0
            after = rest[lead + used:].lstrip()
            if used and after.startswith(_CLOSE[tag]):
                calls, end = _calls(value, names), start + len(rest) - len(after) + len(_CLOSE[tag])
        else:  # the JSON follows the marker with no closing tag
            rest = text[start:]
            lead = len(rest) - len(rest.lstrip())
            try:
                value, used = _DEC.raw_decode(rest[lead:])
            except (ValueError, RecursionError):
                value, used = None, 0
            calls, end = _calls(value, names), start + lead + used
            while calls and tag == "<|python_tag|>":  # Llama may chain calls with ";" or a newline
                more = text[end:].lstrip(" \t\r\n;")
                if not more.startswith("{"):
                    break
                try:
                    v2, u2 = _DEC.raw_decode(more)
                except (ValueError, RecursionError):
                    break
                c2 = _calls(v2, names)
                if not c2:
                    break
                calls += c2
                end = len(text) - len(more) + u2
        if calls:
            keep.append(text[last:m.start()])
            found += calls
            last = pos = end
        else:
            pos = m.end()  # not a valid call: the marker and what follows stay as text
    if found:
        keep.append(text[last:])
        return "".join(keep).strip(), found
    whole = text.strip()
    fenced = _FENCE.match(whole)
    if fenced:
        whole = fenced.group(1).strip()
    if whole[:1] in ("{", "["):
        calls = _calls(_loads(whole), names)
        if calls:
            return "", calls
    return None


def _id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def blocks(calls: list[dict]) -> list[dict]:
    return [{"type": "tool_use", "id": _id(), "name": c["name"], "input": c["input"]} for c in calls]


def _stop(reason: Any, sequence: Any) -> tuple[Any, Any]:
    """stop_reason / stop_sequence after calls were made: an upstream stop on max_tokens stays (the call may be
    cut off, the client must see it); anything else becomes tool_use."""
    if reason == "max_tokens":
        return reason, sequence
    return "tool_use", None


def apply_message(msg: dict, names: Collection[str]) -> int:
    """Convert the text blocks of a non-stream Anthropic message in place; returns the number of calls made
    (0 = untouched: a structured tool_use is already there, or no valid call in the text)."""
    content = msg.get("content")
    if not isinstance(content, list) or any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
        return 0
    text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    got = extract("".join(str(b.get("text") or "") for b in text_blocks), names)
    if got is None:
        return 0
    rest, calls = got
    new = [b for b in content if not (isinstance(b, dict) and b.get("type") == "text")]
    if rest:
        new.append({"type": "text", "text": rest})
    new += blocks(calls)
    msg["content"] = new
    msg["stop_reason"], msg["stop_sequence"] = _stop(msg.get("stop_reason"), msg.get("stop_sequence"))
    return len(calls)


class ShimStream:
    """Wraps a StreamTranslator: buffers its events, and at ``finish()`` emits either the converted answer (text
    block with the leftover text, then one tool_use block per call, stop_reason tool_use) or, when nothing was
    converted, the buffered events unchanged. Over ``MAX_BUFFER`` of text the buffer is flushed and the rest
    streams through as before. ``converted`` = number of calls made."""

    def __init__(self, inner: Any, names: Collection[str], limit: int | None = None) -> None:
        self.inner, self.names, self.limit = inner, names, MAX_BUFFER if limit is None else limit
        self.converted = 0
        self._buf: list[tuple[str, dict]] = []
        self._size = 0
        self._pass = False

    @property
    def complete(self) -> bool:
        return self.inner.complete

    def _take(self, events: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        if self._pass:
            return events
        for _, ev in events:
            d = ev.get("delta") if isinstance(ev, dict) else None
            if isinstance(d, dict):
                self._size += len(str(d.get("text") or d.get("thinking") or d.get("partial_json") or ""))
        self._buf += events
        if self._size > self.limit:
            self._pass, out, self._buf = True, self._buf, []
            return out
        return []

    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        return self._take(self.inner.feed(chunk))

    def finish(self) -> list[tuple[str, dict]]:
        events = self._take(self.inner.finish())
        if self._pass:
            return events
        buf, self._buf = self._buf, []
        return self._convert(buf)

    def _convert(self, buf: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        kinds: dict[int, str] = {}
        parts: dict[int, list[str]] = {}
        start: dict | None = None
        delta: dict | None = None
        for name, ev in buf:
            if name == "message_start":
                start = ev
            elif name == "message_delta":
                delta = ev
            elif name == "content_block_start":
                i = ev["index"]
                kinds[i] = ev["content_block"].get("type", "text")
                parts[i] = [ev["content_block"].get("text") or ev["content_block"].get("thinking") or ""]
            elif name == "content_block_delta":
                d = ev["delta"]
                parts.setdefault(ev["index"], []).append(str(d.get("text") or d.get("thinking") or ""))
        if start is None or delta is None or "tool_use" in kinds.values() or any(i not in kinds for i in parts):
            return buf  # (a delta for a block that never started: pass the events through as they are)
        got = extract("".join("".join(parts[i]) for i in sorted(parts) if kinds[i] == "text"), self.names)
        if got is None:
            return buf
        rest, calls = got
        self.converted = len(calls)
        out: list[tuple[str, dict]] = [("message_start", start)]
        idx = 0

        def block(b: dict, delta_ev: dict | None) -> None:
            nonlocal idx
            out.append(("content_block_start", {"type": "content_block_start", "index": idx, "content_block": b}))
            if delta_ev is not None:
                out.append(("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": delta_ev}))
            out.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
            idx += 1

        for i in sorted(parts):
            if kinds[i] == "thinking" and "".join(parts[i]):
                block({"type": "thinking", "thinking": "", "signature": ""},
                      {"type": "thinking_delta", "thinking": "".join(parts[i])})
        if rest:
            block({"type": "text", "text": ""}, {"type": "text_delta", "text": rest})
        for b in blocks(calls):
            block({**b, "input": {}}, {"type": "input_json_delta", "partial_json": json.dumps(b["input"])})
        old = delta.get("delta", {})
        reason, seq = _stop(old.get("stop_reason"), old.get("stop_sequence"))
        d2 = {**delta, "delta": {**old, "stop_reason": reason, "stop_sequence": seq}}
        out += [("message_delta", d2), ("message_stop", {"type": "message_stop"})]
        return out
