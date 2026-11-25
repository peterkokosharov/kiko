# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus 4.8
"""kiko_runtime_lib: the in-process facade, driven against a fake upstream (no network, no kiko server)."""
import json
import threading

import pytest

from kiko_runtime_lib import Gateway, GatewayError, provider_table
from kikolib import server


class FakeUpstream:
    """Scripted OpenAI / Gemini upstream on a real loopback socket (the provider side only)."""

    def __init__(self):
        self.script = {"openai": [], "gemini": []}
        self.requests = []
        self.lock = threading.Lock()
        app = server.App()
        app.route("POST", "/v1/chat/completions")(lambda req: self._play("openai", req))
        app.route("POST", "/v1beta/models/{mo}")(lambda req: self._play("gemini", req))
        app.route("POST", "/v1alpha/models/{mo}")(lambda req: self._play("gemini", req))
        self.server = server.Server(app, port=0).start()

    @property
    def url(self):
        return self.server.url

    def add(self, kind, **item):
        self.script[kind].append(item)

    def _play(self, kind, req):
        with self.lock:
            self.requests.append({"kind": kind, "body": req.json()})
            item = self.script[kind].pop(0) if self.script[kind] else {"status": 500, "json": {"error": "empty"}}
        if "sse" in item:
            frames = [f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n" for f in item["sse"]]
            return server.StreamResponse(iter(frames), item.get("status", 200))
        return server.JsonResponse(item.get("json", {}), item.get("status", 200))

    def stop(self):
        self.server.stop()


def oai(content="hello", finish="stop", usage=None, **msg):
    return {"json": {"choices": [{"message": {"content": content, **msg}, "finish_reason": finish}],
                     "usage": usage or {"prompt_tokens": 11, "completion_tokens": 3}}}


def chunk(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


@pytest.fixture
def gw(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    fake = FakeUpstream()
    table = provider_table({
        "fake": {"base_url": fake.url + "/v1", "kind": "openai", "key_names": ["FAKE_KEY"], "auth": "bearer",
                 "flags": {}},
        "fakeg": {"base_url": fake.url, "kind": "gemini", "key_names": ["FAKEG_KEY"],
                  "auth": "header:x-goog-api-key", "flags": {}},
    })
    g = Gateway.from_config(
        models={"gpt-x": "fake/model-s", "flash": "fakeg/gemini-3.8-flash"},
        providers=table, secret={"FAKE_KEY": "sk", "FAKEG_KEY": "sk"}.get, env={},
    )
    g._fake = fake  # type: ignore[attr-defined]
    yield g
    fake.stop()


def test_provider_table_is_a_copy():
    base = provider_table()
    assert "openai" in base and "gemini" in base
    base["openai"]["base_url"] = "changed"
    assert provider_table()["openai"]["base_url"] != "changed"  # the original registry is untouched


def test_nonstream_text(gw):
    gw._fake.add("openai", **oai("hello world"))
    out = gw.chat({"model": "gpt-x", "messages": [{"role": "user", "content": "hi"}]})
    assert out["object"] == "chat.completion" and out["model"] == "gpt-x"
    assert out["choices"][0]["message"]["content"] == "hello world"
    assert out["usage"]["total_tokens"] == 14
    assert gw._fake.requests[0]["body"]["model"] == "model-s"  # resolved to the upstream id


def test_stream_text(gw):
    gw._fake.add("openai", sse=[chunk({"role": "assistant", "content": "Hel"}), chunk({"content": "lo"}),
                                chunk({}, finish="stop"), "[DONE]"])
    chunks = list(gw.chat({"model": "gpt-x", "messages": [{"role": "user", "content": "hi"}], "stream": True}))
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Hello"
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)


def test_tools_passthrough(gw):
    gw._fake.add("openai", **oai(None, finish="tool_calls", tool_calls=[
        {"id": "call_9", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}]))
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    out = gw.chat({"model": "gpt-x", "messages": [{"role": "user", "content": "go"}], "tools": tools})
    call = out["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "f"
    assert gw._fake.requests[0]["body"]["tools"][0]["function"]["name"] == "f"


def test_gemini_translation(gw):
    gw._fake.add("gemini", **{"json": {"candidates": [{"content": {"parts": [{"text": "hi from gemini"}]},
                                                       "finishReason": "STOP"}],
                                       "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3}}})
    out = gw.chat({"model": "flash", "messages": [{"role": "user", "content": "hi"}]})
    assert out["choices"][0]["message"]["content"] == "hi from gemini"
    assert gw._fake.requests[0]["kind"] == "gemini"  # it reached the native Gemini endpoint


def test_unknown_model_raises(gw):
    with pytest.raises(GatewayError) as e:
        gw.chat({"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]})
    assert e.value.status >= 400
