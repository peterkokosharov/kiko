# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""OpenAI chat completions FRONT of kiko-lite: ``POST /v1/chat/completions`` and ``GET /v1/models``.

Pure functions and one small stream translator, no network, stdlib only. The OpenAI chat request is converted
to the Anthropic Messages body, the existing gateway pipeline (resolver, failover, cooldowns, billing, text
log) runs unchanged on it, and the Anthropic result (a message, or the stream events) is converted back.

* Request: system / developer messages -> ``system``; ``role: tool`` messages -> ``tool_result`` blocks of a
  user turn (consecutive ones merged, order kept); assistant ``tool_calls`` -> ``tool_use`` blocks (invalid
  JSON arguments -> ``{"_raw": text}``); ``data:`` image URLs -> base64 image sources, http(s) -> url sources;
  ``max_completion_tokens`` or ``max_tokens`` -> ``max_tokens`` (omitted when the client sends none: the
  upstream applies its own per-model default, so a small-output model never gets a too large limit); ``n`` > 1 is a 400; unknown
  fields are ignored. Tool call ids keep the ``toolu_`` round trip of ``providers.openai_compat``.
* Stream chunks contain only ``id``, ``object``, ``created``, ``model``, ``choices`` (some clients raise on any
  other key); every tool-call fragment carries ``id`` and ``type``; the optional usage chunk (stream_options.
  include_usage) has an empty ``choices`` list. Keepalives are SSE comment lines.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .providers import openai_compat as oc

_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
           "refusal": "content_filter"}
# Anthropic error type -> (OpenAI error type, OpenAI code)
_ERRORS = {
    "invalid_request_error": ("invalid_request_error", None),
    "authentication_error": ("authentication_error", "invalid_api_key"),
    "permission_error": ("invalid_request_error", "permission_denied"),
    "not_found_error": ("invalid_request_error", "model_not_found"),
    "request_too_large": ("invalid_request_error", "request_too_large"),
    "rate_limit_error": ("rate_limit_error", "rate_limit_exceeded"),
    "overloaded_error": ("server_error", "overloaded"),
}


class ChatRequestError(ValueError):
    """The OpenAI request cannot be served (a 400 for the client)."""

    def __init__(self, message: str, param: str | None = None) -> None:
        super().__init__(message)
        self.message, self.param = message, param


def error_body(typ: str, message: str, param: str | None = None) -> dict:
    """OpenAI-shaped error ``{"error": {"message", "type", "param", "code"}}`` from an Anthropic error type."""
    otype, code = _ERRORS.get(typ, ("server_error", None))
    return {"error": {"message": message, "type": otype, "param": param, "code": code}}


# ---- request ------------------------------------------------------------------------------------

