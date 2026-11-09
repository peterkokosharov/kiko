# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko-lite POST /v1/responses end to end against a fake upstream."""
import json

import pytest

from kikolib import sse

from .test_kiko_lite_app import MASTER, chunk, env, oai  # noqa: F401  (env is a fixture)


def resp(env, body=None, stream=False, headers=None):
    return env.post("/v1/responses", {"model": "claude-sonnet-x", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}], **(body or {})},
        headers, stream=stream)


def frames(r):
    with r:
        return [json.loads(ev.data) for ev in sse.iter_events(r.iter_lines())]


def types(fr):
    return [f["type"] for f in fr]


def test_nonstream_text_and_usage(env):
    env.fake.add("openai", **oai("hello", usage={"prompt_tokens": 11, "completion_tokens": 3}))
    r = resp(env, {"max_output_tokens": 50})
    assert r.status == 200
    d = json.loads(r.read())
    assert d["object"] == "response" and d["status"] == "completed" and d["model"] == "claude-sonnet-x"
    assert d["output"][0]["content"][0]["text"] == "hello"
    assert d["usage"] == {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}
    sent = env.fake.requests[0]["body"]
    assert sent["model"] == "model-s" and sent["messages"][-1] == {"role": "user", "content": "hi"}
    assert sent["max_completion_tokens"] == 50
    assert env.results[-1].status == 200 and env.results[-1].route == "messages"


def test_stream_text(env):
    env.fake.add("openai", sse=[chunk({"role": "assistant", "content": "Hel"}), chunk({"content": "lo"}),
                                chunk({}, finish="stop"),
                                chunk(usage={"prompt_tokens": 5, "completion_tokens": 2}), "[DONE]"])
    r = resp(env, {"stream": True, "store": False, "include": ["reasoning.encrypted_content"]}, stream=True)
    assert r.status == 200
    fr = frames(r)
    t = types(fr)
    assert t[0] == "response.created" and t[-1] == "response.completed"
    assert t.index("response.output_item.added") < t.index("response.output_text.delta") < t.index(
        "response.output_item.done") < len(t) - 1
    assert "".join(f["delta"] for f in fr if f["type"] == "response.output_text.delta") == "Hello"
    done = next(f for f in fr if f["type"] == "response.output_item.done")["item"]
    assert done["type"] == "message" and done["content"][0] == {"type": "output_text", "text": "Hello",
                                                                 "annotations": []}
    final = fr[-1]["response"]
    assert final["id"].startswith("resp_") and final["output"] == [done]
    assert final["usage"] == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}
    assert env.results[-1].stream is True


def tc(i, **f):
    return chunk({"tool_calls": [{"index": i, **f}]})


def test_stream_one_function_call(env):
    env.fake.add("openai", sse=[tc(0, id="call_1", type="function", function={"name": "f", "arguments": '{"a"'}),
                                tc(0, function={"arguments": ":1}"}), chunk({}, finish="tool_calls"), "[DONE]"])
    fr = frames(resp(env, {"stream": True, "tools": [{"type": "function", "name": "f", "parameters": {
        "type": "object"}}, {"type": "web_search"}], "parallel_tool_calls": False}, stream=True))
    done = [f["item"] for f in fr if f["type"] == "response.output_item.done"]
    assert done == [{"type": "function_call", "id": done[0]["id"], "status": "completed", "call_id": "call_1",
                     "name": "f", "arguments": '{"a":1}'}]
    assert fr[-1]["type"] == "response.completed" and fr[-1]["response"]["output"] == done
    sent = env.fake.requests[0]["body"]
    assert [t["function"]["name"] for t in sent["tools"]] == ["f"]


def test_stream_two_function_calls(env):
    env.fake.add("openai", sse=[
        tc(0, id="call_a", type="function", function={"name": "f", "arguments": '{"a"'}),
        tc(0, function={"arguments": ":1}"}),
        tc(1, id="call_b", type="function", function={"name": "g", "arguments": '{"b"'}),
        tc(1, function={"arguments": ":2}"}),
        chunk({}, finish="tool_calls"), "[DONE]"])
    fr = frames(resp(env, {"stream": True}, stream=True))
    done = [f for f in fr if f["type"] == "response.output_item.done"]
    assert [(d["output_index"], d["item"]["call_id"], d["item"]["name"], d["item"]["arguments"]) for d in done] == [
        (0, "call_a", "f", '{"a":1}'), (1, "call_b", "g", '{"b":2}')]
    assert [i["call_id"] for i in fr[-1]["response"]["output"]] == ["call_a", "call_b"]
    assert all(set(f) >= {"type", "sequence_number"} for f in fr)


