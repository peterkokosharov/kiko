# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Gemini native translator (pure functions, hand-written fixtures)."""
import base64
import json

import pytest

from kiko_lite.providers import gemini as g


def test_endpoint_and_headers():
    assert g.endpoint("gemini-3.8-flash", False) == \
        "https://generativelanguage.googleapis.com/v1alpha/models/gemini-3.8-flash:generateContent"
    assert g.endpoint("gemini-2.5-flash", True) == \
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    h = g.headers("SECRET")
    assert h["x-goog-api-key"] == "SECRET"
    assert "SECRET" not in g.endpoint("gemini-3.5-flash-lite", True)


def test_basic_request_mapping():
    body = {
        "model": "gemini-3.8-flash", "max_tokens": 100, "temperature": 0.2, "top_p": 0.9, "top_k": 40,
        "stop_sequences": ["END"], "metadata": {"user_id": "x"},
        "system": [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "Be kind."}],
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
            {"role": "assistant", "content": "hello"},
        ],
    }
    out = g.to_gemini(body)
    assert out["systemInstruction"] == {"parts": [{"text": "Be brief.\nBe kind."}]}
    assert out["contents"][0] == {"role": "user", "parts": [
        {"text": "hi"}, {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]}
    assert out["contents"][1] == {"role": "model", "parts": [{"text": "hello"}]}
    assert out["generationConfig"] == {
        "maxOutputTokens": 100, "temperature": 0.2, "topP": 0.9, "topK": 40, "stopSequences": ["END"],
        "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 2048}}
    assert "metadata" not in out and "model" not in out


def test_system_string():
    out = g.to_gemini({"model": "m", "system": "sys", "messages": [{"role": "user", "content": "x"}]})
    assert out["systemInstruction"]["parts"] == [{"text": "sys"}]
    assert "generationConfig" not in out


@pytest.mark.parametrize("choice,expected", [
    ({"type": "auto"}, {"mode": "AUTO"}),
    ({"type": "any"}, {"mode": "ANY"}),
    ({"type": "tool", "name": "f"}, {"mode": "ANY", "allowedFunctionNames": ["f"]}),
    ({"type": "none"}, {"mode": "NONE"}),
])
def test_tool_choice(choice, expected):
    body = {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "x"}], "tool_choice": choice,
            "tools": [{"name": "f", "description": "d",
                       "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}}}]}
    out = g.to_gemini(body)
    assert out["toolConfig"] == {"functionCallingConfig": expected}
    decl = out["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "f" and decl["parameters"]["properties"]["a"] == {"type": "string"}


def test_tool_without_params_and_server_tool_skipped():
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": [
        {"name": "noargs", "input_schema": {"type": "object", "properties": {}}},
        {"type": "web_search_20250305", "name": "web_search"}]}
    decls = g.to_gemini(body)["tools"][0]["functionDeclarations"]
    assert decls == [{"name": "noargs", "description": "noargs"}]


# ---- schema ----

def test_schema_inlines_refs_and_drops_keys():
    s = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
         "$defs": {"Pt": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"],
                          "additionalProperties": False}},
         "properties": {"p": {"$ref": "#/$defs/Pt", "description": "a point"}, "q": {"type": "string", "strict": True}},
         "required": ["p", "ghost"]}
    out = g.sanitize_schema(s)
    assert out == {"type": "object", "required": ["p"], "properties": {
        "p": {"type": "object", "description": "a point", "properties": {"x": {"type": "integer"}},
              "required": ["x"]},
        "q": {"type": "string"}}}


def test_schema_cycle_guard():
    s = {"type": "object", "$defs": {"Node": {"type": "object", "properties": {
        "next": {"$ref": "#/$defs/Node"}, "v": {"type": "string"}}}},
        "properties": {"root": {"$ref": "#/$defs/Node"}}}
    out = g.sanitize_schema(s)
    assert out["properties"]["root"]["properties"]["next"] == {"type": "object"}


