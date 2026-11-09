# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite.responses_front: pure conversion both ways (no network)."""
import json

import pytest

from kiko_lite import responses_front as rf
from kiko_lite.chat_front import ChatRequestError


def conv(**kw):
    return rf.convert_request({"model": "m", "input": [], **kw})[0]


def test_codex_request_shape():
    a, opts = rf.convert_request({
        "model": "m", "stream": True, "store": False, "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "low"}, "prompt_cache_key": "k", "client_metadata": {}, "service_tier": "x",
        "text": {}, "tool_choice": "auto", "parallel_tool_calls": False,
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "be brief"}]},
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "hi"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
            {"type": "reasoning", "summary": [], "encrypted_content": "zz"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "calling"}]},
            {"type": "function_call", "name": "f", "arguments": '{"a":1}', "call_id": "call_1"},
            {"type": "function_call_output", "call_id": "call_1", "output": "done"},
        ],
        "tools": [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}},
                  {"type": "web_search"}, {"type": "custom", "name": "apply_patch"}]})
    assert opts == {"stream": True, "model": "m"}
    assert a["system"] == "be brief" and a["stream"] is True and "max_tokens" not in a
    assert a["tools"] == [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]
    assert a["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    u, asst, res = a["messages"]
    assert u["content"][0] == {"type": "text", "text": "hi"}
    assert u["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert asst["role"] == "assistant" and asst["content"][0] == {"type": "text", "text": "calling"}
    tu = asst["content"][1]
    assert tu["type"] == "tool_use" and tu["name"] == "f" and tu["input"] == {"a": 1}
    assert res["role"] == "user" and res["content"] == [
        {"type": "tool_result", "tool_use_id": tu["id"], "content": "done"}]


def test_string_input_instructions_max_tokens():
    a = rf.convert_request({"model": "m", "input": "yo", "instructions": "sys", "max_output_tokens": 9,
                            "temperature": 0.5})[0]
    assert a["messages"] == [{"role": "user", "content": [{"type": "text", "text": "yo"}]}]
    assert a["system"] == "sys" and a["max_tokens"] == 9 and a["temperature"] == 0.5 and "stream" not in a


def test_two_calls_and_outputs_merge_in_order_and_list_output():
    a = conv(input=[
        {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c1"},
        {"type": "function_call", "name": "g", "arguments": "not json", "call_id": "c2"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "x"},
                                                                       {"type": "input_text", "text": "y"}]},
        {"type": "function_call_output", "call_id": "c2", "output": [
            {"type": "input_image", "image_url": "https://e/x.png"}]}])
    asst, user = a["messages"]
    assert [b["name"] for b in asst["content"]] == ["f", "g"] and asst["content"][1]["input"] == {"_raw": "not json"}
    assert [b["tool_use_id"] for b in user["content"]] == [b["id"] for b in asst["content"]]
    assert user["content"][0]["content"] == "x\ny"
    assert user["content"][1]["content"] == [{"type": "image", "source": {"type": "url", "url": "https://e/x.png"}}]


def test_tool_choice_and_parallel():
    t = [{"type": "function", "name": "f"}]
    assert conv(tools=t, tool_choice="required")["tool_choice"] == {"type": "any"}
    assert conv(tools=t, tool_choice={"type": "function", "name": "f"})["tool_choice"] == {"type": "tool", "name": "f"}
    assert conv(tools=t, tool_choice="none", parallel_tool_calls=False)["tool_choice"] == {"type": "none"}
    assert conv(tools=t, parallel_tool_calls=False)["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "tool_choice" not in conv(tools=t, parallel_tool_calls=True)
    assert "tools" not in conv(tools=[{"type": "web_search"}], parallel_tool_calls=False)


@pytest.mark.parametrize("body", [
    [], {"input": []}, {"model": "m"}, {"model": "m", "input": 3},
    {"model": "m", "input": [{"type": "local_shell_call"}]},
    {"model": "m", "input": [{"type": "message", "role": "tool", "content": "x"}]},
    {"model": "m", "input": [{"type": "function_call_output", "output": "x"}]},
    {"model": "m", "input": [], "tool_choice": "weird", "tools": [{"type": "function", "name": "f"}]},
    {"model": "m", "input": [], "tools": [{"type": "function"}]},
])
def test_bad_requests(body):
    with pytest.raises(ChatRequestError):
        rf.convert_request(body)


def test_convert_response_and_usage():
    r = rf.convert_response({"stop_reason": "tool_use", "content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "toolu_abc", "name": "f", "input": {"a": 1}}],
        "usage": {"input_tokens": 4, "cache_read_input_tokens": 6, "output_tokens": 3}}, "m")
    assert r["object"] == "response" and r["status"] == "completed" and r["model"] == "m"
    msg, call = r["output"]
    assert msg["content"] == [{"type": "output_text", "text": "hi", "annotations": []}]
    assert call["type"] == "function_call" and call["call_id"] == "abc" and call["arguments"] == '{"a": 1}'
    assert r["usage"] == {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                          "input_tokens_details": {"cached_tokens": 6},
                          "output_tokens_details": {"reasoning_tokens": 0}}
    assert rf.convert_response({"stop_reason": "max_tokens", "content": []}, "m")["status"] == "incomplete"


def frames(raw):
    return [json.loads(b[len(b"data: "):]) for b in raw]


def test_stream_text_and_two_calls():
    s = rf.ResponsesStream("m")
    evs = [("message_start", {}),
           ("content_block_start", {"index": 0, "content_block": {"type": "thinking"}}),
           ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}}),
           ("content_block_stop", {"index": 0}),
           ("content_block_start", {"index": 1, "content_block": {"type": "text"}}),
           ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "Hel"}}),
           ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "lo"}}),
           ("content_block_stop", {"index": 1}),
           ("content_block_start", {"index": 2, "content_block": {"type": "tool_use", "id": "toolu_a", "name": "f"}}),
           ("content_block_delta", {"index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"a"'}}),
           ("content_block_delta", {"index": 2, "delta": {"type": "input_json_delta", "partial_json": ":1}"}}),
           ("content_block_stop", {"index": 2}),
           ("content_block_start", {"index": 3, "content_block": {"type": "tool_use", "id": "toolu_b", "name": "g"}}),
           ("content_block_stop", {"index": 3}),
           ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 5, "output_tokens": 2}})]
    out = []
    for n, e in evs:
        out += s.event(n, e)
    out += s.close()
    fr = frames(out)
    assert [f["sequence_number"] for f in fr] == list(range(len(fr)))
    types = [f["type"] for f in fr]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert "".join(f["delta"] for f in fr if f["type"] == "response.output_text.delta") == "Hello"
    done = [f for f in fr if f["type"] == "response.output_item.done"]
    assert [d["output_index"] for d in done] == [0, 1, 2]
    assert done[0]["item"]["content"][0]["text"] == "Hello"
    assert done[1]["item"]["arguments"] == '{"a":1}' and done[1]["item"]["call_id"] == "a"
    assert done[2]["item"]["arguments"] == "{}" and done[2]["item"]["call_id"] == "b"
    resp = fr[-1]["response"]
    assert resp["status"] == "completed" and resp["output"] == [d["item"] for d in done]
    assert resp["usage"]["total_tokens"] == 7 and resp["id"] == fr[0]["response"]["id"]


