# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Gemini (AI Studio, native generateContent) <-> Anthropic Messages translation.

Pure functions, no network. Choices worth knowing:

* Thought signatures: Gemini returns `thoughtSignature` on functionCall parts and wants it echoed on
  the same part next turn. LiteLLM suffixes the tool call id only for OpenAI-format clients; on its
  Anthropic path it uses provider_specific_fields. Claude Code has no provider_specific_fields but does
  echo tool_use `id` verbatim, so we keep the id-suffix method:
  ``toolu_<hex>__thought__<urlsafe-b64-signature>``. On the way back the suffix is split off and
  restored on the functionCall part. Missing on a Gemini 3+ functionCall ->
  dummy base64("skip_thought_signature_validator") on the first call of the turn.
* Thinking: Anthropic `thinking {enabled, budget_tokens}` -> thinkingConfig {includeThoughts,
  thinkingBudget} for every model (Gemini 3 still accepts thinkingBudget). Other types (e.g.
  "adaptive") -> includeThoughts without a budget; "disabled"/absent -> nothing. Assistant `thinking`
  blocks from history are dropped (signatures travel via tool_use ids).
* tool_choice.disable_parallel_tool_use has no Gemini equivalent and is ignored.
* Images inside a tool_result become inlineData parts placed right after that functionResponse.
* Schema format: only "enum" and "date-time" survive (as in LiteLLM filter_schema_fields); empty-string
  enum values become null (LiteLLM _fix_enum_empty_strings).
* Safety finish reasons map to stop_reason `refusal`; other unknown reasons to `end_turn`.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any
from urllib.parse import quote

BASE = "https://generativelanguage.googleapis.com"
SIG_SEP = "__thought__"
DUMMY_SIGNATURE = base64.b64encode(b"skip_thought_signature_validator").decode()

_REFUSAL = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
_PLAIN_KEYS = ("description", "minItems", "maxItems", "minimum", "maximum", "minLength",
               "maxLength", "pattern", "title", "propertyOrdering")


class GeminiStreamError(Exception):
    """An `error` object arrived inside an otherwise successful (HTTP 200) stream."""

    def __init__(self, message: str, code: Any = None, status: str = "") -> None:
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _major(model: str) -> int:
    m = re.search(r"gemini-(\d+)", model or "")
    return int(m.group(1)) if m else 0


def endpoint(model: str, stream: bool) -> str:
    """Full URL for a model; the API key never goes in the URL."""
    ver = "v1alpha" if _major(model) >= 3 else "v1beta"
    model = quote(model, safe="")  # one path segment: '/', '?', '#', '%' cannot change the endpoint
    if stream:
        return f"{BASE}/{ver}/models/{model}:streamGenerateContent?alt=sse"
    return f"{BASE}/{ver}/models/{model}:generateContent"


def headers(api_key: str) -> dict[str, str]:
    return {"x-goog-api-key": api_key, "content-type": "application/json"}


# ---- thought signatures in tool_use ids ---------------------------------------------------------

def _new_id(sig: str | None) -> str:
    tid = "toolu_" + uuid.uuid4().hex[:24]
    if sig:
        tid += SIG_SEP + sig.replace("+", "-").replace("/", "_").rstrip("=")
    return tid


def _split_id(tid: str) -> tuple[str, str | None]:
    base, sep, enc = tid.partition(SIG_SEP)
    if not sep or not enc:
        return tid, None
    sig = enc.replace("-", "+").replace("_", "/")
    return base, sig + "=" * (-len(sig) % 4)


# ---- schema -------------------------------------------------------------------------------------

def sanitize_schema(schema: dict) -> dict:
    """JSON Schema -> Gemini Schema subset (inline refs, anyOf for type arrays, drop unknown keys)."""
    root = schema if isinstance(schema, dict) else {}
    return _typed(_clean(root, root, (), [0]))


def _typed(out: dict) -> dict:
    """Gemini needs a type on every schema (LiteLLM add_object_type): default to object."""
    if "type" not in out and "anyOf" not in out:
        out["type"] = "object"
    return out


def _resolve(ref: str, root: dict) -> Any:
    node: Any = root
    if not ref.startswith("#/"):
        return None
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


_MAX_DEPTH = 32
_MAX_NODES = 5000