def test_schema_type_arrays_nullable_enum_items():
    s = {"type": "object", "properties": {
        "a": {"type": ["string", "null"]},
        "b": {"type": ["string", "integer"]},
        "c": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "d": {"type": "integer", "enum": [1, 2]},
        "e": {"type": "string", "enum": ["x", "y"]},
        "f": {"type": "array"},
        "g": {"type": "array", "items": {"type": "number"}, "minItems": 1},
        "h": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        "i": {"const": "k"},
    }}
    p = g.sanitize_schema(s)["properties"]
    assert p["a"] == {"type": "string", "nullable": True}
    assert p["b"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert p["c"] == {"type": "integer", "nullable": True}
    assert p["d"] == {"type": "integer"}
    assert p["e"] == {"type": "string", "enum": ["x", "y"]}
    assert p["f"] == {"type": "array", "items": {"type": "string"}}
    assert p["g"] == {"type": "array", "items": {"type": "number"}, "minItems": 1}
    assert p["h"] == {"anyOf": [{"type": "string"}, {"type": "number"}]}
    assert p["i"] == {"type": "string", "enum": ["k"]}


# ---- multi-turn tool round trip with signatures ----

SIG = base64.b64encode(b"\xfb\xff\xfesig/with+chars").decode()


def _tool_turn_response():
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "pondering", "thought": True},
        {"text": "Let me look.", "thoughtSignature": "ignored"},
        {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": SIG},
        {"functionCall": {"name": "ls", "args": {}}}]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30,
                          "cachedContentTokenCount": 40, "totalTokenCount": 150}}


def test_from_gemini_tool_turn():
    msg = g.from_gemini(_tool_turn_response(), "gemini-3.8-flash")
    assert msg["type"] == "message" and msg["role"] == "assistant" and msg["model"] == "gemini-3.8-flash"
    assert msg["id"].startswith("msg_")
    kinds = [b["type"] for b in msg["content"]]
    assert kinds == ["thinking", "text", "tool_use", "tool_use"]
    assert msg["content"][0]["thinking"] == "pondering"
    tu = msg["content"][2]
    assert tu["id"].startswith("toolu_") and g.SIG_SEP in tu["id"] and tu["input"] == {"path": "a.py"}
    assert g.SIG_SEP not in msg["content"][3]["id"]
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 60, "output_tokens": 50, "cache_read_input_tokens": 40}
    # id survives JSON and contains only Anthropic-legal id characters
    assert all(c.isalnum() or c in "_-" for c in tu["id"])


def test_multi_turn_round_trip_restores_signature():
    msg = g.from_gemini(_tool_turn_response(), "gemini-3.8-flash")
    tu1, tu2 = msg["content"][2], msg["content"][3]
    history = [
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": msg["content"]},  # Claude Code echoes thinking, text, tool_use
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tu1["id"], "content": "print(1)"},
            {"type": "tool_result", "tool_use_id": tu2["id"], "content": [{"type": "text", "text": "a.py"}],
             "is_error": True}]},
    ]
    out = g.to_gemini({"model": "gemini-3.8-flash", "messages": history})
    model_parts = out["contents"][1]["parts"]
    assert model_parts[0] == {"text": "Let me look."}  # thinking dropped
    assert model_parts[1]["thoughtSignature"] == SIG
    assert model_parts[1]["functionCall"] == {"name": "read_file", "args": {"path": "a.py"}}
    assert "thoughtSignature" not in model_parts[2]  # parallel call has none and already first one has
    resp_parts = out["contents"][2]["parts"]
    assert resp_parts[0] == {"functionResponse": {"name": "read_file", "response": {"content": "print(1)"}}}
    assert resp_parts[1] == {"functionResponse": {"name": "ls",
                                                  "response": {"content": "a.py", "is_error": True}}}


