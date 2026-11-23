# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: OpenCode Big Pickle
"""Gemini CLI front: /v1beta/models/{model}:generateContent and :streamGenerateContent, end to end."""
import json

from kikolib import sse

from kiko_lite import gemini_front

from .test_kiko_lite_app import MASTER, chunk, env, oai  # noqa: F401  (env is a fixture)

MODEL = "claude-sonnet-x"


def gemini(env, body, stream=False):
    suffix = ":streamGenerateContent" if stream else ":generateContent"
    return env.post(f"/v1beta/models/{MODEL}{suffix}", body, stream=stream)


def frames(r):
    out = []
    with r:
        for ev in sse.iter_events(r.iter_lines()):
            if ev.data.startswith("{"):
                out.append(json.loads(ev.data))
    return out


def test_nonstream_text(env):
    env.fake.add("openai", **oai("hello", usage={"prompt_tokens": 11, "completion_tokens": 3}))
    r = gemini(env, {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                     "generationConfig": {"maxOutputTokens": 50, "topK": 40}})
    assert r.status == 200
    d = json.loads(r.read())
    assert d["candidates"][0]["content"]["parts"] == [{"text": "hello"}]
    assert d["candidates"][0]["finishReason"] == "STOP"
    assert d["usageMetadata"] == {"promptTokenCount": 11, "candidatesTokenCount": 3, "totalTokenCount": 14}
    sent = env.fake.requests[0]["body"]
    assert sent["model"] == "model-s"
    assert sent.get("max_tokens", sent.get("max_completion_tokens")) == 50
    assert "topK" not in sent and sent["messages"] == [{"role": "user", "content": "hi"}]


def test_stream_text(env):
    env.fake.add("openai", sse=[chunk({"role": "assistant", "content": "Hel"}), chunk({"content": "lo"}),
                                chunk({}, finish="stop"), "[DONE]"])
    r = gemini(env, {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}, stream=True)
    assert r.status == 200
    out = frames(r)
    text = "".join(p.get("text", "") for f in out for p in f["candidates"][0]["content"]["parts"])
    assert text == "Hello"
    assert out[-1]["candidates"][0]["finishReason"] == "STOP"


def test_models_google_shape(env):
    r = env.client.request("GET", env.url + "/v1beta/models", {"x-api-key": MASTER}, b"")
    assert r.status == 200
    d = json.loads(r.read())
    assert d["models"] and all(m["name"].startswith("models/") for m in d["models"])


def test_convert_tools_and_system():
    body = {
        "systemInstruction": {"parts": [{"text": "Be brief"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "call it"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {"a": 1}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"ok": True}}}]},
        ],
        "tools": [{"functionDeclarations": [{"name": "f", "description": "test",
                                             "parameters": {"type": "OBJECT", "properties": {
                                                 "a": {"type": "INTEGER"}}}}]}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"temperature": 0.2, "topP": 0.9, "topK": 40},
    }
    a, opts = gemini_front.convert_request(body, "gemini-lite", stream=False)
    assert opts == {"stream": False, "model": "gemini-lite"}
    assert a["system"] == "Be brief" and a["temperature"] == 0.2 and a["top_p"] == 0.9
    assert "topK" not in a
    assert a["tools"][0]["input_schema"]["type"] == "object"
    assert a["tools"][0]["input_schema"]["properties"]["a"]["type"] == "integer"
    assert a["messages"][1]["content"][0]["type"] == "tool_use"
    assert a["messages"][2]["content"][0]["type"] == "tool_result"
    assert a["messages"][2]["content"][0]["tool_use_id"] == a["messages"][1]["content"][0]["id"]
