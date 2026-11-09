# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""OpenAI Responses FRONT of kiko-lite: ``POST /v1/responses`` (the wire the codex CLI speaks).

Pure functions and one small stream translator, no network, stdlib only; the same design as ``chat_front``: the
request is converted to the Anthropic Messages body, the gateway pipeline (resolver, failover, cooldowns, billing,
text log) runs unchanged on it, and the Anthropic result is converted back.

* Request: ``input`` is a string or a list of items. ``message`` items: ``developer`` / ``system`` -> ``system``,
  ``user`` / ``assistant`` -> turns (``input_text`` / ``output_text`` / ``input_image``); ``function_call`` ->
  ``tool_use`` (``call_id`` is the id, the ``arguments`` STRING is parsed, invalid JSON -> ``{"_raw": text}``);
  ``function_call_output`` -> ``tool_result`` (string, or a list of text / image items); ``reasoning`` and
  ``web_search_call`` items are dropped; any other item type is a 400. ``instructions`` (if a client sends it)
  goes to ``system`` first. Tools of type ``function`` are mapped; every other tool type (``web_search``,
  ``custom``, ``namespace`` ...) is DROPPED, not rejected: the request is still servable and codex sends such
  tools by default. ``parallel_tool_calls: false`` -> ``tool_choice.disable_parallel_tool_use``;
  ``max_output_tokens`` -> ``max_tokens`` (omitted when absent, like the chat front). store, include, reasoning,
  text, client_metadata, prompt_cache_key, service_tier and unknown fields are ignored.
* Stream: every frame is ``data: {json}`` with ``type`` (and ``sequence_number``); a text block gives
  ``output_item.added``, ``output_text.delta`` ..., ``output_item.done`` (message with the full text); a tool_use
  block gives ``output_item.added``, ``function_call_arguments.delta`` ..., ``output_item.done`` (function_call,
  ``arguments`` the full JSON string, ``call_id``); the end is ``response.completed`` (``response.incomplete`` for
  max_tokens); an error is ``response.failed`` and the stream ends. Thinking is dropped. Keepalives are SSE
  comment lines.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import chat_front
from .chat_front import ChatRequestError
from .providers import openai_compat as oc

_DROP_ITEMS = frozenset({"reasoning", "web_search_call"})

# error.code strings the codex CLI matches (codex-rs/codex-api/src/sse/responses_error.rs); any other code makes codex
# retry, so a terminal failure must carry one of the terminal codes below
_CONTEXT_HINTS = ("prompt is too long", "context_length_exceeded", "context length", "context window",
                  "maximum context", "too many tokens", "token count exceeds", "exceeds the maximum number of tokens",
                  "input is too long", "request too large")
_QUOTA_HINTS = ("insufficient_quota", "exceeded your current quota", "credit balance is too low",
                "out of credits", "quota exceeded", "spending cap")
# a quota text that also carries one of these is a per-minute limit that clears by itself (codex retries it)
_RETRY_HINTS = ("retry in", "retrydelay", "per minute", "perminute", "per_minute", "requests per min",
                "tokens per min")
_TERMINAL_4XX = frozenset({"invalid_request_error", "request_too_large", "permission_error"})


def error_code(typ: str, message: str) -> str | None:
    """Anthropic error type + message -> the ``error.code`` codex understands (None: keep the chat-front code)."""
    low = (message or "").lower()
    if (typ in _TERMINAL_4XX or typ == "rate_limit_error") and any(h in low for h in _QUOTA_HINTS) \
            and not any(h in low for h in _RETRY_HINTS):
        return "insufficient_quota"
    if typ == "rate_limit_error":
        return "rate_limit_exceeded"
    if typ == "overloaded_error":
        return "server_is_overloaded"
    if typ == "request_too_large" or (typ == "invalid_request_error" and any(h in low for h in _CONTEXT_HINTS)):
        return "context_length_exceeded"
    if typ in _TERMINAL_4XX:
        return "invalid_prompt"
    return None


def error_body(typ: str, message: str, param: str | None = None) -> dict:
    """OpenAI-shaped error body with the codex ``code`` (non-stream HTTP errors of /v1/responses)."""
    body = chat_front.error_body(typ, message, param)
    code = error_code(typ, message)
    if code:
        body["error"]["code"] = code
    return body


