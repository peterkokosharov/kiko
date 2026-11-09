# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite.chat_front units: OpenAI chat request -> Anthropic body, Anthropic message / events -> OpenAI."""
import json

import pytest

from kiko_lite import chat_front as cf
from kiko_lite.providers import openai_compat as oc


def conv(**kw):
    return cf.convert_request({"model": "m", "messages": [{"role": "user", "content": "hi"}], **kw})[0]


def test_basic_defaults_and_system():
    a, opts = cf.convert_request({"model": "m", "messages": [
        {"role": "system", "content": "be brief"}, {"role": "developer", "content": [{"type": "text", "text": "dev"}]},
        {"role": "user", "content": "hi"}], "unknown_field": 1})
    assert a == {"model": "m", "system": "be brief\n\ndev",
                 "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    assert opts == {"stream": False, "include_usage": False, "model": "m"}


def test_max_tokens_stop_sampling_stream_options():
    a, opts = cf.convert_request({"model": "m", "messages": [], "max_tokens": 5, "max_completion_tokens": 7,
                                  "stop": "x", "temperature": 0.2, "top_p": 0.9, "stream": True,
                                  "stream_options": {"include_usage": True}})
    assert (a["max_tokens"], a["stop_sequences"], a["temperature"], a["top_p"], a["stream"]) == (7, ["x"], 0.2, 0.9, True)
    assert opts["include_usage"] is True
    assert conv(max_tokens=5)["max_tokens"] == 5
    assert conv(stop=["a", "b"])["stop_sequences"] == ["a", "b"]


def test_n_greater_than_one_rejected():
    with pytest.raises(cf.ChatRequestError):
        conv(n=2)
    assert "n" not in conv(n=1)


@pytest.mark.parametrize("bad", [{"messages": "x"}, {"model": ""}, {"tool_choice": "weird"}, {"tools": "x"}])
def test_bad_requests(bad):
    body = {"model": "m", "messages": [], **bad}
    with pytest.raises(cf.ChatRequestError):
        cf.convert_request(body)


def test_tools_and_tool_choice():
    tools = [{"type": "function", "function": {"name": "f", "description": "d",
                                               "parameters": {"type": "object", "properties": {"x": {}}}}},
             {"type": "function", "function": {"name": "g"}}]
    a = conv(tools=tools, tool_choice="required")
    assert a["tools"][0] == {"name": "f", "description": "d",
                             "input_schema": {"type": "object", "properties": {"x": {}}}}
    assert a["tools"][1]["input_schema"] == {"type": "object", "properties": {}}
    assert a["tool_choice"] == {"type": "any"}
    assert conv(tool_choice="auto")["tool_choice"] == {"type": "auto"}  # accepted without tools
    assert conv(tool_choice="none")["tool_choice"] == {"type": "none"}
    assert conv(tools=tools, tool_choice={"type": "function", "function": {"name": "f"}})["tool_choice"] == \
        {"type": "tool", "name": "f"}


def test_tool_round_trip_messages_merged():
    a = conv(messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"x": 1}'}},
            {"id": "call_2", "type": "function", "function": {"name": "g", "arguments": "not json"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": "r2"}]},
        {"role": "user", "content": "thanks"}])["messages"]
    assert [m["role"] for m in a] == ["user", "assistant", "user"]
    assert a[1]["content"] == [
        {"type": "tool_use", "id": "toolu_call_1", "name": "f", "input": {"x": 1}},
        {"type": "tool_use", "id": "toolu_call_2", "name": "g", "input": {"_raw": "not json"}}]
    assert a[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_call_1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "toolu_call_2", "content": "r2"},
        {"type": "text", "text": "thanks"}]
    assert oc.upstream_id(a[1]["content"][0]["id"]) == "call_1"


def test_image_parts():
    a = conv(messages=[{"role": "user", "content": [
        {"type": "text", "text": "see"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}]}])
    assert a["messages"][0]["content"][1:] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
        {"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}}]
    with pytest.raises(cf.ChatRequestError):
        conv(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "ftp://x"}}]}])


def test_convert_response_text_and_tools():
    r = cf.convert_response({"content": [{"type": "text", "text": "hi"}], "stop_reason": "max_tokens",
                             "usage": {"input_tokens": 4, "output_tokens": 2}}, "mod")
    assert r["object"] == "chat.completion" and r["id"].startswith("chatcmpl-") and isinstance(r["created"], int)
    assert r["choices"] == [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "length"}]
    assert r["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    t = cf.convert_response({"content": [{"type": "tool_use", "id": "toolu_abc", "name": "f", "input": {"a": 1}}],
                             "stop_reason": "tool_use", "usage": {}}, "mod")
    ch = t["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None
    assert ch["message"]["tool_calls"] == [{"id": "abc", "type": "function",
                                            "function": {"name": "f", "arguments": '{"a": 1}'}}]
    for sr, want in (("end_turn", "stop"), ("stop_sequence", "stop"), ("refusal", "content_filter")):
        assert cf.finish_reason(sr) == want


def frames(chunks):
    out = []
    for b in chunks:
        assert b.startswith(b"data: ") and b.endswith(b"\n\n")
        t = b[6:-2].decode()
        out.append(t if t == "[DONE]" else json.loads(t))
    return out


def test_stream_translation():
    s = cf.ChatStream("mod", include_usage=True)
    evs = [("message_start", {"type": "message_start", "message": {}}),
           ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
           ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "He"}}),
           ("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_z", "name": "f"}}),
           ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"a"'}}),
           ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": ": 1}"}}),
           ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 3, "output_tokens": 4}}),
           ("message_stop", {})]
    out = []
    for n, e in evs:
        out += s.event(n, e)
    out += s.close()
    fr = frames(out)
    assert fr[-1] == "[DONE]"
    body = fr[:-2]
    for c in body:
        assert set(c) == {"id", "object", "created", "model", "choices"}
        assert c["object"] == "chat.completion.chunk"
    assert body[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert body[1]["choices"][0]["delta"] == {"content": "He"}
    tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in body[2:5]]
    assert all(t["id"] == "z" and t["type"] == "function" and t["index"] == 0 for t in tcs)
    assert tcs[0]["function"]["name"] == "f"
    assert "".join(t["function"]["arguments"] for t in tcs) == '{"a": 1}'
    assert body[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert fr[-2]["choices"] == [] and fr[-2]["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert cf.ChatStream.ping() == b": keepalive\n\n"


def test_error_body_shape():
    e = cf.error_body("invalid_request_error", "bad")["error"]
    assert e == {"message": "bad", "type": "invalid_request_error", "param": None, "code": None}
    assert cf.error_body("authentication_error", "x")["error"]["code"] == "invalid_api_key"


def test_max_tokens_omitted_when_client_sends_none():
    assert "max_tokens" not in conv()
    assert conv(max_completion_tokens=9)["max_tokens"] == 9


def test_stream_error_frame_is_openai_shaped_and_ends_with_done():
    out = cf.ChatStream("m").error("overloaded_error", "busy")
    frame, done = out.split(b"\n\n")[:2]
    assert frame.startswith(b"data: ") and done == b"data: [DONE]"
    assert json.loads(frame[6:]) == {"error": {"message": "busy", "type": "server_error", "param": None,
                                               "code": "overloaded"}}
