# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""OpenAI chat completions translator (pure functions, hand-written fixtures)."""
import json

import pytest

from kiko_lite.providers import lookup
from kiko_lite.providers import openai_compat as oc


def _req(**kw):
    return {"model": "claude-x", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}], **kw}


def test_endpoint_and_headers():
    assert oc.endpoint("https://x.test/v1/") == "https://x.test/v1/chat/completions"
    assert oc.headers("K")["authorization"] == "Bearer K"
    assert oc.headers("K", "header:api-key")["api-key"] == "K"
    assert "K" not in oc.endpoint("https://x.test/v1")


def test_basic_request_mapping():
    body = _req(system=[{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": "B"}],
                temperature=0.3, top_p=0.8, stop_sequences=["END"], stream=True, metadata={"user_id": "u"},
                thinking={"type": "enabled", "budget_tokens": 1000}, top_k=5, anthropic_beta=["x"],
                messages=[
                    {"role": "user", "content": [
                        {"type": "text", "text": "look", "cache_control": {"type": "ephemeral"}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
                    {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "s"},
                                                      {"type": "text", "text": "ok"}]}])
    out = oc.to_openai(body, "up-model")
    assert out["model"] == "up-model"
    assert out["messages"][0] == {"role": "system", "content": "A\nB"}
    assert out["messages"][1] == {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    assert out["messages"][2] == {"role": "assistant", "content": "ok"}
    assert out["max_tokens"] == 50 and out["temperature"] == 0.3 and out["top_p"] == 0.8
    assert out["stop"] == ["END"]
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True}
    for dropped in ("metadata", "thinking", "top_k", "anthropic_beta", "system"):
        assert dropped not in out


def test_provider_flags():
    out = oc.to_openai(_req(stream=True), "m", max_tokens_field="max_completion_tokens", stream_usage=False)
    assert out["max_completion_tokens"] == 50 and "max_tokens" not in out
    assert out["stream"] is True and "stream_options" not in out
    assert "stream" not in oc.to_openai(_req(), "m")


def test_tool_round_trip_request():
    body = _req(messages=[
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "toolu_call_1", "name": "Bash", "input": {"cmd": "ls"}},
            {"type": "tool_use", "id": "toolu_bad", "name": "Bash", "input": {"_raw": "{oops"}}]},
        {"role": "user", "content": [
            {"type": "text", "text": "after"},
            {"type": "tool_result", "tool_use_id": "toolu_call_1", "content": [
                {"type": "text", "text": "a.txt"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]},
            {"type": "tool_result", "tool_use_id": "toolu_bad", "content": "fail", "is_error": True}]}])
    msgs = oc.to_openai(body, "m")["messages"]
    asst = msgs[1]
    assert asst["content"] == "ok"
    assert asst["tool_calls"][0] == {"id": "call_1", "type": "function",
                                     "function": {"name": "Bash", "arguments": json.dumps({"cmd": "ls"})}}
    assert asst["tool_calls"][1]["function"]["arguments"] == "{oops"  # {"_raw"} goes back verbatim
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "call_1"
    assert msgs[2]["content"].startswith("a.txt\n[image omitted")
    assert msgs[3] == {"role": "tool", "tool_call_id": "bad", "content": "fail"}
    assert msgs[4] == {"role": "user", "content": "after"}  # tool messages come first


def test_assistant_tool_only_has_null_content():
    body = _req(messages=[{"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_x", "name": "f", "input": {}}]}])
    assert oc.to_openai(body, "m")["messages"][0]["content"] is None


TOOLS = [{"name": "f", "description": "d", "input_schema": {"$schema": "x", "properties": {"a": {"type": "string"}}}},
         {"type": "web_search_20250305", "name": "web_search"}]


@pytest.mark.parametrize("choice,expected", [
    ({"type": "auto"}, "auto"), ({"type": "any"}, "required"), ({"type": "none"}, "none"),
    ({"type": "tool", "name": "f"}, {"type": "function", "function": {"name": "f"}}),
])
def test_tool_choice(choice, expected):
    out = oc.to_openai(_req(tools=TOOLS, tool_choice=choice), "m")
    assert out["tool_choice"] == expected
    assert out["tools"] == [{"type": "function", "function": {
        "name": "f", "description": "d", "parameters": {"properties": {"a": {"type": "string"}}, "type": "object"}}}]


def test_no_tool_choice_without_tools():
    assert "tool_choice" not in oc.to_openai(_req(tool_choice={"type": "any"}), "m")