def test_dummy_signature_for_gemini3_only():
    hist = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x", "name": "f", "input": {"a": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}]}]
    out3 = g.to_gemini({"model": "gemini-3.5-flash-lite", "messages": hist})
    part = out3["contents"][1]["parts"][0]
    assert base64.b64decode(part["thoughtSignature"]) == b"skip_thought_signature_validator"
    assert out3["contents"][2]["parts"][0]["functionResponse"]["name"] == "f"
    out25 = g.to_gemini({"model": "gemini-2.5-flash", "messages": hist})
    assert "thoughtSignature" not in out25["contents"][1]["parts"][0]


def test_unknown_tool_result_id_does_not_crash():
    out = g.to_gemini({"model": "m", "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "nope", "content": "z"}]}]})
    assert out["contents"][0]["parts"][0]["functionResponse"]["name"] == "unknown_tool"


# ---- response variants ----

def test_from_gemini_finish_reasons():
    def r(finish, parts=None):
        return {"candidates": [{"content": {"parts": parts or [{"text": "t"}]}, "finishReason": finish}]}
    assert g.from_gemini(r("STOP"), "m")["stop_reason"] == "end_turn"
    assert g.from_gemini(r("MAX_TOKENS"), "m")["stop_reason"] == "max_tokens"
    assert g.from_gemini(r("SAFETY"), "m")["stop_reason"] == "refusal"
    assert g.from_gemini(r("RECITATION"), "m")["stop_reason"] == "refusal"
    assert g.from_gemini(r("OTHER"), "m")["stop_reason"] == "end_turn"
    assert g.from_gemini(r("MAX_TOKENS", [{"functionCall": {"name": "f", "args": {}}}]), "m")["stop_reason"] \
        == "max_tokens"


def test_from_gemini_prompt_blocked():
    msg = g.from_gemini({"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 5}}, "m")
    assert msg["stop_reason"] == "refusal"
    assert "SAFETY" in msg["content"][0]["text"]
    assert msg["usage"] == {"input_tokens": 5, "output_tokens": 0}


# ---- streaming ----

def _run(model, chunks):
    t = g.StreamTranslator(model)
    ev = []
    for c in chunks:
        ev += t.feed(c)
    ev += t.finish()
    return ev


def _c(parts, finish=None, usage=None):
    cand = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish:
        cand["finishReason"] = finish
    c = {"candidates": [cand]}
    if usage:
        c["usageMetadata"] = usage
    return c


def test_stream_text_across_chunks():
    ev = _run("gm", [_c([{"text": "Hel"}]), _c([{"text": "lo "}]),
                     _c([{"text": "world"}], "STOP", {"promptTokenCount": 7, "candidatesTokenCount": 3})])
    assert [e for e, _ in ev] == ["message_start", "content_block_start"] + ["content_block_delta"] * 3 + \
        ["content_block_stop", "message_delta", "message_stop"]
    assert "".join(d["delta"]["text"] for e, d in ev if e == "content_block_delta") == "Hello world"
    assert ev[0][1]["message"]["model"] == "gm"
    md = ev[-2][1]
    assert md["delta"]["stop_reason"] == "end_turn" and md["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_stream_thinking_text_tool_order_and_signature():
    ev = _run("gemini-3.8-flash", [
        _c([{"text": "think a", "thought": True}]),
        _c([{"text": " b", "thought": True, "thoughtSignature": "SIGTHINK"}]),
        _c([{"text": "answer"}]),
        _c([{"functionCall": {"name": "f", "args": {"x": 1}}, "thoughtSignature": SIG}], "STOP",
           {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 4}),
    ])
    names = [e for e, _ in ev]
    assert names == ["message_start",
                     "content_block_start", "content_block_delta", "content_block_delta", "content_block_delta",
                     "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    deltas = [d["delta"]["type"] for e, d in ev if e == "content_block_delta"]
    assert deltas == ["thinking_delta", "thinking_delta", "signature_delta", "text_delta", "input_json_delta"]
    starts = [d for e, d in ev if e == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1, 2]
    assert [s["content_block"]["type"] for s in starts] == ["thinking", "text", "tool_use"]
    tool = starts[2]["content_block"]
    assert tool["input"] == {} and g.SIG_SEP in tool["id"]
    assert json.loads([d for e, d in ev if e == "content_block_delta"][-1]["delta"]["partial_json"]) == {"x": 1}
    stops = [d["index"] for e, d in ev if e == "content_block_stop"]
    assert stops == [0, 1, 2]
    md = ev[-2][1]
    assert md["delta"]["stop_reason"] == "tool_use" and md["usage"]["output_tokens"] == 9


def test_stream_tool_id_signature_roundtrip():
    ev = _run("gemini-3.8-flash", [_c([{"functionCall": {"name": "f", "args": {}}, "thoughtSignature": SIG}], "STOP")])
    tid = [d for e, d in ev if e == "content_block_start"][0]["content_block"]["id"]
    out = g.to_gemini({"model": "gemini-3.8-flash", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "f", "input": {}}]}]})
    assert out["contents"][0]["parts"][0]["thoughtSignature"] == SIG


def test_stream_error_in_200():
    t = g.StreamTranslator("m")
    t.feed(_c([{"text": "a"}]))
    with pytest.raises(g.GeminiStreamError) as ei:
        t.feed({"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
    assert ei.value.code == 503 and ei.value.status == "UNAVAILABLE" and "overloaded" in str(ei.value)


def test_stream_max_tokens_and_empty_and_idempotent_finish():
    ev = _run("m", [_c([{"text": "x"}], "MAX_TOKENS")])
    assert ev[-2][1]["delta"]["stop_reason"] == "max_tokens"
    t = g.StreamTranslator("m")
    ev = t.finish()
    assert [e for e, _ in ev] == ["message_start", "message_delta", "message_stop"]
    assert t.finish() == []


def test_stream_blocked_prompt():
    ev = _run("m", [{"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}])
    assert ev[-2][1]["delta"]["stop_reason"] == "refusal"


# ---- review fixes ----

def test_schema_format_only_enum_and_date_time():
    p = g.sanitize_schema({"type": "object", "properties": {
        "u": {"type": "string", "format": "uri"},
        "d": {"type": "string", "format": "date-time"},
        "n": {"type": "integer", "format": "int64"},
    }})["properties"]
    assert p["u"] == {"type": "string"}
    assert p["d"] == {"type": "string", "format": "date-time"}
    assert p["n"] == {"type": "integer"}


def test_schema_nested_empty_properties_dropped():
    out = g.sanitize_schema({"type": "object", "properties": {
        "o": {"type": "object", "properties": {}, "required": []},
        "a": {"type": "array", "items": {"type": "object", "properties": {}}}}})
    assert out["properties"]["o"] == {"type": "object"}
    assert out["properties"]["a"]["items"] == {"type": "object"}


def test_schema_untyped_property_gets_type():
    out = g.sanitize_schema({"type": "object", "properties": {"any": {}, "t": True, "x": {"description": "d"}}})
    assert out["properties"]["any"] == {"type": "object"}
    assert out["properties"]["t"] == {"type": "object"}
    assert out["properties"]["x"] == {"description": "d", "type": "object"}


def test_schema_empty_string_enum_becomes_null():
    out = g.sanitize_schema({"type": "object", "properties": {"e": {"type": "string", "enum": ["", "a"]}}})
    assert out["properties"]["e"]["enum"] == [None, "a"]


def test_schema_ref_fanout_is_bounded():
    defs = {"L0": {"type": "string"}}
    for i in range(1, 25):  # each level references the previous twice: 2**24 expansions if unbounded
        defs[f"L{i}"] = {"type": "object", "properties": {"a": {"$ref": f"#/$defs/L{i-1}"},
                                                          "b": {"$ref": f"#/$defs/L{i-1}"}}}
    out = g.sanitize_schema({"type": "object", "$defs": defs, "properties": {"r": {"$ref": "#/$defs/L24"}}})
    assert out["properties"]["r"]["type"] == "object"
    assert len(json.dumps(out)) < 2_000_000


def test_schema_deep_nesting_bounded():
    node: dict = {"type": "string"}
    for _ in range(100):
        node = {"type": "object", "properties": {"c": node}}
    assert g.sanitize_schema(node)["type"] == "object"


def test_schema_allof_merge():
    out = g.sanitize_schema({"allOf": [
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"properties": {"b": {"type": "integer"}}, "required": ["b"], "description": "merged"}]})
    assert out == {"type": "object", "description": "merged", "required": ["a", "b"],
                   "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}


def test_schema_ref_tilde_escapes_and_dangling():
    s = {"type": "object", "$defs": {"a/b": {"type": "string", "description": "slash"}},
         "properties": {"s": {"$ref": "#/$defs/a~1b"}, "gone": {"$ref": "#/$defs/missing"},
                        "remote": {"$ref": "https://example.com/x.json"}}}
    p = g.sanitize_schema(s)["properties"]
    assert p["s"] == {"type": "string", "description": "slash"}
    assert p["gone"] == {"type": "object"} and p["remote"] == {"type": "object"}


def test_realistic_large_signature_round_trip():
    sig = base64.b64encode(bytes(range(256)) * 8).decode()  # ~2.7 KB with + / =
    resp = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "f", "args": {}}, "thoughtSignature": sig}]}, "finishReason": "STOP"}]}
    tid = g.from_gemini(resp, "gemini-3.8-flash")["content"][0]["id"]
    assert len(tid) > 2000 and all(c.isalnum() or c in "_-" for c in tid)
    out = g.to_gemini({"model": "gemini-3.8-flash", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "f", "input": {}}]}]})
    assert out["contents"][0]["parts"][0]["thoughtSignature"] == sig