def test_function_call_output_round_trip(env):
    env.fake.add("openai", **oai("ok"))
    r = resp(env, {"input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run it"}]},
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "name": "f", "arguments": '{"a":1}', "call_id": "call_7"},
        {"type": "function_call_output", "call_id": "call_7", "output": "result text"}],
        "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}]})
    assert r.status == 200
    msgs = env.fake.requests[0]["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "rules"}
    asst = next(m for m in msgs if m["role"] == "assistant")
    call = asst["tool_calls"][0]
    assert call["id"] == "call_7" and call["function"] == {"name": "f", "arguments": '{"a": 1}'}
    tool = next(m for m in msgs if m["role"] == "tool")
    assert tool["tool_call_id"] == "call_7" and tool["content"] == "result text"


def test_stream_mid_stream_error_is_response_failed(env):
    env.fake.add("openai", sse=[chunk({"content": "par"}),
                                {"error": {"message": "engine died", "type": "server_error", "code": 500}}])
    fr = frames(resp(env, {"stream": True}, stream=True))
    assert fr[-1]["type"] == "response.failed" and fr[-1]["response"]["status"] == "failed"
    assert fr[-1]["response"]["error"]["message"] == "engine died"
    assert "response.completed" not in types(fr) and "response.output_text.delta" in types(fr)


def test_stream_upstream_status_and_cut_are_response_failed(env):
    env.fake.add("openai", status=400, json={"error": {"message": "context too long"}})
    fr = frames(resp(env, {"stream": True}, stream=True))
    assert types(fr) == ["response.failed"] and fr[0]["response"]["error"]["message"] == "context too long"
    env.fake.add("openai", sse=[chunk({"content": "half"})])
    fr = frames(resp(env, {"stream": True}, stream=True))
    assert fr[-1]["type"] == "response.failed"
    assert fr[-1]["response"]["error"]["message"] == "upstream stream ended unexpectedly"


def test_errors_and_auth(env):
    r = resp(env, {"input": 5})
    assert r.status == 400
    e = json.loads(r.read())["error"]
    assert e["type"] == "invalid_request_error" and set(e) == {"message", "type", "param", "code"}
    r = resp(env, {"model": "nowhere/x"})
    assert r.status == 404 and json.loads(r.read())["error"]["code"] == "model_not_found"
    for h in ({"x-api-key": "bad"}, {"x-api-key": "", "authorization": "Bearer wrong"}):
        r = resp(env, headers=h)
        assert r.status == 401 and json.loads(r.read())["error"]["code"] == "invalid_api_key"
    env.fake.add("openai", **oai("x"))
    assert resp(env, headers={"x-api-key": "", "authorization": "Bearer " + MASTER}).status == 200


def test_no_max_output_tokens_is_not_sent_upstream(env):
    env.fake.add("openai", **oai("x"))
    resp(env).read()
    sent = env.fake.requests[0]["body"]
    assert "max_tokens" not in sent and "max_completion_tokens" not in sent


def test_other_routes_unchanged(env):
    r = env.post("/v1/messages", {"model": "m", "messages": "x"})
    assert r.status == 400 and json.loads(r.read())["type"] == "error"
    env.fake.add("openai", **oai("hello"))
    d = json.loads(env.post("/v1/chat/completions", {"model": "claude-sonnet-x", "messages": [
        {"role": "user", "content": "hi"}]}).read())
    assert d["object"] == "chat.completion"


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("status,msg,code", [
    (400, "prompt is too long: 250000 tokens", "context_length_exceeded"),
    (413, "payload too large", "context_length_exceeded"),
    (429, "You exceeded your current quota", "insufficient_quota"),
    (429, "Quota exceeded for metric: requests per minute. Please retry in 12s.", "rate_limit_exceeded"),
    (429, "Quota exceeded for metric: requests per day", "insufficient_quota"),
    (429, "Monthly spending cap of 5 USD reached", "insufficient_quota"),
    (400, "Unknown field billing_address", "invalid_prompt"),
    (429, "slow down", "rate_limit_exceeded"),
    (503, "overloaded", "server_is_overloaded"),
    (400, "bad field", "invalid_prompt")])
def test_error_codes_for_codex(env, stream, status, msg, code):
    for _ in range(4):  # transient failures are retried (4 attempts)
        env.fake.add("openai", status=status, json={"error": {"message": msg}})
    if stream:
        fr = frames(resp(env, {"stream": True}, stream=True))
        assert fr[0]["type"] == "response.failed" and fr[0]["response"]["error"]["code"] == code
    else:
        assert json.loads(resp(env).read())["error"]["code"] == code