def _clean(node: Any, root: dict, stack: tuple[str, ...], budget: list[int], depth: int = 0) -> dict:
    budget[0] += 1
    if depth > _MAX_DEPTH or budget[0] > _MAX_NODES:
        return {"type": "object"}  # runaway nesting / ref fan-out: stop expanding
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _resolve(ref, root)
        if ref in stack or not isinstance(target, dict):
            return {"type": "object"}  # cycle or dangling ref: give up on this branch
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _clean(merged, root, stack + (ref,), budget, depth + 1)
    node = dict(node)
    for sub in node.pop("allOf", None) or []:
        sub = _clean(sub, root, stack, budget, depth + 1)
        for k, v in sub.items():
            if k == "properties":
                node["properties"] = {**node.get("properties", {}), **v}
            elif k == "required":
                node["required"] = list(dict.fromkeys([*node.get("required", []), *v]))
            else:
                node.setdefault(k, v)
    if "oneOf" in node:
        node["anyOf"] = [*(node.get("anyOf") or []), *node.pop("oneOf")]
    if "const" in node and "enum" not in node:
        node["enum"] = [node["const"]]
    out: dict[str, Any] = {}
    t = node.get("type")
    nullable = bool(node.get("nullable"))
    any_of = node.get("anyOf")
    if isinstance(t, list):
        kinds = [k for k in t if k != "null"]
        nullable = nullable or len(kinds) != len(t)
        if len(kinds) > 1:
            any_of = [*(any_of or []), *({"type": k} for k in kinds)]
        t = kinds[0] if len(kinds) == 1 else None
    if isinstance(any_of, list):
        subs = []
        for sub in any_of:
            c = _clean(sub, root, stack, budget, depth + 1)
            if c == {} and isinstance(sub, dict) and sub.get("type") == "null":
                nullable = True
            elif c:
                subs.append(c)
        if len(subs) == 1 and t is None:
            out.update(subs[0])
        elif subs:
            out["anyOf"] = subs
    if isinstance(t, str) and t != "null":
        out["type"] = t
    for key in _PLAIN_KEYS:
        if key in node:
            out.setdefault(key, node[key])
    if node.get("format") in ("enum", "date-time"):
        out["format"] = node["format"]
    enum = node.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(e, str) for e in enum) \
            and out.get("type", "string") == "string":
        out["enum"] = [e or None for e in enum]
        out.setdefault("type", "string")
    props = node.get("properties")
    if isinstance(props, dict):
        out["type"] = "object"
        if props:  # Gemini rejects empty properties
            out["properties"] = {k: _typed(_clean(v, root, stack, budget, depth + 1))
                                 for k, v in props.items()}
            req = [r for r in node.get("required") or [] if r in out["properties"]]
            if req:
                out["required"] = req
    if out.get("type") == "array" or "items" in node:
        items = node.get("items")
        out["items"] = _typed(_clean(items, root, stack, budget, depth + 1)) \
            if isinstance(items, dict) and items else {"type": "string"}
        out.setdefault("type", "array")
    if nullable:
        out["nullable"] = True
    return out


# ---- request ------------------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _parts(content: Any, names: dict[str, str], dummy_sig: bool) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict] = []
    first_call = True
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            if b.get("text"):
                parts.append({"text": b["text"]})
        elif t == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64":
                parts.append({"inlineData": {"mimeType": src.get("media_type", "image/png"),
                                             "data": src.get("data", "")}})
        elif t == "tool_use":
            tid = b.get("id", "")
            tid = tid if isinstance(tid, str) else ""
            _, sig = _split_id(tid)
            names[tid] = b.get("name", "")
            part: dict[str, Any] = {"functionCall": {"name": b.get("name", ""), "args": b.get("input") or {}}}
            if sig is None and dummy_sig and first_call:
                sig = DUMMY_SIGNATURE
            if sig:
                part["thoughtSignature"] = sig
            first_call = False
            parts.append(part)
        elif t == "tool_result":
            resp: dict[str, Any] = {"content": _text_of(b.get("content"))}
            if b.get("is_error"):
                resp["is_error"] = True
            name = names.get(b.get("tool_use_id", ""), "unknown_tool")
            parts.append({"functionResponse": {"name": name, "response": resp}})
            if isinstance(b.get("content"), list):
                parts += _parts([c for c in b["content"] if isinstance(c, dict) and c.get("type") == "image"],
                                names, False)
        # thinking / redacted_thinking / documents / unknown: dropped
    return parts