def _text_parts(content: Any, where: str) -> str:
    """Text of a message content (string or list of parts); non-text parts are ignored."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ChatRequestError(f"{where}: Input should be a string or a list of content parts", where)
    out = []
    for p in content:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and p.get("type") in ("text", "input_text") and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "\n".join(out)


def _image_block(part: dict, where: str) -> dict:
    img = part.get("image_url")
    url = img.get("url") if isinstance(img, dict) else img
    if not isinstance(url, str) or not url:
        raise ChatRequestError(f"{where}.image_url: a url is required", where)
    if url.startswith("data:"):
        head, sep, data = url[5:].partition(",")
        mime, _, enc = head.partition(";")
        if not sep or enc.lower() != "base64" or not data:
            raise ChatRequestError(f"{where}.image_url: only base64 data URLs are supported", where)
        return {"type": "image", "source": {"type": "base64", "media_type": mime or "image/png", "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise ChatRequestError(f"{where}.image_url: the url must be http(s) or a data URL", where)


def _user_blocks(content: Any, where: str) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ChatRequestError(f"{where}: Input should be a string or a list of content parts", where)
    blocks: list[dict] = []
    for j, p in enumerate(content):
        if isinstance(p, str):
            if p:
                blocks.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            raise ChatRequestError(f"{where}.{j}: Input should be a valid dictionary", f"{where}.{j}")
        kind = p.get("type")
        if kind in ("text", "input_text"):
            if not isinstance(p.get("text"), str):
                raise ChatRequestError(f"{where}.{j}.text: Input should be a valid string", f"{where}.{j}.text")
            if p["text"]:
                blocks.append({"type": "text", "text": p["text"]})
        elif kind in ("image_url", "input_image"):
            blocks.append(_image_block(p, f"{where}.{j}"))
        # other part types (input_audio, file ...) are ignored
    return blocks


def _assistant_blocks(m: dict, where: str) -> list[dict]:
    blocks: list[dict] = []
    text = _text_parts(m.get("content"), f"{where}.content")
    if text:
        blocks.append({"type": "text", "text": text})
    calls = m.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise ChatRequestError(f"{where}.tool_calls: Input should be a list", f"{where}.tool_calls")
    for j, tc in enumerate(calls or []):
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise ChatRequestError(f"{where}.tool_calls.{j}.function.name: Field required",
                                   f"{where}.tool_calls.{j}")
        tid = tc.get("id")
        blocks.append({"type": "tool_use", "id": oc.anthropic_id(tid if isinstance(tid, str) and tid else None),
                       "name": fn["name"], "input": oc.parse_arguments(fn.get("arguments"))})
    return blocks


def _tools(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ChatRequestError("tools: Input should be a list", "tools")
    out = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            raise ChatRequestError(f"tools.{i}: Input should be a valid dictionary", f"tools.{i}")
        fn = t.get("function") if t.get("type", "function") == "function" else None
        if fn is None:
            continue  # not a function tool: nothing to map it to
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
            raise ChatRequestError(f"tools.{i}.function.name: Field required", f"tools.{i}")
        params = fn.get("parameters")
        schema = dict(params) if isinstance(params, dict) else {"type": "object", "properties": {}}
        tool = {"name": fn["name"], "input_schema": schema}
        if isinstance(fn.get("description"), str) and fn["description"]:
            tool["description"] = fn["description"]
        out.append(tool)
    return out


def _tool_choice(tc: Any) -> dict:
    if isinstance(tc, str):
        kinds = {"auto": "auto", "none": "none", "required": "any"}
        if tc in kinds:
            return {"type": kinds[tc]}
    elif isinstance(tc, dict) and tc.get("type") == "function":
        fn = tc.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn["name"]:
            return {"type": "tool", "name": fn["name"]}
    raise ChatRequestError("tool_choice: Input should be 'auto', 'none', 'required' or a function choice",
                           "tool_choice")


def convert_request(body: Any) -> tuple[dict, dict]:
    """OpenAI chat request -> (Anthropic Messages body, options ``{"stream", "include_usage", "model"}``)."""
    if not isinstance(body, dict):
        raise ChatRequestError("request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ChatRequestError("model: Field required", "model")
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        raise ChatRequestError("messages: Field required", "messages")
    n = body.get("n")
    if n is not None and (isinstance(n, bool) or n != 1):
        raise ChatRequestError("n: only n=1 is supported", "n")
    system: list[str] = []
    out: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for i, m in enumerate(msgs):
        where = f"messages.{i}"
        if not isinstance(m, dict):
            raise ChatRequestError(f"{where}: Input should be a valid dictionary", where)
        role = m.get("role")
        if role in ("system", "developer"):
            text = _text_parts(m.get("content"), f"{where}.content")
            if text:
                system.append(text)
        elif role == "user":
            add("user", _user_blocks(m.get("content"), f"{where}.content"))
        elif role == "assistant":
            add("assistant", _assistant_blocks(m, where))
        elif role in ("tool", "function"):
            tid = m.get("tool_call_id")
            add("user", [{"type": "tool_result", "tool_use_id": oc.anthropic_id(tid) if isinstance(tid, str) and tid
                          else "", "content": _text_parts(m.get("content"), f"{where}.content")}])
        else:
            raise ChatRequestError(f"{where}.role: Input should be 'system', 'developer', 'user', 'assistant' "
                                   "or 'tool'", f"{where}.role")
    mt = body.get("max_completion_tokens")
    if mt is None:
        mt = body.get("max_tokens")
    a: dict[str, Any] = {"model": model, "messages": out}
    if mt is not None:
        a["max_tokens"] = mt  # absent: the upstream picks its own default for the resolved model
    if system:
        a["system"] = "\n\n".join(system)
    stop = body.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    if stop is not None:
        a["stop_sequences"] = stop  # validated as a list of strings with the converted body
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            a[key] = body[key]
    if body.get("tools") is not None:
        tools = _tools(body["tools"])
        if tools:
            a["tools"] = tools
    if body.get("tool_choice") is not None:
        a["tool_choice"] = _tool_choice(body["tool_choice"])
    stream = bool(body.get("stream"))
    if stream:
        a["stream"] = True
    so = body.get("stream_options")
    include = bool(isinstance(so, dict) and so.get("include_usage"))
    return a, {"stream": stream, "include_usage": include, "model": model}


# ---- non-stream response ------------------------------------------------------------------------

def _usage(u: Any) -> dict[str, int]:
    u = u if isinstance(u, dict) else {}
    p = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
        + int(u.get("cache_creation_input_tokens") or 0)
    c = int(u.get("output_tokens") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def finish_reason(stop_reason: Any) -> str:
    return _FINISH.get(stop_reason, "stop")


def new_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def convert_response(message: dict, model: str) -> dict:
    """Anthropic message -> chat.completion."""
    texts: list[str] = []
    calls: list[dict] = []
    for b in message.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(str(b.get("text") or ""))
        elif b.get("type") == "tool_use":
            calls.append({"id": oc.upstream_id(str(b.get("id") or "")) or oc.upstream_id(oc.anthropic_id(None)),
                          "type": "function",
                          "function": {"name": b.get("name", ""), "arguments": oc._arguments(b.get("input"))}})
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(texts) if texts else None}
    if calls:
        msg["tool_calls"] = calls
    reason = finish_reason(message.get("stop_reason"))
    if calls and reason == "stop":
        reason = "tool_calls"
    return {"id": new_id(), "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": reason}], "usage": _usage(message.get("usage"))}


# ---- stream -------------------------------------------------------------------------------------

class ChatStream:
    """Anthropic stream events -> OpenAI ``chat.completion.chunk`` SSE frames (one instance per response)."""

    def __init__(self, model: str, include_usage: bool = False) -> None:
        self.model, self.include_usage = model, include_usage
        self.id, self.created = new_id(), int(time.time())
        self._tools: dict[int, dict] = {}  # Anthropic block index -> {"index", "id"}
        self._usage: dict[str, int] | None = None
        self._finished = False

    @staticmethod
    def ping() -> bytes:
        return b": keepalive\n\n"

    @staticmethod
    def _frame(obj: Any) -> bytes:
        return b"data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n\n"

    def _chunk(self, delta: dict, finish: str | None = None) -> bytes:
        return self._frame({"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                            "model": self.model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    def error(self, typ: str, message: str) -> bytes:
        """A mid-stream error: the OpenAI error frame, then ``[DONE]`` so the client's read loop ends."""
        return self._frame(error_body(typ, message)) + b"data: [DONE]\n\n"

    def event(self, name: str, ev: dict) -> list[bytes]:
        if name == "message_start":
            return [self._chunk({"role": "assistant"})]
        if name == "content_block_start":
            blk = ev.get("content_block") or {}
            if blk.get("type") == "tool_use":
                tool = {"index": len(self._tools),
                        "id": oc.upstream_id(str(blk.get("id") or "")) or oc.upstream_id(oc.anthropic_id(None))}
                self._tools[ev.get("index", len(self._tools))] = tool
                return [self._chunk({"tool_calls": [{"index": tool["index"], "id": tool["id"], "type": "function",
                                                     "function": {"name": blk.get("name", ""), "arguments": ""}}]})]
            return []
        if name == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                return [self._chunk({"content": str(d["text"])})]
            if d.get("type") == "input_json_delta" and d.get("partial_json"):
                tool = self._tools.get(ev.get("index"))
                if tool is not None:
                    return [self._chunk({"tool_calls": [{"index": tool["index"], "id": tool["id"],
                                                         "type": "function",
                                                         "function": {"arguments": str(d["partial_json"])}}]})]
            return []  # thinking deltas and the like are not part of chat completions
        if name == "message_delta":
            reason = finish_reason((ev.get("delta") or {}).get("stop_reason"))
            if self._tools and reason == "stop":
                reason = "tool_calls"
            self._usage = _usage(ev.get("usage"))
            self._finished = True
            return [self._chunk({}, reason)]
        return []

    def close(self) -> list[bytes]:
        """After the last event: the usage chunk (when asked for) and ``[DONE]``."""
        out: list[bytes] = []
        if self.include_usage and self._usage is not None:
            out.append(self._frame({"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                                    "model": self.model, "choices": [], "usage": self._usage}))
        out.append(b"data: [DONE]\n\n")
        return out
