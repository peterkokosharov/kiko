# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""OpenAI chat completions (NVIDIA, Groq, DashScope, OpenAI ...) <-> Anthropic Messages translation.

Pure functions, no network. Choices worth knowing:

* Request fields are built from a whitelist, so unknown client fields (metadata, cache_control, betas,
  api_base, extra_headers ...) never reach the upstream. ``thinking`` and ``top_k`` are not sent.
* Tool call ids: the Anthropic id is ``toolu_`` + the upstream id; on the way back the prefix is removed,
  so the mapping is reversible without state. Invalid JSON arguments become ``{"_raw": "<text>"}`` and an
  input of exactly that shape is sent back as the raw argument string.
* tool_result blocks become ``role: tool`` messages placed before the rest of that user turn; images
  inside a tool_result are replaced by a text note (chat completions tool messages are text only).
* Reasoning: ``reasoning_content`` / ``reasoning`` fields, or ``<think>...</think>`` at the very start of
  the content, become a ``thinking`` block (empty signature). Thinking blocks from history are dropped.
* The upstream never says which stop sequence matched (and strips it); ``stop_sequence`` is reported only
  when the returned text still ends with one of the requested sequences.
* Streaming: tool call arguments are streamed as they arrive for the call being built. A delta for a call
  whose block was already closed (parallel calls interleaved out of order) is dropped.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

ID_PREFIX = "toolu_"
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
_FINISH = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use",
           "content_filter": "refusal"}


class OpenAIStreamError(Exception):
    """An ``error`` object arrived in a response that was otherwise HTTP 200 (or inside a stream)."""

    def __init__(self, message: str, code: Any = None, kind: str = "") -> None:
        super().__init__(message)
        self.message, self.code, self.kind = message, code, kind


def endpoint(base_url: str) -> str:
    """Chat completions URL; the API key never goes in the URL."""
    return base_url.rstrip("/") + "/chat/completions"


def headers(api_key: str, style: str = "bearer") -> dict[str, str]:
    """Auth style "bearer" or "header:<Name>"."""
    h = {"content-type": "application/json"}
    if style.startswith("header:"):
        h[style[7:]] = api_key
    else:
        h["authorization"] = "Bearer " + api_key
    return h


# ---- ids ----------------------------------------------------------------------------------------

def anthropic_id(upstream_id: str | None) -> str:
    return ID_PREFIX + upstream_id if upstream_id else ID_PREFIX + uuid.uuid4().hex[:24]


def upstream_id(tid: str) -> str:
    if tid.startswith(ID_PREFIX) and len(tid) > len(ID_PREFIX):
        return tid[len(ID_PREFIX):]
    return tid


# ---- request ------------------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _image_part(b: dict) -> dict | None:
    src = b.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return {"type": "image_url",
                "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _tool_result_text(b: dict) -> str:
    content = b.get("content")
    text = _text_of(content)
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "image" for c in content):
        text = (text + "\n" if text else "") + "[image omitted: images in tool results are not supported]"
    return text or "(no output)"


def _user_messages(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}] if content else []
    tool_msgs: list[dict] = []
    parts: list[dict] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            if b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
        elif t == "image":
            p = _image_part(b)
            if p:
                parts.append(p)
        elif t == "tool_result":
            tool_msgs.append({"role": "tool", "tool_call_id": upstream_id(str(b.get("tool_use_id", ""))),
                              "content": _tool_result_text(b)})
    msgs = tool_msgs  # tool messages must directly follow the assistant tool_calls message
    if parts:
        if all(p["type"] == "text" for p in parts):
            msgs.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
        else:
            msgs.append({"role": "user", "content": parts})
    return msgs


def _arguments(inp: Any) -> str:
    if isinstance(inp, dict) and set(inp) == {"_raw"} and isinstance(inp["_raw"], str):
        return inp["_raw"]
    return json.dumps(inp if isinstance(inp, dict) else {}, ensure_ascii=False)


def _assistant_message(content: Any) -> dict | None:
    if isinstance(content, str):
        return {"role": "assistant", "content": content} if content else None
    texts: list[str] = []
    calls: list[dict] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            texts.append(b["text"])
        elif t == "tool_use":
            calls.append({"id": upstream_id(str(b.get("id", ""))) or anthropic_id(None), "type": "function",
                          "function": {"name": b.get("name", ""), "arguments": _arguments(b.get("input"))}})
        # thinking / redacted_thinking / unknown: dropped
    if not texts and not calls:
        return None
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
    if calls:
        msg["tool_calls"] = calls
    return msg