def to_gemini(body: dict) -> dict:
    """Anthropic Messages request -> Gemini generateContent request body."""
    model = str(body.get("model", ""))
    names: dict[str, str] = {}
    out: dict[str, Any] = {}
    system = _text_of(body.get("system"))
    if system:
        out["systemInstruction"] = {"parts": [{"text": system}]}
    contents = []
    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        parts = _parts(m.get("content"), names, role == "model" and _major(model) >= 3)
        if parts:
            contents.append({"role": role, "parts": parts})
    out["contents"] = contents
    decls = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or "input_schema" not in tool:
            continue  # Anthropic server tools have no Gemini equivalent
        d: dict[str, Any] = {"name": tool.get("name", ""),
                             "description": tool.get("description") or tool.get("name", "")}
        params = sanitize_schema(tool["input_schema"])
        if params.get("properties"):
            d["parameters"] = params
        decls.append(d)
    if decls:
        out["tools"] = [{"functionDeclarations": decls}]
        tc = body.get("tool_choice")
        kind = tc.get("type") if isinstance(tc, dict) else None
        cfg: dict[str, Any] | None = None
        if kind == "auto":
            cfg = {"mode": "AUTO"}
        elif kind == "any":
            cfg = {"mode": "ANY"}
        elif kind == "tool" and tc.get("name"):
            cfg = {"mode": "ANY", "allowedFunctionNames": [tc["name"]]}
        elif kind == "none":
            cfg = {"mode": "NONE"}
        if cfg:
            out["toolConfig"] = {"functionCallingConfig": cfg}
    gen: dict[str, Any] = {}
    for src, dst in (("max_tokens", "maxOutputTokens"), ("temperature", "temperature"),
                     ("top_p", "topP"), ("top_k", "topK")):
        if body.get(src) is not None:
            gen[dst] = body[src]
    if body.get("stop_sequences"):
        stops = body["stop_sequences"]
        gen["stopSequences"] = [stops] if isinstance(stops, str) else [x for x in stops if isinstance(x, str)]
    th = body.get("thinking")
    if isinstance(th, dict) and th.get("type") not in (None, "disabled"):
        cfg_t: dict[str, Any] = {"includeThoughts": True}
        if th.get("type") == "enabled" and th.get("budget_tokens") is not None:
            cfg_t["thinkingBudget"] = th["budget_tokens"]
        gen["thinkingConfig"] = cfg_t
    if gen:
        out["generationConfig"] = gen
    return out


# ---- response -----------------------------------------------------------------------------------

def _stop_reason(finish: str | None, has_tool: bool) -> str:
    if finish == "MAX_TOKENS":
        return "max_tokens"
    if finish in _REFUSAL:
        return "refusal"
    return "tool_use" if has_tool else "end_turn"


def _usage(meta: dict | None) -> dict[str, int]:
    meta = meta or {}
    cached = int(meta.get("cachedContentTokenCount") or 0)
    prompt = int(meta.get("promptTokenCount") or 0)
    out = int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
    u = {"input_tokens": max(prompt - cached, 0), "output_tokens": out}
    if cached:
        u["cache_read_input_tokens"] = cached
    return u


def _block_reason(resp: dict) -> str | None:
    return (resp.get("promptFeedback") or {}).get("blockReason")


def from_gemini(resp: dict, model: str) -> dict:
    """Non-stream Gemini response -> Anthropic message."""
    content: list[dict] = []
    cand = (resp.get("candidates") or [{}])[0]
    has_tool = False
    for p in (cand.get("content") or {}).get("parts") or []:
        if "functionCall" in p:
            fc = p["functionCall"]
            has_tool = True
            content.append({"type": "tool_use", "id": _new_id(p.get("thoughtSignature")),
                            "name": fc.get("name", ""), "input": fc.get("args") or {}})
        elif p.get("thought"):
            content.append({"type": "thinking", "thinking": p.get("text", ""),
                            "signature": p.get("thoughtSignature", "")})
        elif p.get("text"):
            content.append({"type": "text", "text": p["text"]})
    stop = _stop_reason(cand.get("finishReason"), has_tool)
    reason = _block_reason(resp)
    if reason:
        stop = "refusal"
        content = [{"type": "text", "text": f"[Request blocked by Gemini: {reason}]"}]
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": _usage(resp.get("usageMetadata"))}