# ---- response ----

def _resp(message, finish="stop", usage=None):
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


def test_text_response_and_usage():
    out = oc.from_openai(_resp({"content": "hello"}, usage={
        "prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}}), "claude-x")
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn" and out["model"] == "claude-x" and out["role"] == "assistant"
    assert out["usage"] == {"input_tokens": 60, "output_tokens": 7, "cache_read_input_tokens": 40}


@pytest.mark.parametrize("finish,expected", [
    ("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "refusal"), ("weird", "end_turn")])
def test_finish_reasons(finish, expected):
    assert oc.from_openai(_resp({"content": "x"}, finish), "m")["stop_reason"] == expected


def test_stop_sequence_when_text_still_ends_with_it():
    out = oc.from_openai(_resp({"content": "abc###"}), "m", ["###"])
    assert out["stop_reason"] == "stop_sequence" and out["stop_sequence"] == "###"
    assert oc.from_openai(_resp({"content": "abc"}), "m", ["###"])["stop_reason"] == "end_turn"


@pytest.mark.parametrize("message", [
    {"content": "answer", "reasoning_content": "because"},
    {"content": "answer", "reasoning": "because"},
    {"content": "<think>because</think>\n\nanswer"},
    {"content": "<think>\nbecause\n</think>answer"},
])
def test_reasoning_forms(message):
    out = oc.from_openai(_resp(message), "m")
    assert out["content"] == [{"type": "thinking", "thinking": "because", "signature": ""},
                              {"type": "text", "text": "answer"}]


def test_think_tag_only_at_start():
    out = oc.from_openai(_resp({"content": "see <think>x</think>"}), "m")
    assert out["content"] == [{"type": "text", "text": "see <think>x</think>"}]


def test_tool_calls_response():
    msg = {"content": None, "tool_calls": [
        {"id": "call_a", "type": "function", "function": {"name": "f", "arguments": '{"x": 1}'}},
        {"id": "call_b", "type": "function", "function": {"name": "g", "arguments": "{broken"}},
        {"id": None, "type": "function", "function": {"name": "h", "arguments": ""}}]}
    out = oc.from_openai(_resp(msg, "tool_calls"), "m")
    c = out["content"]
    assert out["stop_reason"] == "tool_use"
    assert c[0] == {"type": "tool_use", "id": "toolu_call_a", "name": "f", "input": {"x": 1}}
    assert c[1]["input"] == {"_raw": "{broken"}
    assert c[2]["id"].startswith("toolu_") and c[2]["input"] == {}
    assert oc.upstream_id(c[0]["id"]) == "call_a"


def test_tool_calls_with_finish_stop_is_tool_use():
    msg = {"tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{}"}}]}
    assert oc.from_openai(_resp(msg, "stop"), "m")["stop_reason"] == "tool_use"


def test_empty_message_gets_empty_text_block():
    assert oc.from_openai(_resp({"content": None}), "m")["content"] == [{"type": "text", "text": ""}]


def test_error_object_raises():
    with pytest.raises(oc.OpenAIStreamError, match="boom"):
        oc.from_openai({"error": {"message": "boom", "type": "server_error"}}, "m")


def test_providers_table_lookup():
    name, entry = lookup("Qwen")
    assert name == "dashscope" and entry["kind"] == "openai"
    assert lookup("openai")[1]["flags"] == {"max_tokens_field": "max_completion_tokens"}
    assert lookup("gemini")[1]["kind"] == "gemini"
    assert lookup("nope") is None


# ---- streaming ----

def _chunk(delta=None, finish=None, usage=None):
    c = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if (delta is not None or finish)
         else []}
    if usage:
        c["usage"] = usage
    return c


def _run(chunks, model="m", stops=None):
    tr = oc.StreamTranslator(model, stops)
    events = []
    for c in chunks:
        events += tr.feed(c)
    events += tr.finish()
    return events


def _names(events):
    return [e[0] for e in events]


def _text(events, idx_type="text_delta", key="text"):
    return "".join(e[1]["delta"][key] for e in events if e[0] == "content_block_delta"
                   and e[1]["delta"]["type"] == idx_type)


def test_stream_text_with_late_usage():
    ev = _run([_chunk({"role": "assistant", "content": ""}), _chunk({"content": "Hel"}), _chunk({"content": "lo"}),
               _chunk({}, "stop"), _chunk(usage={"prompt_tokens": 9, "completion_tokens": 2})])
    assert _names(ev) == ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                          "content_block_stop", "message_delta", "message_stop"]
    assert _text(ev) == "Hello"
    md = ev[-2][1]
    assert md["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert md["usage"] == {"input_tokens": 9, "output_tokens": 2}


def test_stream_reasoning_then_text():
    ev = _run([_chunk({"reasoning_content": "th"}), _chunk({"reasoning_content": "ink"}),
               _chunk({"content": "ans"}), _chunk({}, "stop")])
    starts = [e[1]["content_block"]["type"] for e in ev if e[0] == "content_block_start"]
    assert starts == ["thinking", "text"]
    assert _text(ev, "thinking_delta", "thinking") == "think" and _text(ev) == "ans"


def test_stream_think_tags_split_across_chunks():
    pieces = ["<th", "ink>rea", "son</thi", "nk>\n\nan", "swer"]
    ev = _run([_chunk({"content": p}) for p in pieces] + [_chunk({}, "stop")])
    assert _text(ev, "thinking_delta", "thinking") == "reason"
    assert _text(ev) == "answer"


def test_stream_plain_text_starting_with_angle_bracket():
    ev = _run([_chunk({"content": "<b"}), _chunk({"content": "old>x"}), _chunk({}, "stop")])
    assert _text(ev) == "<bold>x" and "thinking" not in json.dumps(ev)


def test_stream_unclosed_think_flushed_at_finish():
    ev = _run([_chunk({"content": "<think>abc</th"}), _chunk({}, "stop")])
    assert _text(ev, "thinking_delta", "thinking") == "abc</th"


def test_stream_tool_call_split_across_chunks():
    ev = _run([
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "Bash", "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"cm'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'd":"ls"}'}}]}),
        _chunk({}, "tool_calls"), _chunk(usage={"prompt_tokens": 3, "completion_tokens": 4})])
    start = next(e[1] for e in ev if e[0] == "content_block_start")
    assert start["content_block"] == {"type": "tool_use", "id": "toolu_call_1", "name": "Bash", "input": {}}
    assert json.loads(_text(ev, "input_json_delta", "partial_json")) == {"cmd": "ls"}
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_stream_text_tool_text_parallel_tools():
    ev = _run([
        _chunk({"content": "before"}),
        _chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f", "arguments": "{}"}},
                               {"index": 1, "id": "b", "function": {"name": "g", "arguments": '{"k":'}}]}),
        _chunk({"tool_calls": [{"index": 1, "function": {"arguments": "1}"}}]}),
        _chunk({"content": "after"}), _chunk({}, "tool_calls")])
    kinds = [(e[1]["index"], e[1]["content_block"]["type"]) for e in ev if e[0] == "content_block_start"]
    assert kinds == [(0, "text"), (1, "tool_use"), (2, "tool_use"), (3, "text")]
    stops = [e[1]["index"] for e in ev if e[0] == "content_block_stop"]
    assert stops == [0, 1, 2, 3]
    assert _text(ev, "input_json_delta", "partial_json") == '{}{"k":1}'