def _tool_choice(tc: Any) -> Any:
    kind = tc.get("type") if isinstance(tc, dict) else None
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None


def _remove_schema_refs(schema: Any, depth: int = 10) -> Any:
    """Drop ``$id`` and ``$schema`` at every depth (port of litellm/utils.py ``_remove_json_schema_refs``,
    which Mistral needs: its grammar validation fails on them). Works on a copy."""
    if depth <= 0:
        return schema
    if isinstance(schema, dict):
        return {k: _remove_schema_refs(v, depth - 1) for k, v in schema.items() if k not in ("$id", "$schema")}
    if isinstance(schema, list):
        return [_remove_schema_refs(v, depth - 1) for v in schema]
    return schema


def to_openai(body: dict, upstream_model: str, *, max_tokens_field: str = "max_tokens",
              stream_usage: bool = True, mistral: bool = False, thinking: str | None = None) -> dict:
    """Anthropic Messages request -> chat completions request body.

    ``thinking`` (per-model, from the models table): None = send nothing; "on" / "off" = NVIDIA's documented
    ``chat_template_kwargs: {"enable_thinking": true|false}`` body field; "off-prompt" = a "/no_think" line at the
    end of the system message instead. Note: a model that keeps thinking on may spend the whole max_tokens on
    reasoning and answer with an empty text; the client still gets the thinking block, nothing is added.
    """
    messages: list[dict] = []
    system = _text_of(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            a = _assistant_message(m.get("content"))
            if a:
                messages.append(a)
        else:
            messages += _user_messages(m.get("content"))
    if thinking == "off-prompt":
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] += "\n/no_think"
        else:
            messages.insert(0, {"role": "system", "content": "/no_think"})
    out: dict[str, Any] = {"model": upstream_model, "messages": messages}
    if thinking in ("on", "off"):
        out["chat_template_kwargs"] = {"enable_thinking": thinking == "on"}
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or not isinstance(tool.get("input_schema"), dict):
            continue  # Anthropic server tools (web_search ...) have no chat completions equivalent
        params = {k: v for k, v in tool["input_schema"].items() if k != "$schema"}
        if mistral:
            params = _remove_schema_refs(params)
        params.setdefault("type", "object")
        tools.append({"type": "function", "function": {
            "name": tool.get("name", ""), "description": tool.get("description") or "", "parameters": params}})
    if tools:
        out["tools"] = tools
        choice = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            if mistral and choice not in ("auto", "none"):
                choice = "any"  # Mistral has no "required" and no tool_choice object (LiteLLM _map_tool_choice)
            out["tool_choice"] = choice
    if body.get("max_tokens") is not None:
        out[max_tokens_field] = body["max_tokens"]
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            out[key] = body[key]
    stops = body.get("stop_sequences")
    if isinstance(stops, list) and stops:
        out["stop"] = [s for s in stops if isinstance(s, str)][:4]  # OpenAI allows at most 4
    if body.get("stream"):
        out["stream"] = True
        if stream_usage:
            out["stream_options"] = {"include_usage": True}
    return out


# ---- response -----------------------------------------------------------------------------------