# ---- streaming ----------------------------------------------------------------------------------

class StreamTranslator:
    """Gemini SSE chunks (as dicts) -> Anthropic SSE events [(event_name, data), ...]."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._started = False
        self._done = False
        self._open: str | None = None  # "text" | "thinking" | None
        self._index = -1
        self._has_tool = False
        self._finish: str | None = None
        self._blocked: str | None = None
        self._usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    @property
    def complete(self) -> bool:
        """True once a finishReason (or a prompt block) was seen; Gemini streams end without a [DONE]."""
        return self._finish is not None or self._blocked is not None

    def _start(self) -> list[tuple[str, dict]]:
        if self._started:
            return []
        self._started = True
        msg = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": self.model,
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": self._usage["input_tokens"], "output_tokens": 1}}
        return [("message_start", {"type": "message_start", "message": msg})]

    def _close(self) -> list[tuple[str, dict]]:
        if self._open is None:
            return []
        self._open = None
        return [("content_block_stop", {"type": "content_block_stop", "index": self._index})]

    def _begin(self, kind: str) -> list[tuple[str, dict]]:
        ev = self._close()
        self._index += 1
        self._open = kind
        block = {"type": "text", "text": ""} if kind == "text" \
            else {"type": "thinking", "thinking": "", "signature": ""}
        ev.append(("content_block_start",
                   {"type": "content_block_start", "index": self._index, "content_block": block}))
        return ev

    def _delta(self, delta: dict) -> tuple[str, dict]:
        return ("content_block_delta", {"type": "content_block_delta", "index": self._index, "delta": delta})

    def _text(self, kind: str, text: str, sig: str | None) -> list[tuple[str, dict]]:
        ev: list[tuple[str, dict]] = []
        if self._open != kind:
            ev += self._begin(kind)
        if text:
            ev.append(self._delta({"type": "text_delta", "text": text} if kind == "text"
                                  else {"type": "thinking_delta", "thinking": text}))
        if sig and kind == "thinking":
            ev.append(self._delta({"type": "signature_delta", "signature": sig}))
        return ev

    def feed(self, event: dict) -> list[tuple[str, dict]]:
        """Translate one Gemini chunk. Raises GeminiStreamError on an in-stream `error` object."""
        if isinstance(event.get("error"), dict):
            err = event["error"]
            raise GeminiStreamError(str(err.get("message", "Gemini stream error")), err.get("code"),
                                    str(err.get("status", "")))
        if "usageMetadata" in event:
            self._usage = _usage(event["usageMetadata"])
        ev = self._start()
        reason = _block_reason(event)
        if reason:
            self._blocked = reason
            ev += self._text("text", f"[Request blocked by Gemini: {reason}]", None)
        cand = (event.get("candidates") or [{}])[0]
        for p in (cand.get("content") or {}).get("parts") or []:
            if "functionCall" in p:
                fc = p["functionCall"]
                ev += self._close()
                self._index += 1
                self._has_tool = True
                ev.append(("content_block_start", {
                    "type": "content_block_start", "index": self._index,
                    "content_block": {"type": "tool_use", "id": _new_id(p.get("thoughtSignature")),
                                      "name": fc.get("name", ""), "input": {}}}))
                ev.append(self._delta({"type": "input_json_delta",
                                       "partial_json": json.dumps(fc.get("args") or {})}))
                ev.append(("content_block_stop", {"type": "content_block_stop", "index": self._index}))
            elif p.get("thought"):
                if p.get("text") or p.get("thoughtSignature"):
                    ev += self._text("thinking", p.get("text", ""), p.get("thoughtSignature"))
            elif p.get("text"):
                ev += self._text("text", p["text"], None)
        if cand.get("finishReason"):
            self._finish = cand["finishReason"]
        return ev

    def finish(self) -> list[tuple[str, dict]]:
        """Close any open block and emit message_delta + message_stop (idempotent)."""
        if self._done:
            return []
        self._done = True
        ev = self._start() + self._close()
        stop = "refusal" if self._blocked else _stop_reason(self._finish, self._has_tool)
        ev.append(("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": dict(self._usage)}))
        ev.append(("message_stop", {"type": "message_stop"}))
        return ev