def test_stream_error_and_incomplete():
    s = rf.ResponsesStream("m")
    f = frames([s.error("rate_limit_error", "slow")])[0]
    assert f["type"] == "response.failed" and f["response"]["status"] == "failed"
    assert f["response"]["error"] == {"code": "rate_limit_exceeded", "message": "slow"}
    s = rf.ResponsesStream("m")
    s.event("message_delta", {"delta": {"stop_reason": "max_tokens"}, "usage": {}})
    last = frames(s.close())[-1]
    assert last["type"] == "response.incomplete" and last["response"]["incomplete_details"] == {
        "reason": "max_output_tokens"}


def test_error_code_mapping_pins_codex_codes():
    ec = rf.error_code
    assert ec("invalid_request_error", "prompt is too long: 250000 tokens > 200000 maximum") == "context_length_exceeded"
    assert ec("invalid_request_error", "This model's maximum context length is 8192 tokens") == "context_length_exceeded"
    assert ec("request_too_large", "Request exceeds the maximum size") == "context_length_exceeded"
    assert ec("rate_limit_error", "You exceeded your current quota") == "insufficient_quota"
    assert ec("invalid_request_error", "Your credit balance is too low") == "insufficient_quota"
    gem = "You exceeded your current quota, please check your plan. Quota exceeded for metric: requests per minute."
    assert ec("rate_limit_error", gem + " Please retry in 12s.") == "rate_limit_exceeded"
    assert ec("rate_limit_error", "Quota exceeded for metric: requests per day") == "insufficient_quota"
    assert ec("rate_limit_error", "Monthly spending cap of 50 USD reached") == "insufficient_quota"
    assert ec("invalid_request_error", "Unknown field billing_address") == "invalid_prompt"
    assert ec("permission_error", "forbidden") == "invalid_prompt"
    assert ec("rate_limit_error", "slow down") == "rate_limit_exceeded"
    assert ec("overloaded_error", "busy") == "server_is_overloaded"
    assert ec("invalid_request_error", "bad field") == "invalid_prompt"
    assert ec("api_error", "boom") is None
    assert rf.error_body("overloaded_error", "x")["error"]["code"] == "server_is_overloaded"
    assert rf.error_body("api_error", "x")["error"]["code"] is None


def test_stream_error_codes_for_codex():
    for typ, msg, code in (("invalid_request_error", "prompt is too long", "context_length_exceeded"),
                           ("rate_limit_error", "insufficient_quota", "insufficient_quota"),
                           ("overloaded_error", "busy", "server_is_overloaded"),
                           ("rate_limit_error", "quota exceeded. Please retry in 12s.", "rate_limit_exceeded"),
                           ("rate_limit_error", "Monthly spending cap of 5 USD reached", "insufficient_quota"),
                           ("permission_error", "forbidden", "invalid_prompt"),
                           ("invalid_request_error", "bad", "invalid_prompt")):
        f = frames([rf.ResponsesStream("m").error(typ, msg)])[0]
        assert f["response"]["error"]["code"] == code
