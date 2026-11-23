# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: OpenCode Big Pickle
"""Gemini API FRONT of kiko-lite: ``/v1beta/models/{model}:generateContent`` and ``:streamGenerateContent``.

This is the wire the Gemini CLI (the ``@google/genai`` SDK) speaks when ``GOOGLE_GEMINI_BASE_URL`` is set.
The Gemini body is converted to the Anthropic Messages body, the usual kiko pipeline (resolver, failover,
cooldowns, billing, text log) runs unchanged, and the result is converted back to a Gemini response.

* Request: ``contents`` (user / model turns), ``systemInstruction``, ``tools.functionDeclarations``,
  ``generationConfig`` (temperature, topP, maxOutputTokens, stopSequences) and
  ``toolConfig.functionCallingConfig.mode`` are mapped. Gemini-only knobs (topK, thinkingConfig,
  safetySettings, responseMimeType, candidateCount > 1) are ignored or rejected — never forwarded.
* Function calls keep the thought-signature id suffix of ``providers.gemini`` so a tool round trip survives.
* Non-stream: one ``candidates[0].content.parts`` message, ``finishReason`` and ``usageMetadata``.
* Stream: every frame is ``data: {json}`` with ``candidates[0].content.parts`` deltas; the last frame carries
  ``finishReason`` and ``usageMetadata``. No ``[DONE]``: the Gemini SDK stops when the stream closes.
  Keepalives are SSE comment lines. An error is a Gemini ``error`` object.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .providers import gemini as gp

_FINISH = {
    "end_turn": "STOP", "stop_sequence": "STOP", "tool_use": "STOP", "refusal": "SAFETY",
    "max_tokens": "MAX_TOKENS",
}
_ERRORS = {
    "invalid_request_error": ("400", "INVALID_ARGUMENT"),
    "authentication_error": ("401", "UNAUTHENTICATED"),
    "permission_error": ("403", "PERMISSION_DENIED"),
    "not_found_error": ("404", "NOT_FOUND"),
    "request_too_large": ("400", "INVALID_ARGUMENT"),
    "rate_limit_error": ("429", "RESOURCE_EXHAUSTED"),
    "overloaded_error": ("503", "UNAVAILABLE"),
}


class GeminiRequestError(ValueError):
    """The Gemini request cannot be served (a 400 for the client)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def error_body(typ: str, message: str, param: str | None = None) -> dict:
    """Gemini-shaped error from an Anthropic error type."""
    code, status = _ERRORS.get(typ, ("500", "INTERNAL"))
    return {"error": {"code": code, "message": message, "status": status}}