def test_tool_result_image_becomes_inline_data():
    out = g.to_gemini({"model": "m", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "shot", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "done"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]}]}]})
    assert out["contents"][1]["parts"] == [
        {"functionResponse": {"name": "shot", "response": {"content": "done"}}},
        {"inlineData": {"mimeType": "image/jpeg", "data": "QUJD"}}]


def test_thinking_types():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    ad = g.to_gemini({**base, "thinking": {"type": "adaptive"}})
    assert ad["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    assert "generationConfig" not in g.to_gemini({**base, "thinking": {"type": "disabled"}})


def test_disable_parallel_tool_use_ignored():
    out = g.to_gemini({"model": "m", "messages": [{"role": "user", "content": "x"}],
                       "tools": [{"name": "f", "input_schema": {"type": "object", "properties": {}}}],
                       "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}})
    assert out["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}


def test_stream_empty_thought_part_opens_no_block():
    ev = _run("m", [_c([{"thought": True, "text": ""}]), _c([{"text": "hi"}], "STOP")])
    starts = [d["content_block"]["type"] for e, d in ev if e == "content_block_start"]
    assert starts == ["text"]


def test_stream_first_chunk_usage_in_message_start():
    ev = _run("m", [_c([{"text": "a"}], usage={"promptTokenCount": 42, "cachedContentTokenCount": 2}),
                    _c([{"text": "b"}], "STOP", {"promptTokenCount": 42, "candidatesTokenCount": 3})])
    assert ev[0][1]["message"]["usage"]["input_tokens"] == 40


def test_stream_text_then_function_call_one_chunk():
    ev = _run("m", [_c([{"text": "ok"}, {"functionCall": {"name": "f", "args": {"a": 1}}}], "STOP")])
    assert [e for e, _ in ev] == ["message_start", "content_block_start", "content_block_delta",
                                  "content_block_stop", "content_block_start", "content_block_delta",
                                  "content_block_stop", "message_delta", "message_stop"]
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"
