# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite app end to end, fully offline: a fake upstream (kikolib.server App on 127.0.0.1:0) plays scripted
OpenAI and Gemini responses; the client is kikolib.http against the kiko_lite app."""
import json
import threading
import time

import pytest

from kiko_lite import app as lite
from kikolib import http as khttp
from kikolib import retry, server, sse

MASTER = "master-key"
UPSTREAM_KEY = "UPSTREAM-SECRET-123"


# ---- fake upstream ----

class FakeUpstream:
    """Scripted responses per kind ("openai" | "gemini"); every request is recorded."""

    def __init__(self):
        self.script = {"openai": [], "gemini": []}
        self.requests = []
        self.lock = threading.Lock()
        self.release = threading.Event()  # ends "stall" / "endless" scripts (set on teardown)
        self.closed = threading.Event()  # set when an "endless" stream was dropped by its client
        app = server.App()
        app.route("POST", "/v1/chat/completions")(lambda req: self._play("openai", req))
        app.route("POST", "/v1beta/models/{mo}")(lambda req: self._play("gemini", req))
        self.server = server.Server(app, port=0).start()

    @property
    def url(self):
        return self.server.url

    def add(self, kind, **item):
        self.script[kind].append(item)

    def _stall(self, frames):
        yield from frames
        self.release.wait(10)

    def _endless(self):
        try:
            while not self.release.is_set():
                yield 'data: {"choices":[{"index":0,"delta":{"content":"."},"finish_reason":null}]}\n\n'
                time.sleep(0.05)
        finally:
            self.closed.set()

    def _play(self, kind, req):
        with self.lock:
            self.requests.append({"kind": kind, "path": req.path, "query": req.query,
                                  "headers": dict(req.headers), "body": req.json()})
            item = self.script[kind].pop(0) if self.script[kind] else {"status": 500, "json": {"error": "empty script"}}
        if "raw" in item:  # a body that is not valid JSON (truncated)
            return server.StreamResponse(iter([item["raw"]]), item.get("status", 200), content_type="application/json")
        if item.get("endless"):
            return server.StreamResponse(self._endless())
        if "sse" in item:
            frames = [f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n" for f in item["sse"]]
            if item.get("stall"):  # headers and the frames go out, then the body stops
                frames = self._stall(frames)
            return server.StreamResponse(iter(frames), item.get("status", 200))
        return server.JsonResponse(item.get("json", {}), item.get("status", 200), item.get("headers"))


@pytest.fixture
def env(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    fake = FakeUpstream()
    table = {
        "fake": {"base_url": fake.url + "/v1", "kind": "openai", "key_names": ["FAKE_KEY"], "auth": "bearer",
                 "flags": {"max_tokens_field": "max_completion_tokens"}},
        "fakeg": {"base_url": fake.url, "kind": "gemini", "key_names": ["FAKEG_KEY"], "auth": "header:x-goog-api-key"},
        "capped": {"base_url": fake.url + "/v1", "kind": "openai", "key_names": ["FAKE_KEY"],
                   "flags": {"max_tokens_cap": 10}},
        "nokey": {"base_url": fake.url + "/v1", "kind": "openai", "key_names": ["NOKEY_KEY"]},
    }
    resolver = lite.SimpleResolver(aliases={"claude-sonnet-*": "fake/model-s"}, classes={"haiku": "fake/model-h"},
                                   env={}, providers=table)
    results = []
    policy = retry.RetryPolicy(attempts=4, base_delay=0.02, max_delay=0.05, budget=5.0, jitter=False)
    secrets = {"FAKE_KEY": UPSTREAM_KEY, "FAKEG_KEY": UPSTREAM_KEY}
    app = lite.build_app(resolver, MASTER, providers=table, policy=policy, secret=secrets.get,
                         on_result=results.append, ping_interval=0.2, upstream_timeout=1.0)
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()

    class Env:
        pass

    e = Env()
    e.fake, e.results, e.url, e.client, e.app = fake, results, srv.url, client, app

    def post(path, body, headers=None, stream=False):
        h = {"x-api-key": MASTER, "content-type": "application/json", **(headers or {})}
        return client.request("POST", srv.url + path, h, json.dumps(body).encode(), stream=stream)

    def events(body):
        r = post("/v1/messages", {**body, "stream": True}, stream=True)
        assert r.status == 200
        with r:
            return [(ev.event, json.loads(ev.data)) for ev in sse.iter_events(r.iter_lines())]

    e.post, e.events = post, events
    yield e
    fake.release.set()
    client.close()
    srv.stop()
    fake.server.stop()


def req(**kw):
    return {"model": "fake/model-x", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}], **kw}


def oai(content="hello", finish="stop", usage=None, **msg):
    return {"json": {"choices": [{"message": {"content": content, **msg}, "finish_reason": finish}],
                     "usage": usage or {"prompt_tokens": 11, "completion_tokens": 3}}}


def chunk(delta=None, finish=None, usage=None):
    c = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if delta is not None else []}
    if usage:
        c["usage"] = usage
    return c


def names(evs):
    return [n for n, _ in evs]


def text_of(evs, typ="text_delta", key="text"):
    return "".join(d["delta"][key] for n, d in evs if n == "content_block_delta" and d["delta"]["type"] == typ)


# ---- basics: auth, health, errors ----

def test_health_needs_no_key(env):
    r = env.client.request("GET", env.url + "/health")
    assert r.status == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{"x-api-key": "wrong"}, {"x-api-key": "", "authorization": "Bearer nope"}, {}])
def test_auth_401_anthropic_shape(env, headers):
    r = env.client.request("POST", env.url + "/v1/messages", {"content-type": "application/json", **headers},
                           json.dumps(req()).encode())
    assert r.status == 401
    assert r.json() == {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    assert env.fake.requests == []


def test_bearer_auth_accepted(env):
    env.fake.add("openai", **oai())
    r = env.client.request("POST", env.url + "/v1/messages", {"authorization": f"Bearer {MASTER}", "content-type": "application/json"},
                           json.dumps(req()).encode())
    assert r.status == 200


def test_empty_master_key_refused():
    with pytest.raises(ValueError):
        lite.build_app(lambda m: None, "")


def test_unknown_model_is_anthropic_404(env):
    r = env.post("/v1/messages", req(model="no-such-model"))
    assert r.status == 404
    assert r.json()["error"]["type"] == "not_found_error" and "no-such-model" in r.json()["error"]["message"]
    assert env.fake.requests == []


def test_bad_request_bodies(env):
    assert env.post("/v1/messages", {"messages": []}).status == 400
    assert env.post("/v1/messages", {"model": "fake/m"}).status == 400
    r = env.client.request("POST", env.url + "/v1/messages", {"x-api-key": MASTER, "content-type": "application/json"}, b"{not json")
    assert r.status == 400 and r.json()["error"]["type"] == "invalid_request_error"


def test_missing_upstream_key_503_without_secrets(env):
    r = env.post("/v1/messages", req(model="nokey/m"))
    assert r.status == 503 and "nokey" in r.json()["error"]["message"]


def test_count_tokens(env):
    r = env.post("/v1/messages/count_tokens", req(system="be brief", tools=[
        {"name": "f", "description": "d", "input_schema": {"type": "object"}}]))
    assert r.status == 200 and r.json()["input_tokens"] > 5
    assert env.post("/v1/messages/count_tokens", {"model": "x"}).status == 400
    assert env.fake.requests == []


# ---- non-stream ----

def test_nonstream_text(env):
    env.fake.add("openai", **oai("hello"))
    body = req(system="sys", stop_sequences=["X"], api_base="http://evil", extra_headers={"x": "y"},
               metadata={"user_id": "u"}, temperature=0.5)
    r = env.post("/v1/messages", body)
    assert r.status == 200
    msg = r.json()
    assert msg["type"] == "message" and msg["role"] == "assistant" and msg["model"] == "fake/model-x"
    assert msg["content"] == [{"type": "text", "text": "hello"}] and msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {"input_tokens": 11, "output_tokens": 3}
    up = env.fake.requests[0]
    assert up["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert "x-api-key" not in up["headers"]
    assert up["body"]["model"] == "model-x" and up["body"]["max_completion_tokens"] == 64
    assert up["body"]["messages"][0] == {"role": "system", "content": "sys"} and up["body"]["stop"] == ["X"]
    for leaked in ("api_base", "extra_headers", "metadata", "stream"):
        assert leaked not in up["body"]
    res = env.results[0]
    assert (res.provider, res.upstream_model, res.attempts, res.status, res.stream) == ("fake", "model-x", 1, 200, False)
    assert res.usage == {"input_tokens": 11, "output_tokens": 3} and res.latency > 0 and res.stop_reason == "end_turn"


def test_alias_class_resolution_end_to_end(env):
    env.fake.add("openai", **oai())
    env.fake.add("openai", **oai())
    env.post("/v1/messages", req(model="claude-sonnet-4-5"))
    env.post("/v1/messages", req(model="claude-3-5-haiku-latest"))
    assert [r["body"]["model"] for r in env.fake.requests] == ["model-s", "model-h"]


def test_nonstream_reasoning_fields_and_think_tags(env):
    env.fake.add("openai", **oai("answer", reasoning_content="because"))
    env.fake.add("openai", **oai("<think>why</think>\nanswer"))
    for want in ("because", "why"):
        c = env.post("/v1/messages", req()).json()["content"]
        assert c == [{"type": "thinking", "thinking": want, "signature": ""}, {"type": "text", "text": "answer"}]


def test_nonstream_usage_fallback_estimate(env):
    env.fake.add("openai", json={"choices": [{"message": {"content": "some answer text"}, "finish_reason": "stop"}]})
    u = env.post("/v1/messages", req()).json()["usage"]
    assert u["input_tokens"] > 0 and u["output_tokens"] > 0


def test_tool_use_round_trip_nonstream(env):
    tools = [{"name": "Bash", "description": "run", "input_schema": {"type": "object",
                                                                    "properties": {"cmd": {"type": "string"}}}}]
    env.fake.add("openai", json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "call_77", "type": "function", "function": {"name": "Bash", "arguments": '{"cmd": "ls"}'}}]}}]})
    env.fake.add("openai", **oai("done"))
    first = env.post("/v1/messages", req(tools=tools, tool_choice={"type": "any"})).json()
    assert first["stop_reason"] == "tool_use"
    use = first["content"][0]
    assert use == {"type": "tool_use", "id": "toolu_call_77", "name": "Bash", "input": {"cmd": "ls"}}
    up1 = env.fake.requests[0]["body"]
    assert up1["tool_choice"] == "required" and up1["tools"][0]["function"]["parameters"]["type"] == "object"

    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": [use]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "a.txt"}]}]
    assert env.post("/v1/messages", req(messages=msgs, tools=tools)).json()["content"][0]["text"] == "done"
    sent = env.fake.requests[1]["body"]["messages"]
    assert sent[1]["tool_calls"][0]["id"] == "call_77"
    assert sent[2] == {"role": "tool", "tool_call_id": "call_77", "content": "a.txt"}


@pytest.mark.parametrize("choice,expected", [
    ({"type": "auto"}, "auto"), ({"type": "none"}, "none"),
    ({"type": "tool", "name": "Bash"}, {"type": "function", "function": {"name": "Bash"}})])
def test_tool_choice_mapping_via_app(env, choice, expected):
    env.fake.add("openai", **oai())
    env.post("/v1/messages", req(tool_choice=choice, tools=[
        {"name": "Bash", "input_schema": {"type": "object", "properties": {}}}]))
    assert env.fake.requests[0]["body"]["tool_choice"] == expected


# ---- non-stream errors and retry ----

def test_retry_503_twice_then_ok(env):
    env.fake.add("openai", status=503, json={"error": {"message": "busy"}})
    env.fake.add("openai", status=503, json={"error": {"message": "busy"}})
    env.fake.add("openai", **oai("finally"))
    r = env.post("/v1/messages", req())
    assert r.status == 200 and r.json()["content"][0]["text"] == "finally"
    assert len(env.fake.requests) == 3 and env.results[0].attempts == 3


def test_429_gives_up_with_retry_after(env):
    for _ in range(4):
        env.fake.add("openai", status=429, headers={"Retry-After": "120"}, json={"error": {"message": "slow down"}})
    r = env.post("/v1/messages", req())
    assert r.status == 429 and r.headers.get("Retry-After") == "120"
    assert r.json() == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    assert len(env.fake.requests) == 1  # Retry-After beyond the budget: no pointless waiting
    assert env.results[0].error == "rate_limit_error" and env.results[0].status == 429


def test_503_exhausted_is_529_with_retry_after(env):
    for _ in range(4):
        env.fake.add("openai", status=503, json={"error": {"message": "down"}})
    r = env.post("/v1/messages", req())
    assert r.status == 529 and r.json()["error"]["type"] == "overloaded_error"
    assert r.headers.get("Retry-After") and env.results[0].attempts == 4


def test_upstream_400_passes_message_and_scrubs_key(env):
    env.fake.add("openai", status=400, json={"error": {"message": f"bad param (key {UPSTREAM_KEY})"}})
    r = env.post("/v1/messages", req())
    assert r.status == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert "bad param" in r.json()["error"]["message"] and UPSTREAM_KEY not in r.read().decode()
    assert len(env.fake.requests) == 1  # not retryable


def test_upstream_401_is_502_generic(env):
    env.fake.add("openai", status=401, json={"error": {"message": "Incorrect API key sk-abc***"}})
    r = env.post("/v1/messages", req())
    assert r.status == 502 and r.json()["error"]["type"] == "api_error"
    assert "sk-abc" not in r.read().decode()


def test_upstream_unreachable_is_529(env):
    env.fake.server.request_stop()
    env.fake.server.close()  # the listening socket is gone: connection refused
    r = env.post("/v1/messages", req())
    assert r.status == 529 and r.headers.get("Retry-After")


# ---- stream ----

def test_stream_text(env):
    env.fake.add("openai", sse=[chunk({"role": "assistant", "content": ""}), chunk({"content": "Hel"}),
                                chunk({"content": "lo"}), chunk({}, "stop"),
                                chunk(usage={"prompt_tokens": 8, "completion_tokens": 2}), "[DONE]"])
    evs = env.events(req())
    assert names(evs)[0] == "ping"
    body = evs[1:]
    assert names(body) == ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                           "content_block_stop", "message_delta", "message_stop"]
    assert text_of(body) == "Hello"
    assert body[0][1]["message"]["model"] == "fake/model-x"
    assert body[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert body[-2][1]["usage"] == {"input_tokens": 8, "output_tokens": 2}
    up = env.fake.requests[0]["body"]
    assert up["stream"] is True and up["stream_options"] == {"include_usage": True}
    res = env.results[0]
    assert res.stream and res.usage == {"input_tokens": 8, "output_tokens": 2} and res.attempts == 1


def test_stream_tool_use_split_chunks_and_reasoning(env):
    env.fake.add("openai", sse=[
        chunk({"reasoning_content": "hmm"}),
        chunk({"content": "I will run it"}),
        chunk({"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                               "function": {"name": "Bash", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd":'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}),
        chunk({}, "tool_calls"), chunk(usage={"prompt_tokens": 5, "completion_tokens": 6}), "[DONE]"])
    evs = env.events(req())[1:]
    kinds = [d["content_block"]["type"] for n, d in evs if n == "content_block_start"]
    assert kinds == ["thinking", "text", "tool_use"]
    assert text_of(evs, "thinking_delta", "thinking") == "hmm"
    tool = [d for n, d in evs if n == "content_block_start"][2]["content_block"]
    assert tool["id"] == "toolu_call_9" and tool["name"] == "Bash"
    assert json.loads(text_of(evs, "input_json_delta", "partial_json")) == {"cmd": "ls"}
    assert evs[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_stream_think_tags(env):
    env.fake.add("openai", sse=[chunk({"content": "<think>pl"}), chunk({"content": "an</think>\n\nok"}),
                                chunk({}, "stop"), "[DONE]"])
    evs = env.events(req())[1:]
    assert text_of(evs, "thinking_delta", "thinking") == "plan" and text_of(evs) == "ok"


def test_stream_ping_while_retrying(env):
    env.fake.add("openai", status=503, json={"error": {"message": "busy"}})
    env.fake.add("openai", status=503, json={"error": {"message": "busy"}})
    env.fake.add("openai", sse=[chunk({"content": "late"}), chunk({}, "stop"), "[DONE]"])
    evs = env.events(req())
    ns = names(evs)
    first_real = ns.index("message_start")
    assert ns[:first_real].count("ping") >= 3  # one at once, one per retry wait
    assert text_of(evs) == "late" and ns[-1] == "message_stop"
    assert env.results[0].attempts == 3


def test_stream_gives_up_with_error_event(env):
    for _ in range(4):
        env.fake.add("openai", status=429, headers={"Retry-After": "120"}, json={"error": {"message": "slow"}})
    evs = env.events(req())
    assert names(evs) == ["ping", "error"]
    assert evs[1][1] == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    assert env.results[0].error == "rate_limit_error"


def test_stream_mid_stream_error_event(env):
    env.fake.add("openai", sse=[chunk({"content": "par"}),
                                {"error": {"message": "engine died", "type": "server_error", "code": 500}}])
    evs = env.events(req())
    assert text_of(evs) == "par"
    assert names(evs)[-1] == "error" and "message_stop" not in names(evs)
    assert evs[-1][1]["error"] == {"type": "api_error", "message": "engine died"}


def test_stream_upstream_400_is_error_event(env):
    env.fake.add("openai", status=400, json={"error": {"message": "context too long"}})
    evs = env.events(req())
    assert evs[-1][1]["error"] == {"type": "invalid_request_error", "message": "context too long"}


def test_stream_without_done_still_finishes(env):
    env.fake.add("openai", sse=[chunk({"content": "x"}), chunk({}, "length")])
    evs = env.events(req())
    assert names(evs)[-1] == "message_stop" and evs[-2][1]["delta"]["stop_reason"] == "max_tokens"


def test_stream_usage_fallback_estimate(env):
    env.fake.add("openai", sse=[chunk({"content": "some words here"}), chunk({}, "stop"), "[DONE]"])
    u = env.events(req())[-2][1]["usage"]
    assert u["input_tokens"] > 0 and u["output_tokens"] > 0


# ---- gemini kind ----

def test_gemini_nonstream_routed_to_native(env):
    env.fake.add("gemini", json={"candidates": [{"content": {"parts": [{"text": "bonjour"}]}, "finishReason": "STOP"}],
                                 "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}})
    r = env.post("/v1/messages", req(model="fakeg/gemini-2.5-flash", system="s", max_tokens=33))
    assert r.status == 200
    msg = r.json()
    assert msg["content"] == [{"type": "text", "text": "bonjour"}] and msg["usage"] == {"input_tokens": 4,
                                                                                       "output_tokens": 2}
    up = env.fake.requests[0]
    assert up["path"] == "/v1beta/models/gemini-2.5-flash:generateContent"
    assert up["headers"]["x-goog-api-key"] == UPSTREAM_KEY and "authorization" not in up["headers"]
    assert up["body"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert up["body"]["generationConfig"] == {"maxOutputTokens": 33}
    assert UPSTREAM_KEY not in json.dumps(up["query"])


def test_gemini_stream_and_tool_call(env):
    env.fake.add("gemini", sse=[
        {"candidates": [{"content": {"parts": [{"text": "let me"}]}}]},
        {"candidates": [{"content": {"parts": [{"functionCall": {"name": "Bash", "args": {"cmd": "ls"}}}]},
                         "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 3}}])
    evs = env.events(req(model="fakeg/gemini-2.5-flash"))
    up = env.fake.requests[0]
    assert up["path"].endswith(":streamGenerateContent") and up["query"] == {"alt": "sse"}
    assert text_of(evs) == "let me"
    starts = [d["content_block"] for n, d in evs if n == "content_block_start"]
    assert starts[1]["type"] == "tool_use" and starts[1]["name"] == "Bash" and starts[1]["id"].startswith("toolu_")
    assert json.loads(text_of(evs, "input_json_delta", "partial_json")) == {"cmd": "ls"}
    assert evs[-2][1]["delta"]["stop_reason"] == "tool_use" and evs[-2][1]["usage"]["input_tokens"] == 6


def test_gemini_stream_error_event(env):
    env.fake.add("gemini", sse=[{"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}])
    evs = env.events(req(model="fakeg/gemini-2.5-flash"))
    assert evs[-1][1]["error"] == {"type": "rate_limit_error", "message": "quota"}


# ---- resolver ----

def test_simple_resolver_order(monkeypatch):
    table = {"p": {"base_url": "x", "kind": "openai", "key_names": []}}
    env_vars = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "p/from-env", "ANTHROPIC_DEFAULT_OPUS_MODEL": "p/opus-env"}
    r = lite.SimpleResolver(aliases={"claude-sonnet-*": "p/from-alias"}, classes={"OPUS": "p/from-class"},
                            env=env_vars, providers=table)
    assert r("claude-sonnet-4") == ("p", "from-alias")  # alias before class and env
    assert r("claude-opus-4") == ("p", "from-class")  # class before env
    assert r("Claude-Sonnet") == ("p", "from-env")  # glob is case-sensitive; keyword match is not
    assert r("p/direct/model") == ("p", "direct/model")
    assert r("claude-haiku-4") is None  # no env var, not a provider/model name
    assert r("nope/model") is None


def test_sequential_requests_reuse_connections(env):
    for _ in range(3):
        env.fake.add("openai", **oai())
    assert [env.post("/v1/messages", req()).status for _ in range(3)] == [200, 200, 200]


# ---- review fixes: model id validation ----

@pytest.mark.parametrize("model", [
    "fakeg/../../v1beta/cachedContents?", "fakeg/foo?x=", "fakeg/foo#frag", "fakeg/%2e%2e", "fakeg/foo%3Fx",
    "fakeg/foo bar", "fakeg/a/b", "fake/../x", "fake/./x", "fake/a//b", "fake/foo?x=1", "fake/foo\nbar",
    "fake/", "fakeg/.."])
def test_hostile_model_ids_rejected_before_any_upstream_call(env, model):
    r = env.post("/v1/messages", req(model=model))
    assert r.status in (400, 404) and r.json()["type"] == "error"
    assert env.fake.requests == []


def test_resolver_returning_empty_model_id_is_400(env):
    app = lite.build_app(lambda m: ("fake", ""), MASTER, providers={"fake": {"base_url": "http://x", "kind": "openai",
                                                                            "key_names": ["K"]}}, secret=lambda n: "k")
    with pytest.raises(server.HTTPError) as ei:
        app.gateway._plan(req())
    assert ei.value.status == 400


def test_valid_model_id_function():
    ok = ["gemini-2.5-flash", "meta/llama-3.1-70b-instruct", "qwen3:32b", "a.b_c-d/e"]
    bad = ["", "..", "a/../b", "a b", "a?b", "a#b", "a%2eb", "/a", "a/", "a\b", "é"]
    assert all(lite.valid_model_id(m) for m in ok)
    assert not any(lite.valid_model_id(m) for m in bad)
    assert not lite.valid_model_id("a/b", "gemini") and lite.valid_model_id("a-b", "gemini")


def test_gemini_endpoint_quotes_model():
    from kiko_lite.providers import gemini as g
    url = g.endpoint("gemini-2.5-flash/../x?y#z", False)
    assert url.endswith(":generateContent") and "?" not in url and "#" not in url
    assert url.count("/") == g.BASE.count("/") + 3 and "%2F" in url


# ---- review fixes: no re-sending after the response head ----

def test_nonstream_body_stall_is_not_retried(env):
    env.fake.add("openai", sse=['{"choices": ['], stall=True)
    r = env.post("/v1/messages", req())
    assert r.status == 529 and r.json()["error"]["type"] == "overloaded_error"
    assert len(env.fake.requests) == 1 and env.results[0].attempts == 1


def test_nonstream_truncated_json_is_502_not_retried(env):
    env.fake.add("openai", raw='{"choices": [{"message": {"content": "x"')
    r = env.post("/v1/messages", req())
    assert r.status == 502 and r.json()["error"]["type"] == "api_error"
    assert len(env.fake.requests) == 1


# ---- review fixes: client disconnect ----

def _wait(cond, seconds=8.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_disconnect_during_backoff_stops_retrying(env):
    env.app.gateway.policy = retry.RetryPolicy(attempts=5, base_delay=0.6, max_delay=1.0, budget=30.0, jitter=False)
    for _ in range(5):
        env.fake.add("openai", status=503, json={"error": {"message": "busy"}})
    r = env.post("/v1/messages", req(stream=True), stream=True)
    assert r.status == 200
    lines = r.iter_lines()
    next(lines)  # first ping: the worker is in its first backoff by now
    assert _wait(lambda: len(env.fake.requests) >= 1)
    r.close()
    assert _wait(lambda: env.results, 8.0)  # the generator noticed and ended
    time.sleep(1.5)  # longer than a backoff: a live worker would send again
    assert len(env.fake.requests) == 1


def test_disconnect_mid_stream_closes_upstream(env):
    env.fake.add("openai", endless=True)
    r = env.post("/v1/messages", req(stream=True), stream=True)
    lines = r.iter_lines()
    seen = 0
    for line in lines:
        seen += line.startswith("data:") and "content_block_delta" in line
        if seen >= 2:
            break
    r.close()
    assert env.fake.closed.wait(8.0), "upstream connection was not closed after the client left"


# ---- review fixes: truncated stream, max_tokens cap ----

def test_stream_cut_without_done_or_finish_is_error_event(env):
    env.fake.add("openai", sse=[chunk({"content": "half"})])
    evs = env.events(req())
    assert text_of(evs) == "half" and "message_stop" not in names(evs)
    assert evs[-1][1]["error"] == {"type": "api_error", "message": "upstream stream ended unexpectedly"}
    assert env.results[0].error == "api_error"


def test_done_without_finish_reason_still_completes(env):
    env.fake.add("openai", sse=[chunk({"content": "ok"}), "[DONE]"])
    assert names(env.events(req()))[-1] == "message_stop"


def test_gemini_stream_cut_without_finish_is_error_event(env):
    env.fake.add("gemini", sse=[{"candidates": [{"content": {"parts": [{"text": "half"}]}}]}])
    evs = env.events(req(model="fakeg/gemini-2.5-flash"))
    assert evs[-1][1]["error"]["message"] == "upstream stream ended unexpectedly"


def test_max_tokens_cap_clamps(env):
    env.fake.add("openai", **oai())
    env.fake.add("openai", **oai())
    env.post("/v1/messages", req(model="capped/m", max_tokens=4096))
    env.post("/v1/messages", req(model="capped/m", max_tokens=5))
    assert [r["body"]["max_tokens"] for r in env.fake.requests] == [10, 5]
    env.fake.add("openai", **oai())
    env.post("/v1/messages", req(max_tokens=4096))  # no cap on "fake"
    assert env.fake.requests[2]["body"]["max_completion_tokens"] == 4096