def parse_arguments(raw: Any) -> dict:
    """Tool call arguments -> dict; anything unparseable is kept as {"_raw": text}."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return {"_raw": raw}
    return v if isinstance(v, dict) else {"_raw": raw}


def _usage(u: Any) -> dict[str, int]:
    u = u if isinstance(u, dict) else {}
    prompt = int(u.get("prompt_tokens") or 0)
    det = u.get("prompt_tokens_details")
    cached = int((det.get("cached_tokens") if isinstance(det, dict) else 0) or u.get("prompt_cache_hit_tokens") or 0)
    out = {"input_tokens": max(prompt - cached, 0), "output_tokens": int(u.get("completion_tokens") or 0)}
    if cached:
        out["cache_read_input_tokens"] = cached
    return out


def _stop(finish: str | None, has_tool: bool, text: str, stops: tuple[str, ...]) -> tuple[str, str | None]:
    if has_tool and finish in (None, "stop"):
        return "tool_use", None
    reason = _FINISH.get(finish or "", "end_turn")
    if reason == "end_turn":
        for s in stops:
            if s and text.endswith(s):
                return "stop_sequence", s
    return reason, None


def _split_think(text: str) -> tuple[str, str]:
    s = text.lstrip()
    if not s.startswith(_THINK_OPEN):
        return "", text
    end = s.find(_THINK_CLOSE)
    if end < 0:
        return s[len(_THINK_OPEN):].strip("\n"), ""
    return s[len(_THINK_OPEN):end].strip("\n"), s[end + len(_THINK_CLOSE):].lstrip("\n")


def raise_if_error(obj: Any) -> None:
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        if isinstance(err, dict):
            raise OpenAIStreamError(str(err.get("message") or "upstream error"), err.get("code"),
                                    str(err.get("type") or ""))
        raise OpenAIStreamError(str(err))


def from_openai(resp: dict, model: str, stop_sequences: list[str] | tuple[str, ...] | None = None) -> dict:
    """Non-stream chat completion -> Anthropic message. Raises OpenAIStreamError on an ``error`` object."""
    raise_if_error(resp)
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    text = msg.get("content") if isinstance(msg.get("content"), str) else ""
    thinking = msg.get("reasoning_content") or msg.get("reasoning")
    if not isinstance(thinking, str) or not thinking:
        thinking, text = _split_think(text)
    if thinking:
        content.append({"type": "thinking", "thinking": thinking, "signature": ""})
    if text:
        content.append({"type": "text", "text": text})
    calls = msg.get("tool_calls") or []
    for c in calls:
        fn = (c.get("function") or {}) if isinstance(c, dict) else {}
        content.append({"type": "tool_use", "id": anthropic_id(c.get("id")), "name": fn.get("name", ""),
                        "input": parse_arguments(fn.get("arguments"))})
    if not content:
        content.append({"type": "text", "text": ""})  # Claude Code indexes content[0]
    stop, seq = _stop(choice.get("finish_reason"), bool(calls), text, tuple(stop_sequences or ()))
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": seq, "usage": _usage(resp.get("usage"))}


# ---- streaming ----------------------------------------------------------------------------------

def _hold_len(buf: str, tag: str) -> int:
    """Length of the longest proper suffix of ``buf`` that is a prefix of ``tag``."""
    for k in range(min(len(tag) - 1, len(buf)), 0, -1):
        if buf.endswith(tag[:k]):
            return k
    return 0


class StreamTranslator:
    """Chat completion chunks (as dicts) -> Anthropic SSE events [(event_name, data), ...].

    ``feed(chunk)`` per chunk (raises OpenAIStreamError on an ``error`` object), ``finish()`` once at
    ``[DONE]`` / end of stream (idempotent). Usage normally arrives in the last chunk, after finish_reason.
    """

    def __init__(self, model: str, stop_sequences: list[str] | tuple[str, ...] | None = None) -> None:
        self.model = model
        self.stop_sequences = tuple(stop_sequences or ())
        self._started = False
        self._done = False
        self._open: str | None = None  # "text" | "thinking" | "tool" | None
        self._open_call: int | None = None
        self._index = -1
        self._calls: dict[int, dict[str, Any]] = {}
        self._last_call_id = ""
        self._finish: str | None = None
        self._usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._cmode = "head"  # content parser: head (undecided) | think | plain
        self._hold = ""
        self._strip_lead = False
        self._tail = ""
        self._emitted = False

    @property
    def complete(self) -> bool:
        """True once a finish_reason was seen (a stream that ends without one was cut off)."""
        return self._finish is not None

    # -- block helpers --
    def _start(self) -> list[tuple[str, dict]]:
        if self._started:
            return []
        self._started = True
        msg = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": self.model,
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 0, "output_tokens": 1}}
        return [("message_start", {"type": "message_start", "message": msg})]

    def _close(self) -> list[tuple[str, dict]]:
        if self._open is None:
            return []
        if self._open == "tool" and self._open_call is not None:
            self._calls[self._open_call]["closed"] = True
        self._open, self._open_call = None, None
        return [("content_block_stop", {"type": "content_block_stop", "index": self._index})]

    def _begin(self, kind: str, block: dict) -> list[tuple[str, dict]]:
        ev = self._close()
        self._index += 1
        self._open = kind
        self._emitted = True
        ev.append(("content_block_start", {"type": "content_block_start", "index": self._index,
                                           "content_block": block}))
        return ev

    def _delta(self, delta: dict) -> tuple[str, dict]:
        return ("content_block_delta", {"type": "content_block_delta", "index": self._index, "delta": delta})

    def _emit(self, kind: str, text: str) -> list[tuple[str, dict]]:
        if not text:
            return []
        ev: list[tuple[str, dict]] = []
        if self._open != kind:
            ev += self._begin(kind, {"type": "text", "text": ""} if kind == "text"
                              else {"type": "thinking", "thinking": "", "signature": ""})
        if kind == "text":
            self._tail = (self._tail + text)[-max([len(s) for s in self.stop_sequences] + [1]):]
            ev.append(self._delta({"type": "text_delta", "text": text}))
        else:
            ev.append(self._delta({"type": "thinking_delta", "thinking": text}))
        return ev

    # -- content (text with optional leading <think> block) --
    def _content(self, text: str) -> list[tuple[str, dict]]:
        ev: list[tuple[str, dict]] = []
        buf, self._hold = self._hold + text, ""
        while buf:
            if self._cmode == "head":
                s = buf.lstrip()
                if not s:
                    self._hold = buf
                    break
                if s.startswith(_THINK_OPEN):
                    buf, self._cmode = s[len(_THINK_OPEN):], "think"
                    continue
                if _THINK_OPEN.startswith(s):
                    self._hold = buf
                    break
                self._cmode = "plain"
                continue
            if self._cmode == "think":
                i = buf.find(_THINK_CLOSE)
                if i >= 0:
                    ev += self._emit("thinking", buf[:i])
                    buf, self._cmode, self._strip_lead = buf[i + len(_THINK_CLOSE):], "plain", True
                    continue
                k = _hold_len(buf, _THINK_CLOSE)
                ev += self._emit("thinking", buf[:len(buf) - k])
                self._hold = buf[len(buf) - k:]
                break
            if self._strip_lead:
                buf = buf.lstrip("\n")
                if not buf:
                    break
                self._strip_lead = False
            ev += self._emit("text", buf)
            break
        return ev

    def _tool_delta(self, tc: dict) -> list[tuple[str, dict]]:
        idx = tc.get("index")
        if not isinstance(idx, int):  # some providers omit the index: a new id means a new call
            tid = tc.get("id") or ""
            if self._calls and not (tid and tid != self._last_call_id):
                idx = max(self._calls)
            else:
                idx = len(self._calls)
        call = self._calls.setdefault(idx, {"id": "", "name": "", "pending": "", "block": None, "closed": False})
        fn = tc.get("function") or {}
        if tc.get("id") and not call["id"]:
            call["id"] = str(tc["id"])
            self._last_call_id = call["id"]
        if fn.get("name") and not call["name"]:
            call["name"] = str(fn["name"])
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args)
        if isinstance(args, str) and args:
            call["pending"] += args
        ev: list[tuple[str, dict]] = []
        if call["block"] is None and call["name"] and not call["closed"]:
            ev += self._begin("tool", {"type": "tool_use", "id": anthropic_id(call["id"]),
                                       "name": call["name"], "input": {}})
            call["block"], self._open_call = self._index, idx
        if call["block"] is not None and not call["closed"] and call["pending"]:
            ev.append(self._delta({"type": "input_json_delta", "partial_json": call["pending"]}))
            call["pending"] = ""
        return ev

    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        raise_if_error(chunk)
        ev = self._start()
        u = chunk.get("usage")
        if isinstance(u, dict) and (u.get("prompt_tokens") or u.get("completion_tokens")):
            self._usage = _usage(u)
        choices = chunk.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            ev += self._emit("thinking", reasoning)
        content = delta.get("content")
        if isinstance(content, str) and content:
            ev += self._content(content)
        for tc in delta.get("tool_calls") or []:
            if isinstance(tc, dict):
                ev += self._tool_delta(tc)
        if choice.get("finish_reason"):
            self._finish = choice["finish_reason"]
        return ev

    def finish(self) -> list[tuple[str, dict]]:
        """Flush held text, close the open block, emit message_delta + message_stop (idempotent)."""
        if self._done:
            return []
        self._done = True
        ev = self._start()
        if self._hold:
            held, self._hold = self._hold, ""
            if self._cmode == "think":
                ev += self._emit("thinking", held)
            elif held.strip():
                ev += self._emit("text", held)
        if not self._emitted:
            ev += self._begin("text", {"type": "text", "text": ""})
        ev += self._close()
        has_tool = any(c["block"] is not None for c in self._calls.values())
        stop, seq = _stop(self._finish, has_tool, self._tail, self.stop_sequences)
        ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": seq},
                                     "usage": dict(self._usage)}))
        ev.append(("message_stop", {"type": "message_stop"}))
        return ev
