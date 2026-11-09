# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""
TASK #110 regression: kiko-full crashed on every request carrying `tools` because litellm/main.py imports
litellm_proxy_mcp_handler -> litellm.proxy._experimental.mcp_server.utils, which the kiko strip had removed.
The module is restored; these tests use the REAL module (no stub) and a local fake OpenAI-compatible upstream
(no external network, max_retries 0).
"""
import importlib
import json
import threading

import httpx
import pytest

from kikolib import server  # noqa: E402

pytestmark = [pytest.mark.asyncio(loop_scope="session")]

MASTER = "sk-tools-test"
MODEL = "tools-model"
GEM_MODEL = "tools-gemini"
OAI_TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get weather.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
ANT_TOOLS = [{"name": "get_weather", "description": "Get weather.",
              "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]


def _tool_json():
    return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1700000000, "model": "gpt-x",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"Sofia"}'}}]}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}}


def _chunk(delta, finish=None, usage=None):
    c = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "model": "gpt-x",
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


def _tool_sse():
    tc = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
    frames = [_chunk({"role": "assistant", "content": None, "tool_calls": [tc]}),
              _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Sofia"}'}}]}),
              _chunk({}, "tool_calls", {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}),
              "[DONE]"]
    return [f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n" for f in frames]


def _gemini_json():
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "get_weather", "args": {"city": "Sofia"}}}]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8}}


class Fake:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        app = server.App()
        app.route("POST", "/v1/chat/completions")(self._oai)
        app.route("POST", "/models/gemini-x:generateContent")(self._gem)
        app.route("POST", "/models/gemini-x:streamGenerateContent")(self._gem_stream)
        self.server = server.Server(app, port=0).start()

    def _oai(self, req):
        body = req.json()
        with self.lock:
            self.requests.append(body)
        if body.get("stream"):
            return server.StreamResponse(iter(_tool_sse()), 200)
        return server.JsonResponse(_tool_json(), 200)

    def _gem(self, req):
        with self.lock:
            self.requests.append(req.json())
        return server.JsonResponse(_gemini_json(), 200)

    def _gem_stream(self, req):
        with self.lock:
            self.requests.append(req.json())
        return server.StreamResponse(iter([f"data: {json.dumps(_gemini_json())}\n\n"]), 200)

    @property
    def url(self):
        return self.server.url


@pytest.fixture
def fake(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    f = Fake()
    yield f
    f.server.stop()


@pytest.fixture
def full_app(monkeypatch, tmp_path, fake):
    import litellm
    import litellm.proxy.kiko_server as ks
    from litellm.proxy.integrations.kiko_sqlite_logger import _init_db

    db = tmp_path / "tools.db"
    _init_db(db)
    monkeypatch.setenv("MASTER_KEY", MASTER)
    monkeypatch.setenv("KIKO_DB_PATH", str(db))
    monkeypatch.setattr(ks, "_billing_db_path", None)
    monkeypatch.setattr(ks, "_model_list", [{
        "model_name": MODEL,
        "litellm_params": {"model": "openai/gpt-x", "api_key": "sk-up", "api_base": fake.url + "/v1",
                           "max_retries": 0}},
        {"model_name": GEM_MODEL,
         "litellm_params": {"model": "gemini/gemini-x", "api_key": "sk-up", "api_base": fake.url, "max_retries": 0}}])
    monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
    for name in ("callbacks", "success_callback", "failure_callback", "input_callback"):
        monkeypatch.setattr(litellm, name, [])
    ks._cached_app = None
    yield ks.app
    ks._cached_app = None


async def _post(app, path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://test") as c:
        return await c.post(path, content=json.dumps(body),
                            headers={"content-type": "application/json", "authorization": f"Bearer {MASTER}"})


def _sse_events(text):
    out = []
    for block in text.split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data and data != "[DONE]":
            out.append((ev, json.loads(data)))
    return out


# ---------------------------------------------------------------------------
# import health
# ---------------------------------------------------------------------------

def test_import_health_mcp_handler():
    importlib.import_module("litellm.responses.mcp.litellm_proxy_mcp_handler")
    from litellm.proxy._experimental.mcp_server import utils
    assert hasattr(utils, "split_server_prefix_from_name") and hasattr(utils, "upstream_credential_headers")
    # the real module, not a test stub
    assert utils.__file__.replace("\\", "/").endswith("litellm/proxy/_experimental/mcp_server/utils.py")


# ---------------------------------------------------------------------------
# litellm.acompletion / Router / sync
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,kw", [("openai/gpt-x", "oai"), ("gemini/gemini-x", "gem")])
async def test_acompletion_tools_nonstream(fake, model, kw, monkeypatch):
    import litellm
    resp = await litellm.acompletion(
        model=model, messages=[{"role": "user", "content": "weather?"}], tools=OAI_TOOLS, api_key="sk-up",
        api_base=(fake.url + "/v1") if kw == "oai" else fake.url, max_retries=0)
    tc = resp.choices[0].message.tool_calls
    assert tc and tc[0].function.name == "get_weather" and json.loads(tc[0].function.arguments) == {"city": "Sofia"}


async def test_acompletion_tools_stream(fake):
    import litellm
    stream = await litellm.acompletion(
        model="openai/gpt-x", messages=[{"role": "user", "content": "w"}], tools=OAI_TOOLS, stream=True,
        api_key="sk-up", api_base=fake.url + "/v1", max_retries=0)
    names, args = [], ""
    async for ch in stream:
        for t in ch.choices[0].delta.tool_calls or []:
            if t.function.name:
                names.append(t.function.name)
            args += t.function.arguments or ""
    assert names == ["get_weather"] and json.loads(args) == {"city": "Sofia"}


async def test_router_acompletion_tools(fake):
    import litellm
    router = litellm.Router(model_list=[{"model_name": "r", "litellm_params": {
        "model": "openai/gpt-x", "api_key": "sk-up", "api_base": fake.url + "/v1", "max_retries": 0}}],
        num_retries=0)
    resp = await router.acompletion(model="r", messages=[{"role": "user", "content": "w"}], tools=OAI_TOOLS)
    assert resp.choices[0].message.tool_calls[0].function.name == "get_weather"


def test_sync_completion_tools(fake):
    import litellm
    resp = litellm.completion(model="openai/gpt-x", messages=[{"role": "user", "content": "w"}], tools=OAI_TOOLS,
                              api_key="sk-up", api_base=fake.url + "/v1", max_retries=0)
    assert resp.choices[0].message.tool_calls[0].function.name == "get_weather"


# ---------------------------------------------------------------------------
# kiko_server routes with tools
# ---------------------------------------------------------------------------

def _ant(**kw):
    return {"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "weather in Sofia?"}],
            "tools": ANT_TOOLS, **kw}


async def test_messages_tools_nonstream(full_app):
    r = await _post(full_app, "/v1/messages", _ant())
    assert r.status_code == 200, r.text
    d = r.json()
    blocks = [b for b in d["content"] if b["type"] == "tool_use"]
    assert blocks and blocks[0]["name"] == "get_weather" and blocks[0]["input"] == {"city": "Sofia"}
    assert d["stop_reason"] == "tool_use"


async def test_messages_tools_stream(full_app):
    r = await _post(full_app, "/v1/messages", _ant(stream=True))
    assert r.status_code == 200, r.text
    evs = _sse_events(r.text)
    starts = [d for e, d in evs if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
    assert starts and starts[0]["content_block"]["name"] == "get_weather"
    partial = "".join(d["delta"].get("partial_json", "") for e, d in evs if e == "content_block_delta")
    assert json.loads(partial) == {"city": "Sofia"}


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_completions_tools(full_app, stream):
    body = {"model": MODEL, "messages": [{"role": "user", "content": "w"}], "tools": OAI_TOOLS, "stream": stream}
    r = await _post(full_app, "/v1/chat/completions", body)
    assert r.status_code == 200, r.text
    if stream:
        assert "get_weather" in r.text
    else:
        assert r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"


async def test_responses_route_with_tools(full_app):
    body = {"model": MODEL, "input": [{"role": "user", "content": "w"}], "stream": False,
            "tools": [{"type": "function", "name": "get_weather", "description": "d",
                       "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}]}
    r = await _post(full_app, "/v1/responses", body)
    assert r.status_code == 200, r.text
    assert any(o.get("type") == "function_call" and o.get("name") == "get_weather" for o in r.json()["output"])


@pytest.mark.parametrize("stream", [False, True])
async def test_messages_tools_gemini(full_app, fake, stream):
    r = await _post(full_app, "/v1/messages", _ant(model=GEM_MODEL, stream=stream))
    assert r.status_code == 200, r.text
    assert any(b.get("tools") or b.get("tool_config") for b in fake.requests), fake.requests
    if stream:
        evs = _sse_events(r.text)
        starts = [d for e, d in evs if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
        assert starts and starts[0]["content_block"]["name"] == "get_weather"
    else:
        blocks = [b for b in r.json()["content"] if b["type"] == "tool_use"]
        assert blocks and blocks[0]["name"] == "get_weather" and blocks[0]["input"] == {"city": "Sofia"}


# ---------------------------------------------------------------------------
# client-supplied MCP tools make no outbound connection (inert pass-through)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses", "/v1/chat/completions"])
async def test_client_mcp_tool_never_connects(full_app, fake, path):
    mcp_hits = []

    def _hit(req):
        mcp_hits.append(req.path)
        return server.JsonResponse({"jsonrpc": "2.0", "id": 1, "result": {}}, 200)

    app = server.App()
    for m in ("POST", "GET"):
        app.route(m, "/mcp")(_hit)
    mcp = server.Server(app, port=0).start()
    try:
        mcp_tool = {"type": "mcp", "server_label": "evil", "server_url": mcp.url + "/mcp",
                    "require_approval": "never"}
        if path == "/v1/messages":
            body = _ant(tools=[mcp_tool])
        elif path == "/v1/responses":
            body = {"model": MODEL, "input": [{"role": "user", "content": "w"}], "tools": [mcp_tool]}
        else:
            body = {"model": MODEL, "messages": [{"role": "user", "content": "w"}], "tools": [mcp_tool]}
        try:
            r = await _post(full_app, path, body)
            rejected = r.status_code >= 400
            text = r.text
        except Exception:
            rejected, text = True, ""
        # Observed contract: kiko never acts on a client mcp tool. It is either rejected, or passed to the model as
        # an inert tool definition; either way kiko must not connect to the client's server_url nor run mcp calls.
        assert mcp_hits == [], f"outbound connection to client mcp server_url: {mcp_hits}"
        if not rejected:
            assert "mcp_call" not in text and "mcp_list_tools" not in text and "mcp_approval_request" not in text
    finally:
        mcp.stop()