def test_stream_tool_name_arrives_late_and_missing_index():
    ev = _run([
        _chunk({"tool_calls": [{"id": "x", "function": {"arguments": '{"a"'}}]}),
        _chunk({"tool_calls": [{"function": {"name": "f", "arguments": ":1}"}}]}),
        _chunk({}, "stop")])
    assert [e[1]["content_block"]["name"] for e in ev if e[0] == "content_block_start"] == ["f"]
    assert _text(ev, "input_json_delta", "partial_json") == '{"a":1}'
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_stream_empty_response_still_valid():
    ev = _run([_chunk({}, "stop")])
    assert _names(ev) == ["message_start", "content_block_start", "content_block_stop", "message_delta",
                          "message_stop"]


def test_stream_finish_is_idempotent_and_stop_sequence():
    tr = oc.StreamTranslator("m", ["###"])
    tr.feed(_chunk({"content": "ab###"}))
    tr.feed(_chunk({}, "stop"))
    out = tr.finish()
    assert out[-2][1]["delta"] == {"stop_reason": "stop_sequence", "stop_sequence": "###"}
    assert tr.finish() == []


def test_stream_error_chunk_raises():
    tr = oc.StreamTranslator("m")
    tr.feed(_chunk({"content": "x"}))
    with pytest.raises(oc.OpenAIStreamError, match="overloaded"):
        tr.feed({"error": {"message": "overloaded", "type": "server_error", "code": 503}})
