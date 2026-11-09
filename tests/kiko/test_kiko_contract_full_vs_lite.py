# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""
Contract tests: the SAME POST /v1/messages request goes to kiko-full (litellm/proxy/kiko_server.py, in-process
ASGI) and to kiko-lite (kiko_lite/, real socket); both talk to ONE local fake OpenAI-compatible upstream
(kikolib.server on 127.0.0.1:0), so there is no external network.  The responses are normalised (ids, timestamps,
cost and free-text error messages are ignored) and compared: HTTP status, error type, content blocks, stop_reason,
usage fields and the order of SSE event types.

Where the two differ ON PURPOSE or by a known gap the case is marked xfail(strict=True) with the reason; when a
gap is fixed the strict xfail turns into a failure, which is the signal to drop the marker.
"""
import json
import threading

import httpx
import pytest

from kiko_lite import app as lite
from kikolib import http as khttp
from kikolib import retry, server, sse

pytestmark = [pytest.mark.asyncio(loop_scope="session")]

MASTER = "sk-contract-test"
FULL_MODEL = LITE_MODEL = "contract-model"
TOOLS = [{"name": "get_weather", "description": "Get weather for a city.",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]


# ---------------------------------------------------------------------------
# fake upstream: sticky scripted OpenAI chat.completions answer, every request recorded
# ---------------------------------------------------------------------------

class FakeUpstream:
    def __init__(self):
        self.item: dict = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        app = server.App()
        app.route("POST", "/v1/chat/completions")(self._play)
        self.server = server.Server(app, port=0).start()

    @property
    def url(self) -> str:
        return self.server.url

    def set(self, **item) -> None:
        with self.lock:
            self.item = item
            self.requests.clear()

    def _play(self, req):
        with self.lock:
            self.requests.append(req.json())
            item = dict(self.item)
        if "sse" in item:
            frames = [f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n" for f in item["sse"]]
            return server.StreamResponse(iter(frames), item.get("status", 200))
        return server.JsonResponse(item.get("json", {}), item.get("status", 200), item.get("headers"))


def _chat(content="Hello from upstream", finish="stop", usage=None, **msg):
    return {"json": {"id": "chatcmpl-x", "object": "chat.completion", "created": 1700000000, "model": "gpt-x",
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": content, **msg},
                                  "finish_reason": finish}],
                     "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}}


def _tool_chat():
    return _chat(None, "tool_calls", {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
                 tool_calls=[{"id": "call_1", "type": "function",
                              "function": {"name": "get_weather", "arguments": '{"city":"Sofia"}'}}])


def _delta(delta, finish=None, usage=None):
    c = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "model": "gpt-x",
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


STREAM_TEXT = {"sse": [_delta({"role": "assistant", "content": "Hello"}), _delta({"content": " world"}),
                       _delta({}, "stop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
                       "[DONE]"]}


# ---------------------------------------------------------------------------
# the two servers
# ---------------------------------------------------------------------------

class Pair:
    """post(case-independent body, headers) -> normalised result for each implementation."""

    def __init__(self, fake, full_app, lite_url, lite_client):
        self.fake, self.full_app, self.lite_url, self.lite_client = fake, full_app, lite_url, lite_client

    async def full(self, body, key=MASTER, raw=None, path="/v1/messages"):
        h = {"content-type": "application/json"}
        if key is not None:
            h["authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.full_app, raise_app_exceptions=False), base_url="http://test") as c:
            r = await c.post(path, content=raw if raw is not None else json.dumps(body), headers=h)
        return Result(r.status_code, r.headers.get("content-type", ""), r.content)

    async def lite(self, body, key=MASTER, raw=None):
        h = {"content-type": "application/json"}
        if key is not None:
            h["x-api-key"] = key
        r = self.lite_client.request("POST", self.lite_url + "/v1/messages", h,
                                     raw if raw is not None else json.dumps(body).encode())
        with r:
            data = r.read()
        return Result(r.status, r.headers.get("content-type", ""), data)


class Result:
    def __init__(self, status, ctype, data):
        self.status, self.ctype, self.data = status, ctype, data

    def json(self):
        return json.loads(self.data)

    def events(self):
        out = []
        for ev in sse.iter_events(iter(self.data.decode("utf-8").splitlines(keepends=True))):
            if ev.data and ev.data != "[DONE]":
                out.append((ev.event, json.loads(ev.data)))
        return out


@pytest.fixture
def pair(monkeypatch, tmp_path):

    import litellm
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db

    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    fake = FakeUpstream()

    # --- full ---
    db = tmp_path / "contract.db"
    _init_db(db)
    monkeypatch.setenv("MASTER_KEY", MASTER)
    monkeypatch.setenv("KIKO_DB_PATH", str(db))
    monkeypatch.setattr(ks, "_billing_db_path", None)
    monkeypatch.setattr(ks, "_model_list", [{
        "model_name": FULL_MODEL,
        "litellm_params": {"model": "openai/gpt-x", "api_key": "sk-upstream", "api_base": fake.url + "/v1",
                         "max_retries": 0}}])
    monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
    for name in ("callbacks", "success_callback", "failure_callback", "input_callback"):
        monkeypatch.setattr(litellm, name, [])  # no billing logger: this is a wire contract test
    ks._cached_app = None
    full_app = ks.app

    # --- lite ---
    table = {"fake": {"base_url": fake.url + "/v1", "kind": "openai", "key_names": ["FAKE_KEY"], "auth": "bearer"}}
    resolver = lite.SimpleResolver(aliases={LITE_MODEL: "fake/gpt-x"}, env={}, providers=table)
    policy = retry.RetryPolicy(attempts=2, base_delay=0.02, max_delay=0.05, budget=3.0, jitter=False)
    lite_app = lite.build_app(resolver, MASTER, providers=table, policy=policy,
                              secret={"FAKE_KEY": "sk-upstream"}.get, ping_interval=0.2, upstream_timeout=5.0)
    srv = server.Server(lite_app, port=0).start()
    client = khttp.StdlibBackend()

    yield Pair(fake, full_app, srv.url, client)

    client.close()
    srv.stop()
    fake.server.stop()
    ks._cached_app = None


def req(**kw):
    return {"model": FULL_MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}], **kw}


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def norm_block(b):
    out = {k: v for k, v in b.items() if k not in ("id", "signature", "citations")}
    return out


def norm_message(d):
    return {
        "type": d.get("type"),
        "role": d.get("role"),
        "content": [norm_block(b) for b in d.get("content", [])],
        "stop_reason": d.get("stop_reason"),
        "stop_sequence": d.get("stop_sequence"),
        "usage_keys": sorted(k for k in d.get("usage", {}) if k in ("input_tokens", "output_tokens")),
        "input_tokens": d.get("usage", {}).get("input_tokens"),
        "output_tokens": d.get("usage", {}).get("output_tokens"),
    }


def norm_error(r):
    try:
        d = r.json()
    except ValueError:  # e.g. a plain-text "Internal Server Error"
        return {"status": r.status, "type": None, "error_type": None, "has_message": False}
    return {"status": r.status, "type": d.get("type"), "error_type": (d.get("error") or {}).get("type"),
            "has_message": bool((d.get("error") or {}).get("message"))}


def norm_stream(r):
    evs = r.events()
    text = "".join(d["delta"]["text"] for n, d in evs
                   if n == "content_block_delta" and d["delta"].get("type") == "text_delta")
    partial = "".join(d["delta"]["partial_json"] for n, d in evs
                      if n == "content_block_delta" and d["delta"].get("type") == "input_json_delta")
    starts = [d["content_block"]["type"] for n, d in evs if n == "content_block_start"]
    md = next((d for n, d in evs if n == "message_delta"), {})
    return {
        "status": r.status,
        "events": [n for n, _ in evs if n != "ping"],
        "block_types": starts,
        "text": text,
        "tool_json": json.loads(partial) if partial else None,
        "stop_reason": md.get("delta", {}).get("stop_reason"),
        "usage_keys": sorted(k for k in md.get("usage", {}) if k in ("input_tokens", "output_tokens")),
        "output_tokens": md.get("usage", {}).get("output_tokens"),
    }


async def both(pair, fake_item, body, **kw):
    pair.fake.set(**fake_item)
    f = await pair.full(body, **kw)
    lt = await pair.lite(body, **kw)
    return f, lt


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

async def test_nonstream_text(pair):
    f, lt = await both(pair, _chat(), req())
    assert (f.status, lt.status) == (200, 200), (f.data, lt.data)
    nf, nl = norm_message(f.json()), norm_message(lt.json())
    assert nf == nl
    assert nf["content"] == [{"type": "text", "text": "Hello from upstream"}]
    assert nf["stop_reason"] == "end_turn" and nf["role"] == "assistant" and nf["type"] == "message"


async def test_usage_token_fields(pair):
    f, lt = await both(pair, _chat(usage={"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}), req())
    for r in (f, lt):
        u = r.json()["usage"]
        assert u["input_tokens"] == 123 and u["output_tokens"] == 45, u


async def test_nonstream_tool_use(pair):
    f, lt = await both(pair, _tool_chat(), req(tools=TOOLS))
    assert norm_message(f.json()) == norm_message(lt.json())
    n = norm_message(lt.json())
    assert n["stop_reason"] == "tool_use"
    assert n["content"] == [{"type": "tool_use", "name": "get_weather", "input": {"city": "Sofia"}}]
    for r in (f, lt):  # the tool id is opaque (ignored in the comparison) but must exist
        assert r.json()["content"][0]["id"]


async def test_tool_round_trip_request_reaches_upstream_alike(pair):
    """2nd turn with tool_use + tool_result: both must send an OpenAI tool message upstream."""
    body = req(tools=TOOLS, messages=[
        {"role": "user", "content": "weather in Sofia?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01", "name": "get_weather",
                                           "input": {"city": "Sofia"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "sunny"}]}])
    f, lt = await both(pair, _chat("It is sunny."), body)
    assert norm_message(f.json()) == norm_message(lt.json())

    def roles(up):  # upstream request of the last call
        return [(m["role"], bool(m.get("tool_calls")), m.get("content")) for m in up["messages"]]
    pair.fake.set(**_chat("It is sunny."))
    # re-send to capture one upstream body per implementation
    await pair.full(body)
    up_full = pair.fake.requests[-1]
    await pair.lite(body)
    up_lite = pair.fake.requests[-1]
    assert [m["role"] for m in up_full["messages"]] == [m["role"] for m in up_lite["messages"]] == \
        ["user", "assistant", "tool"]
    assert up_full["messages"][2]["content"] == up_lite["messages"][2]["content"] == "sunny"
    assert up_full["messages"][1]["tool_calls"][0]["function"] == up_lite["messages"][1]["tool_calls"][0]["function"]
    assert roles(up_full)[1][1] and roles(up_lite)[1][1]


async def test_stream_text(pair):
    body = req(stream=True)
    pair.fake.set(**STREAM_TEXT)
    f, lt = await pair.full(body), await pair.lite(body)
    nf, nl = norm_stream(f), norm_stream(lt)
    assert nf == nl, (nf, nl)
    assert nl["events"] == ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                            "content_block_stop", "message_delta", "message_stop"]
    assert nl["text"] == "Hello world" and nl["stop_reason"] == "end_turn"
    for r in (f, lt):
        assert "text/event-stream" in r.ctype


async def test_stream_tool_use(pair):
    sse_tool = {"sse": [
        _delta({"role": "assistant", "content": ""}),
        _delta({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "get_weather", "arguments": ""}}]}),
        _delta({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
        _delta({"tool_calls": [{"index": 0, "function": {"arguments": ' "Sofia"}'}}]}),
        _delta({}, "tool_calls", {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}),
        "[DONE]"]}
    body = req(stream=True, tools=TOOLS)
    pair.fake.set(**sse_tool)
    f, lt = await pair.full(body), await pair.lite(body)
    nf, nl = norm_stream(f), norm_stream(lt)
    assert nl["block_types"] == ["tool_use"] and nl["tool_json"] == {"city": "Sofia"}
    assert nl["stop_reason"] == "tool_use"
    assert nf == nl, (nf, nl)


@pytest.mark.parametrize("up_status,hdr", [
    (429, {"retry-after": "7"}),
    (500, None),
])
async def test_upstream_error_status_and_type(pair, up_status, hdr):
    err = {"json": {"error": {"message": "boom", "type": "x"}}, "status": up_status, "headers": hdr}
    f, lt = await both(pair, err, req())
    nf, nl = norm_error(f), norm_error(lt)
    assert nf["type"] == nl["type"] == "error"
    assert nf["error_type"] == nl["error_type"], (nf, nl)
    assert nf["status"] == nl["status"], (nf, nl)
    if up_status == 429:
        assert nl["status"] == 429 and nl["error_type"] == "rate_limit_error"


async def test_upstream_429_keeps_retry_after(pair):
    err = {"json": {"error": {"message": "slow down"}}, "status": 429, "headers": {"retry-after": "7"}}
    pair.fake.set(**err)
    body = req()
    h = {"content-type": "application/json", "authorization": f"Bearer {MASTER}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.full_app, raise_app_exceptions=False), base_url="http://test") as c:
        rf = await c.post("/v1/messages", content=json.dumps(body), headers=h)
    rl = pair.lite_client.request("POST", pair.lite_url + "/v1/messages", {"x-api-key": MASTER, "content-type": "application/json"},
                                 json.dumps(body).encode())
    with rl:
        rl.read()
    assert rf.headers.get("retry-after") == "7"
    assert rl.headers.get("retry-after") == "7"


async def _bad_request(pair, body=None, raw=None):
    pair.fake.set(**_chat())
    f, lt = await pair.full(body, raw=raw), await pair.lite(body, raw=raw)
    nf, nl = norm_error(f), norm_error(lt)
    assert nl == {"status": 400, "type": "error", "error_type": "invalid_request_error", "has_message": True}
    assert nf == nl, (nf, nl)
    assert pair.fake.requests == []


async def test_bad_request_missing_model(pair):
    await _bad_request(pair, {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5})


async def test_bad_request_missing_messages(pair):
    await _bad_request(pair, {"model": FULL_MODEL, "max_tokens": 5})


async def test_bad_request_not_json(pair):
    await _bad_request(pair, raw=b"{not json")


@pytest.mark.parametrize("raw,case", [
    (b"[]", "json array"),
    (b'"x"', "json string"),
])
async def test_bad_request_not_object(pair, raw, case):
    await _bad_request(pair, raw=raw)


@pytest.mark.parametrize("extra", [{"model": ""}, {"model": 123}, {"messages": "x"}])
async def test_bad_request_wrong_field_types(pair, extra):
    await _bad_request(pair, req(**extra))


async def test_responses_bad_request_and_upstream_502(pair):
    pair.fake.set(**_chat())
    r = await pair.full(None, raw=b"{not json", path="/v1/responses")
    assert norm_error(r) == {"status": 400, "type": "error", "error_type": "invalid_request_error",
                             "has_message": True}
    r = await pair.full({"input": "hi"}, path="/v1/responses")  # no model
    assert norm_error(r)["status"] == 400
    pair.fake.set(json={"error": {"message": "boom", "type": "x"}}, status=500)
    r = await pair.full({"model": FULL_MODEL, "input": "hi"}, path="/v1/responses")
    assert norm_error(r) == {"status": 502, "type": "error", "error_type": "api_error", "has_message": True}


async def _full_with_raising_call(pair, monkeypatch, exc):
    import litellm

    async def boom(**kw):
        raise exc

    monkeypatch.setattr(litellm, "anthropic_messages", boom)
    return norm_error(await pair.full(req()))


async def test_internal_error_stays_500_generic_exception(pair, monkeypatch):
    n = await _full_with_raising_call(pair, monkeypatch, RuntimeError("bug in transformation"))
    assert n["status"] == 500 and n["error_type"] == "api_error"


async def test_internal_error_stays_500_api_connection_error(pair, monkeypatch):
    import litellm
    exc = litellm.exceptions.APIConnectionError(message="catch-all", llm_provider="openai", model="gpt-x")
    n = await _full_with_raising_call(pair, monkeypatch, exc)
    assert n["status"] == 500 and n["error_type"] == "api_error"


async def test_internal_error_stays_500_wrapped_internal_server_error(pair, monkeypatch):
    # InternalServerError built without any upstream response (how a wrapped generic exception arrives)
    import litellm
    exc = litellm.exceptions.InternalServerError(message="wrapped", llm_provider="openai", model="gpt-x")
    n = await _full_with_raising_call(pair, monkeypatch, exc)
    assert n["status"] == 500 and n["error_type"] == "api_error"


async def test_auth_failure_wrong_key(pair):
    f, lt = await both(pair, _chat(), req(), key="wrong-key")
    nf, nl = norm_error(f), norm_error(lt)
    assert nl == {"status": 401, "type": "error", "error_type": "authentication_error", "has_message": True}
    assert nf == nl, (nf, nl)
    assert pair.fake.requests == []  # neither forwarded the request


async def test_auth_failure_missing_key(pair):
    f, lt = await both(pair, _chat(), req(), key=None)
    nf, nl = norm_error(f), norm_error(lt)
    assert nl["status"] == 401
    assert nf == nl, (nf, nl)


async def test_unknown_model(pair):
    f, lt = await both(pair, _chat(), req(model="no-such-model"))
    nf, nl = norm_error(f), norm_error(lt)
    assert nl == {"status": 404, "type": "error", "error_type": "not_found_error", "has_message": True}
    assert nf == nl, (nf, nl)
    assert pair.fake.requests == []


async def test_stop_reason_max_tokens(pair):
    f, lt = await both(pair, _chat("cut", "length"), req())
    assert norm_message(f.json()) == norm_message(lt.json())
    assert lt.json()["stop_reason"] == "max_tokens"


@pytest.mark.xfail(strict=True, reason=(
    "DRIFT: tool_use id -> full passes the upstream id through ('call_1'); lite prefixes it ('toolu_call_1', "
    "kiko_lite/providers/openai_compat.py from_openai). Claude Code accepts both; ids are ignored elsewhere"))
async def test_tool_use_id_prefix(pair):
    f, lt = await both(pair, _tool_chat(), req(tools=TOOLS))
    assert f.json()["content"][0]["id"].startswith("toolu_")
    assert lt.json()["content"][0]["id"].startswith("toolu_")
