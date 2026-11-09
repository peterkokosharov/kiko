# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko-lite OpenAI routes (POST /v1/chat/completions, GET /v1/models) end to end against a fake upstream."""
import json

from kikolib import sse

from .test_kiko_lite_app import MASTER, chunk, env, oai  # noqa: F401  (env is a fixture)

CHUNK_KEYS = {"id", "object", "created", "model", "choices"}


def chat(env, body, stream=False, headers=None):
    return env.post("/v1/chat/completions", {"model": "claude-sonnet-x", "messages": [{"role": "user", "content": "hi"}],
                                             **body}, headers, stream=stream)


def sse_frames(r):
    out = []
    with r:
        for ev in sse.iter_events(r.iter_lines()):
            out.append(ev.data if ev.data == "[DONE]" else json.loads(ev.data))
    return out


def test_nonstream_text(env):
    env.fake.add("openai", **oai("hello", usage={"prompt_tokens": 11, "completion_tokens": 3}))
    r = chat(env, {"max_tokens": 50})
    assert r.status == 200
    d = json.loads(r.read())
    assert d["object"] == "chat.completion" and d["model"] == "claude-sonnet-x"
    assert d["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert d["choices"][0]["finish_reason"] == "stop"
    assert d["usage"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    sent = env.fake.requests[0]["body"]
    assert sent["model"] == "model-s" and sent["messages"][-1] == {"role": "user", "content": "hi"}
    assert env.results[-1].status == 200 and env.results[-1].route == "messages"


def test_nonstream_tool_call_and_length(env):
    env.fake.add("openai", **oai(None, finish="tool_calls", tool_calls=[
        {"id": "call_9", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}]))
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    d = json.loads(chat(env, {"tools": tools}).read())
    ch = d["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None
    assert ch["message"]["tool_calls"] == [{"id": "call_9", "type": "function",
                                            "function": {"name": "f", "arguments": '{"a": 1}'}}]
    env.fake.add("openai", **oai("cut", finish="length"))
    assert json.loads(chat(env, {}).read())["choices"][0]["finish_reason"] == "length"


def text_stream(env):
    env.fake.add("openai", sse=[chunk({"role": "assistant", "content": "Hel"}), chunk({"content": "lo"}),
                                chunk({}, finish="stop"),
                                chunk(usage={"prompt_tokens": 5, "completion_tokens": 2}), "[DONE]"])


def test_stream_text_with_and_without_usage(env):
    text_stream(env)
    fr = sse_frames(chat(env, {"stream": True, "stream_options": {"include_usage": True}}, stream=True))
    assert fr[-1] == "[DONE]"
    body = fr[:-1]
    usage_chunk = body[-1]
    assert usage_chunk["choices"] == [] and usage_chunk["usage"] == {
        "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert set(usage_chunk) == CHUNK_KEYS | {"usage"}
    main = body[:-1]
    assert all(set(c) == CHUNK_KEYS for c in main)
    assert main[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in main) == "Hello"
    assert main[-1]["choices"][0]["finish_reason"] == "stop"
    text_stream(env)
    fr = sse_frames(chat(env, {"stream": True}, stream=True))
    assert fr[-1] == "[DONE]" and all(set(c) == CHUNK_KEYS for c in fr[:-1])
    assert env.results[-1].stream is True


def test_stream_tool_call_has_id_on_every_fragment_and_length(env):
    def tc(**f):
        return chunk({"tool_calls": [{"index": 0, **f}]})
    env.fake.add("openai", sse=[tc(id="call_1", type="function", function={"name": "f", "arguments": '{"a"'}),
                                tc(function={"arguments": ":1}"}), chunk({}, finish="length"), "[DONE]"])
    fr = sse_frames(chat(env, {"stream": True}, stream=True))
    body = fr[:-1]
    calls = [c["choices"][0]["delta"]["tool_calls"][0] for c in body if "tool_calls" in c["choices"][0]["delta"]]
    assert calls and all(c["id"] == "call_1" and c["type"] == "function" for c in calls)
    assert calls[0]["function"]["name"] == "f"
    assert "".join(c["function"].get("arguments", "") for c in calls) == '{"a":1}'
    assert body[-1]["choices"][0]["finish_reason"] == "length"
    assert all(set(c) == CHUNK_KEYS for c in body)


def test_models_lists_only_resolvable_and_needs_auth(env):
    r = env.client.request("GET", env.url + "/v1/models", {"authorization": "Bearer " + MASTER})
    d = json.loads(r.read())
    assert d["object"] == "list"
    ids = [m["id"] for m in d["data"]]
    assert "haiku" in ids and "opus" not in ids  # only the class with a target
    assert all(m["object"] == "model" and m["owned_by"] == "kiko" and m["created"] == 0 for m in d["data"])
    assert all(env.app.gateway.resolver(i) is not None for i in ids)
    for h in ({}, {"authorization": "Bearer wrong"}):
        r = env.client.request("GET", env.url + "/v1/models", h)
        assert r.status == 401
        e = json.loads(r.read())["error"]
        assert set(e) == {"message", "type", "param", "code"} and e["code"] == "invalid_api_key"
    r = env.client.request("GET", env.url + "/v1/models", {"x-api-key": MASTER})
    assert r.status == 200


def test_openai_error_shapes(env):
    r = chat(env, {"n": 2})
    assert r.status == 400
    e = json.loads(r.read())["error"]
    assert e["type"] == "invalid_request_error" and set(e) == {"message", "type", "param", "code"}
    r = chat(env, {"max_tokens": -1})
    assert r.status == 400 and "error" in json.loads(r.read())
    r = chat(env, {"model": "nowhere/x"})
    assert r.status == 404 and json.loads(r.read())["error"]["code"] == "model_not_found"
    r = env.post("/v1/chat/completions", {"model": "m", "messages": []}, {"x-api-key": "bad"})
    assert r.status == 401 and json.loads(r.read())["error"]["type"] == "authentication_error"


def test_messages_route_keeps_anthropic_error_shape(env):
    r = env.post("/v1/messages", {"model": "m", "messages": "x"})
    assert r.status == 400 and json.loads(r.read())["type"] == "error"
    r = env.post("/v1/messages", {"model": "m", "messages": []}, {"x-api-key": "bad"})
    assert r.status == 401 and json.loads(r.read())["type"] == "error"


def test_no_max_tokens_is_not_sent_upstream(env):
    env.fake.add("openai", **oai("x"))
    assert chat(env, {}).status == 200
    sent = env.fake.requests[0]["body"]
    assert "max_tokens" not in sent and "max_completion_tokens" not in sent
    env.fake.add("openai", **oai("x"))
    chat(env, {"max_tokens": 7}).read()
    assert env.fake.requests[1]["body"]["max_completion_tokens"] == 7


def error_frame_last(fr):
    assert fr[-1] == "[DONE]"  # the client does not hang
    err = fr[-2]
    assert set(err) == {"error"} and set(err["error"]) == {"message", "type", "param", "code"}
    return err["error"]


def test_stream_upstream_error_status_is_openai_error_frame(env):
    env.fake.add("openai", status=400, json={"error": {"message": "context too long"}})
    e = error_frame_last(sse_frames(chat(env, {"stream": True}, stream=True)))
    assert e["type"] == "invalid_request_error" and e["message"] == "context too long"


def test_stream_mid_stream_error_is_openai_error_frame(env):
    env.fake.add("openai", sse=[chunk({"content": "par"}),
                                {"error": {"message": "engine died", "type": "server_error", "code": 500}}])
    fr = sse_frames(chat(env, {"stream": True}, stream=True))
    e = error_frame_last(fr)
    assert e["message"] == "engine died"
    assert fr[1]["choices"][0]["delta"] == {"content": "par"} or fr[0]["choices"][0]["delta"].get("content") == "par"
    assert not any(isinstance(c, dict) and c.get("choices") and c["choices"][0].get("finish_reason") for c in fr)


def test_stream_cut_is_openai_error_frame(env):
    env.fake.add("openai", sse=[chunk({"content": "half"})])
    e = error_frame_last(sse_frames(chat(env, {"stream": True}, stream=True)))
    assert e["message"] == "upstream stream ended unexpectedly"


def test_stream_exception_is_openai_error_frame(env):
    for _ in range(4):
        env.fake.add("openai", status=429, headers={"Retry-After": "120"}, json={"error": {"message": "slow"}})
    e = error_frame_last(sse_frames(chat(env, {"stream": True}, stream=True)))
    assert e["type"] == "rate_limit_error" and e["code"] == "rate_limit_exceeded"


def test_stream_two_tool_calls_indexes_and_ids(env):
    def tc(i, **f):
        return chunk({"tool_calls": [{"index": i, **f}]})
    env.fake.add("openai", sse=[
        tc(0, id="call_a", type="function", function={"name": "f", "arguments": '{"a"'}),
        tc(0, function={"arguments": ":1}"}),
        tc(1, id="call_b", type="function", function={"name": "g", "arguments": '{"b"'}),
        tc(1, function={"arguments": ":2}"}),
        chunk({}, finish="tool_calls"), "[DONE]"])
    fr = sse_frames(chat(env, {"stream": True}, stream=True))
    calls = [c["choices"][0]["delta"]["tool_calls"][0] for c in fr[:-1] if "tool_calls" in c["choices"][0]["delta"]]
    assert all(c["id"] and c["type"] == "function" and "index" in c for c in calls)
    assert {c["index"] for c in calls} == {0, 1}
    ids = {c["index"]: {x["id"] for x in calls if x["index"] == c["index"]} for c in calls}
    assert len(ids[0]) == 1 and len(ids[1]) == 1 and ids[0] != ids[1]
    args = {i: "".join(c["function"].get("arguments", "") for c in calls if c["index"] == i) for i in (0, 1)}
    assert args == {0: '{"a":1}', 1: '{"b":2}'}
    assert fr[-2]["choices"][0]["finish_reason"] == "tool_calls"