def _text(node: Any, where: str) -> str:
    """Text of a Gemini ``systemInstruction`` (parts or a bare text field)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        raise GeminiRequestError(f"{where}: must be an object")
    if isinstance(node.get("text"), str):
        return node["text"]
    parts = node.get("parts")
    if not isinstance(parts, list):
        return ""
    out = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "\n".join(out)


def _json_schema(node: Any, depth: int = 0) -> dict:
    """Gemini Schema (uppercase types) -> JSON Schema for the Anthropic ``input_schema``."""
    if not isinstance(node, dict) or depth > 32:
        return {"type": "object", "properties": {}}
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "type" and isinstance(v, str):
            out["type"] = v.lower()
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {str(n): _json_schema(s, depth + 1) for n, s in v.items()}
        elif k == "items" and isinstance(v, dict):
            out["items"] = _json_schema(v, depth + 1)
        elif k in ("enum", "required", "description", "minimum", "maximum", "minLength", "maxLength",
                   "pattern", "title", "format"):
            out[k] = v
    if "type" not in out and "anyOf" not in out:
        out["type"] = "object"
    return out


def _tools(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GeminiRequestError("tools: must be a list")
    out: list[dict] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        decls = t.get("functionDeclarations")
        if not isinstance(decls, list):
            continue
        for d in decls:
            if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"]:
                raise GeminiRequestError("tools.functionDeclarations.name: required")
            tool = {"name": d["name"], "input_schema": _json_schema(d.get("parameters"))}
            if isinstance(d.get("description"), str) and d["description"]:
                tool["description"] = d["description"]
            out.append(tool)
    return out


def _part_blocks(parts: Any, role: str, ids: dict[str, str], where: str) -> list[dict]:
    if parts is None:
        return []
    if not isinstance(parts, list):
        raise GeminiRequestError(f"{where}.parts: must be a list")
    blocks: list[dict] = []
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            raise GeminiRequestError(f"{where}.parts.{i}: must be an object")
        if isinstance(p.get("text"), str) and not p.get("thought"):
            if p["text"]:
                blocks.append({"type": "text", "text": p["text"]})
            continue
        if "inlineData" in p:
            data = p["inlineData"]
            if isinstance(data, dict) and isinstance(data.get("data"), str) and data["data"]:
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": data.get("mimeType") or "image/png", "data": data["data"]}})
            continue
        if "functionCall" in p:
            fc = p["functionCall"]
            if not isinstance(fc, dict) or not isinstance(fc.get("name"), str):
                raise GeminiRequestError(f"{where}.parts.{i}.functionCall: invalid")
            tid = gp._new_id(p.get("thoughtSignature"))
            ids.setdefault(fc["name"], tid)
            blocks.append({"type": "tool_use", "id": tid, "name": fc["name"],
                           "input": fc.get("args") if isinstance(fc.get("args"), dict) else {}})
            continue
        if "functionResponse" in p:
            fr = p["functionResponse"]
            if not isinstance(fr, dict):
                raise GeminiRequestError(f"{where}.parts.{i}.functionResponse: invalid")
            name = str(fr.get("name") or "")
            tid = ids.get(name) or gp._new_id(None)
            resp = fr.get("response")
            text = json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) else resp
            blocks.append({"type": "tool_result", "tool_use_id": tid, "content": text})
            continue
        # thought parts, fileData, executableCode ...: nothing we can carry to the upstream
    return blocks


def convert_request(body: Any, model: str, stream: bool) -> tuple[dict, dict]:
    """Gemini request -> (Anthropic Messages body, opts). ``model`` comes from the URL, not the body."""
    if not isinstance(body, dict):
        raise GeminiRequestError("request body must be an object")
    contents = body.get("contents")
    if not isinstance(contents, list):
        raise GeminiRequestError("contents: Field required")
    cfg = body.get("generationConfig") or {}
    if not isinstance(cfg, dict):
        raise GeminiRequestError("generationConfig: must be an object")
    n = cfg.get("candidateCount")
    if n is not None and n not in (None, 1):
        raise GeminiRequestError("candidateCount > 1 is not supported")

    ids: dict[str, str] = {}
    messages: list[dict] = []
    role: str | None = None
    pending: list[dict] = []

    def flush() -> None:
        nonlocal role, pending
        if role is not None and pending:
            messages.append({"role": role, "content": pending})
        role, pending = None, []

    for i, item in enumerate(contents):
        if not isinstance(item, dict):
            raise GeminiRequestError(f"contents.{i}: must be an object")
        r = "assistant" if item.get("role") == "model" else "user"
        blocks = _part_blocks(item.get("parts"), r, ids, f"contents.{i}")
        if r != role:
            flush()
            role = r
        pending.extend(blocks)
    flush()
    if not messages:
        raise GeminiRequestError("contents: at least one text part is required")

    a: dict[str, Any] = {"model": model, "messages": messages}
    system = _text(body.get("systemInstruction"), "systemInstruction")
    if system:
        a["system"] = system
    if cfg.get("maxOutputTokens") is not None:
        a["max_tokens"] = cfg["maxOutputTokens"]
    for source, target in (("temperature", "temperature"), ("topP", "top_p")):
        if cfg.get(source) is not None:
            a[target] = cfg[source]
    if cfg.get("stopSequences") is not None:
        a["stop_sequences"] = cfg["stopSequences"]
    tools = _tools(body.get("tools"))
    cfg_tool = body.get("toolConfig") or {}
    mode = ((cfg_tool.get("functionCallingConfig") or {}).get("mode") if isinstance(cfg_tool, dict) else None)
    if mode != "NONE" and tools:
        a["tools"] = tools
        if mode == "ANY":
            a["tool_choice"] = {"type": "any"}
    if stream:
        a["stream"] = True
    return a, {"stream": stream, "model": model}


# ---- non-stream response ------------------------------------------------------------------------

def _finish(stop: Any) -> str:
    return _FINISH.get(stop, "STOP")


def _usage(u: Any) -> dict[str, int]:
    u = u if isinstance(u, dict) else {}
    return {"input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0)}


def _candidate(parts: list[dict], finish: str, usage: dict | None = None) -> dict:
    c: dict[str, Any] = {"content": {"parts": parts, "role": "model"}, "finishReason": finish, "index": 0}
    out = {"candidates": [c], "modelVersion": ""}
    if usage is not None:
        out["usageMetadata"] = {"promptTokenCount": usage["input_tokens"],
                                "candidatesTokenCount": usage["output_tokens"],
                                "totalTokenCount": usage["input_tokens"] + usage["output_tokens"]}
    return out


def convert_response(message: dict, model: str) -> dict:
    """Anthropic message -> Gemini ``GenerateContentResponse``."""
    parts: list[dict] = []
    for b in message.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            parts.append({"text": b["text"]})
        elif b.get("type") == "tool_use":
            tid, sig = gp._split_id(str(b.get("id") or ""))
            p: dict[str, Any] = {"functionCall": {"name": b.get("name", ""),
                                                  "args": b.get("input") if isinstance(b.get("input"), dict) else {}}}
            if sig:
                p["thoughtSignature"] = sig
            parts.append(p)
    out = _candidate(parts, _finish(message.get("stop_reason")), _usage(message.get("usage")))
    out["modelVersion"] = model
    return out


# ---- stream -------------------------------------------------------------------------------------

class GeminiStream:
    """Anthropic stream events -> Gemini SSE frames (one instance per response). Same interface as ChatStream."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._blocks: dict[int, dict] = {}
        self._finish: str | None = None
        self._usage: dict | None = None

    @staticmethod
    def ping() -> bytes:
        # The @google/genai SDK keeps every unmatched byte in a buffer and throws
        # "Incomplete JSON segment at the end" at EOF if an SSE comment is left there.
        return b""

    @staticmethod
    def _frame(obj: Any) -> bytes:
        return b"data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n\n"

    def _out(self, parts: list[dict], finish: str | None = None, usage: dict | None = None) -> bytes:
        c: dict[str, Any] = {"content": {"parts": parts, "role": "model"}, "index": 0}
        if finish is not None:
            c["finishReason"] = finish
        return self._frame(_candidate([], finish or "STOP", usage) | {"candidates": [c],
                                                                      "modelVersion": self.model})

    def error(self, typ: str, message: str) -> bytes:
        return self._frame(error_body(typ, message))

    def event(self, name: str, ev: dict) -> list[bytes]:
        if name == "content_block_start":
            blk = ev.get("content_block") or {}
            idx = ev.get("index", len(self._blocks))
            if blk.get("type") == "text":
                self._blocks[idx] = {"kind": "text"}
            elif blk.get("type") == "tool_use":
                self._blocks[idx] = {"kind": "tool", "name": blk.get("name", ""), "args": []}
            return []
        if name == "content_block_delta":
            st = self._blocks.get(ev.get("index"))
            d = ev.get("delta") or {}
            if st is not None and st["kind"] == "text" and d.get("type") == "text_delta" and d.get("text"):
                return [self._out([{"text": str(d["text"])}])]
            if st is not None and st["kind"] == "tool" and d.get("type") == "input_json_delta":
                st["args"].append(str(d.get("partial_json") or ""))
            return []
        if name == "content_block_stop":
            st = self._blocks.pop(ev.get("index"), None)
            if st is not None and st["kind"] == "tool":
                args = "".join(st["args"]) or "{}"
                try:
                    parsed = json.loads(args)
                except ValueError:
                    parsed = {}
                return [self._out([{"functionCall": {"name": st["name"], "args": parsed}}])]
            return []
        if name == "message_delta":
            self._finish = _finish((ev.get("delta") or {}).get("stop_reason"))
            self._usage = _usage(ev.get("usage"))
        return []

    def close(self) -> list[bytes]:
        """A final frame with finishReason + usage; the Gemini SDK stops when the stream ends."""
        return [self._out([], self._finish or "STOP", self._usage)]