# ---- request ------------------------------------------------------------------------------------

def _assistant_text(content: Any, where: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ChatRequestError(f"{where}: Input should be a string or a list of content parts", where)
    return "\n".join(p["text"] for p in content if isinstance(p, dict)
                     and p.get("type") in ("output_text", "input_text", "text") and isinstance(p.get("text"), str))


def _output_content(out: Any, where: str) -> str | list[dict]:
    """function_call_output ``output``: a string, or a list of input_text / input_image items."""
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if not isinstance(out, list):
        raise ChatRequestError(f"{where}: Input should be a string or a list of content items", where)
    blocks = chat_front._user_blocks(out, where)
    if all(b["type"] == "text" for b in blocks):
        return "\n".join(b["text"] for b in blocks)
    return blocks


def _tools(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ChatRequestError("tools: Input should be a list", "tools")
    out = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            raise ChatRequestError(f"tools.{i}: Input should be a valid dictionary", f"tools.{i}")
        if t.get("type") != "function":
            continue  # web_search, custom, namespace ...: nothing to map it to
        if not isinstance(t.get("name"), str) or not t["name"]:
            raise ChatRequestError(f"tools.{i}.name: Field required", f"tools.{i}")
        params = t.get("parameters")
        tool = {"name": t["name"], "input_schema": dict(params) if isinstance(params, dict)
                else {"type": "object", "properties": {}}}
        if isinstance(t.get("description"), str) and t["description"]:
            tool["description"] = t["description"]
        out.append(tool)
    return out


def _tool_choice(tc: Any) -> dict:
    if isinstance(tc, str):
        kinds = {"auto": "auto", "none": "none", "required": "any"}
        if tc in kinds:
            return {"type": kinds[tc]}
    elif isinstance(tc, dict) and tc.get("type") == "function":
        name = tc.get("name")
        if isinstance(name, str) and name:
            return {"type": "tool", "name": name}
    raise ChatRequestError("tool_choice: Input should be 'auto', 'none', 'required' or a function choice",
                           "tool_choice")


def convert_request(body: Any) -> tuple[dict, dict]:
    """Responses request -> (Anthropic Messages body, options ``{"stream", "model"}``)."""
    if not isinstance(body, dict):
        raise ChatRequestError("request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ChatRequestError("model: Field required", "model")
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    if not isinstance(items, list):
        raise ChatRequestError("input: Field required", "input")
    system: list[str] = []
    if isinstance(body.get("instructions"), str) and body["instructions"]:
        system.append(body["instructions"])
    out: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for i, it in enumerate(items):
        where = f"input.{i}"
        if not isinstance(it, dict):
            raise ChatRequestError(f"{where}: Input should be a valid dictionary", where)
        kind = it.get("type") or ("message" if "role" in it else None)
        if kind in _DROP_ITEMS:
            continue
        if kind == "message":
            role = it.get("role")
            if role in ("system", "developer"):
                text = chat_front._text_parts(it.get("content"), f"{where}.content")
                if text:
                    system.append(text)
            elif role == "user":
                add("user", chat_front._user_blocks(it.get("content"), f"{where}.content"))
            elif role == "assistant":
                text = _assistant_text(it.get("content"), f"{where}.content")
                add("assistant", [{"type": "text", "text": text}] if text else [])
            else:
                raise ChatRequestError(f"{where}.role: Input should be 'system', 'developer', 'user' or "
                                       "'assistant'", f"{where}.role")
        elif kind == "function_call":
            name, cid = it.get("name"), it.get("call_id")
            if not isinstance(name, str) or not name:
                raise ChatRequestError(f"{where}.name: Field required", f"{where}.name")
            add("assistant", [{"type": "tool_use", "id": oc.anthropic_id(cid if isinstance(cid, str) and cid else None),
                               "name": name, "input": oc.parse_arguments(it.get("arguments"))}])
        elif kind == "function_call_output":
            cid = it.get("call_id")
            if not isinstance(cid, str) or not cid:
                raise ChatRequestError(f"{where}.call_id: Field required", f"{where}.call_id")
            add("user", [{"type": "tool_result", "tool_use_id": oc.anthropic_id(cid),
                          "content": _output_content(it.get("output"), f"{where}.output")}])
        else:
            raise ChatRequestError(f"{where}.type: unsupported input item type {kind!r}", f"{where}.type")
    a: dict[str, Any] = {"model": model, "messages": out}
    mt = body.get("max_output_tokens")
    if mt is not None:
        a["max_tokens"] = mt  # absent: the upstream picks its own default for the resolved model
    if system:
        a["system"] = "\n\n".join(system)
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            a[key] = body[key]
    tools = _tools(body["tools"]) if body.get("tools") is not None else []
    if tools:
        a["tools"] = tools
        if body.get("tool_choice") is not None:
            a["tool_choice"] = _tool_choice(body["tool_choice"])
        if body.get("parallel_tool_calls") is False:
            tc = a.setdefault("tool_choice", {"type": "auto"})
            if tc["type"] != "none":
                tc["disable_parallel_tool_use"] = True
    stream = bool(body.get("stream"))
    if stream:
        a["stream"] = True
    return a, {"stream": stream, "model": model}


# ---- response -----------------------------------------------------------------------------------

def _usage(u: Any) -> dict:
    u = u if isinstance(u, dict) else {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    p = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
    c = int(u.get("output_tokens") or 0)
    return {"input_tokens": p, "output_tokens": c, "total_tokens": p + c,
            "input_tokens_details": {"cached_tokens": cached}, "output_tokens_details": {"reasoning_tokens": 0}}


def new_id() -> str:
    return "resp_" + uuid.uuid4().hex[:24]


def _call_id(block_id: Any) -> str:
    return oc.upstream_id(str(block_id or "")) or oc.upstream_id(oc.anthropic_id(None))


def _message_item(item_id: str, text: str, status: str = "completed") -> dict:
    return {"type": "message", "id": item_id, "status": status, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _call_item(item_id: str, call_id: str, name: str, arguments: str, status: str = "completed") -> dict:
    return {"type": "function_call", "id": item_id, "status": status, "call_id": call_id, "name": name,
            "arguments": arguments}


def _response(rid: str, model: str, created: int, status: str, output: list, usage: dict | None = None,
              stop_reason: Any = None) -> dict:
    r: dict[str, Any] = {"id": rid, "object": "response", "created_at": created, "status": status, "model": model,
                         "output": output, "error": None,
                         "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None}
    if usage is not None:
        r["usage"] = usage
    return r


def _status(stop_reason: Any) -> str:
    return "incomplete" if stop_reason == "max_tokens" else "completed"


def convert_response(message: dict, model: str) -> dict:
    """Anthropic message -> Responses ``response`` object (stream false)."""
    output: list[dict] = []
    for b in message.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            output.append(_message_item("msg_" + uuid.uuid4().hex[:24], str(b.get("text") or "")))
        elif b.get("type") == "tool_use":
            output.append(_call_item("fc_" + uuid.uuid4().hex[:24], _call_id(b.get("id")), b.get("name", ""),
                                     oc._arguments(b.get("input"))))
    return _response(new_id(), model, int(time.time()), _status(message.get("stop_reason")), output,
                     _usage(message.get("usage")))


# ---- stream -------------------------------------------------------------------------------------

class ResponsesStream:
    """Anthropic stream events -> Responses SSE frames (one instance per response). Same interface as ChatStream."""

    def __init__(self, model: str) -> None:
        self.model, self.id, self.created = model, new_id(), int(time.time())
        self._blocks: dict[int, dict] = {}  # Anthropic block index -> state of the open text / tool_use block
        self._output: list[tuple[int, dict]] = []  # finished (output_index, item)
        self._n = 0  # next output_index
        self._seq = 0
        self._usage: dict | None = None
        self._stop: Any = None

    @staticmethod
    def ping() -> bytes:
        return b": keepalive\n\n"

    def _frame(self, typ: str, **fields: Any) -> bytes:
        obj = {"type": typ, **fields, "sequence_number": self._seq}
        self._seq += 1
        return b"data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n\n"

    def _resp(self, status: str, **extra: Any) -> dict:
        return {**_response(self.id, self.model, self.created, status, [], None), **extra}

    def error(self, typ: str, message: str) -> bytes:
        """A mid-stream error: ``response.failed``; the stream ends after it (no ``[DONE]`` in this protocol)."""
        err = error_body(typ, message)["error"]
        return self._frame("response.failed", response=self._resp(
            "failed", error={"code": err["code"] or err["type"], "message": message}))

    def event(self, name: str, ev: dict) -> list[bytes]:
        if name == "message_start":
            return [self._frame("response.created", response=self._resp("in_progress"))]
        if name == "content_block_start":
            blk = ev.get("content_block") or {}
            idx = ev.get("index", len(self._blocks))
            if blk.get("type") == "text":
                st = {"kind": "text", "oi": self._n, "item": "msg_" + uuid.uuid4().hex[:24], "text": []}
                self._blocks[idx] = st
                self._n += 1
                return [self._frame("response.output_item.added", output_index=st["oi"],
                                    item={"type": "message", "id": st["item"], "status": "in_progress",
                                          "role": "assistant", "content": []}),
                        self._frame("response.content_part.added", item_id=st["item"], output_index=st["oi"],
                                    content_index=0, part={"type": "output_text", "text": "", "annotations": []})]
            if blk.get("type") == "tool_use":
                st = {"kind": "tool", "oi": self._n, "item": "fc_" + uuid.uuid4().hex[:24],
                      "call": _call_id(blk.get("id")), "name": blk.get("name", ""), "args": []}
                self._blocks[idx] = st
                self._n += 1
                return [self._frame("response.output_item.added", output_index=st["oi"],
                                    item=_call_item(st["item"], st["call"], st["name"], "", "in_progress"))]
            return []  # thinking and the like are not part of this front
        st = self._blocks.get(ev.get("index"))
        if name == "content_block_delta" and st is not None:
            d = ev.get("delta") or {}
            if st["kind"] == "text" and d.get("type") == "text_delta" and d.get("text"):
                st["text"].append(str(d["text"]))
                return [self._frame("response.output_text.delta", item_id=st["item"], output_index=st["oi"],
                                    content_index=0, delta=str(d["text"]))]
            if st["kind"] == "tool" and d.get("type") == "input_json_delta" and d.get("partial_json"):
                st["args"].append(str(d["partial_json"]))
                return [self._frame("response.function_call_arguments.delta", item_id=st["item"],
                                    output_index=st["oi"], delta=str(d["partial_json"]))]
            return []
        if name == "content_block_stop" and st is not None:
            del self._blocks[ev["index"]]
            return self._finish_block(st)
        if name == "message_delta":
            self._stop = (ev.get("delta") or {}).get("stop_reason")
            self._usage = _usage(ev.get("usage"))
        return []

    def _finish_block(self, st: dict) -> list[bytes]:
        if st["kind"] == "text":
            text = "".join(st["text"])
            item = _message_item(st["item"], text)
            frames = [self._frame("response.output_text.done", item_id=st["item"], output_index=st["oi"],
                                  content_index=0, text=text),
                      self._frame("response.content_part.done", item_id=st["item"], output_index=st["oi"],
                                  content_index=0, part=item["content"][0])]
        else:
            args = "".join(st["args"]) or "{}"
            item = _call_item(st["item"], st["call"], st["name"], args)
            frames = [self._frame("response.function_call_arguments.done", item_id=st["item"],
                                  output_index=st["oi"], arguments=args)]
        self._output.append((st["oi"], item))
        frames.append(self._frame("response.output_item.done", output_index=st["oi"], item=item))
        return frames

    def close(self) -> list[bytes]:
        """After the last event: open blocks are finished, then ``response.completed`` (or ``.incomplete``)."""
        out: list[bytes] = []
        for idx in sorted(self._blocks):
            out += self._finish_block(self._blocks[idx])
        self._blocks.clear()
        status = _status(self._stop)
        items = [it for _, it in sorted(self._output, key=lambda p: p[0])]
        resp = _response(self.id, self.model, self.created, status, items, self._usage or _usage(None))
        out.append(self._frame("response.incomplete" if status == "incomplete" else "response.completed",
                               response=resp))
        return out
