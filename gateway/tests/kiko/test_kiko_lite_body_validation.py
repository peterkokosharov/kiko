# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Malformed Anthropic bodies are a 400 invalid_request_error for openai- and gemini-backed models (no upstream call)."""
import copy
import json

import pytest

from kiko_lite import app as lite
from kiko_lite.providers import gemini
from kikolib import http as khttp
from kikolib import server

MSG = [{"role": "user", "content": "hi"}]
OAI = {"choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
GEM = {"candidates": [{"content": {"parts": [{"text": "yo"}]}, "finishReason": "STOP"}]}
MODELS = ["fake/m", "fakeg/gemini-2.5-flash"]


@pytest.fixture
def env(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    hits = []
    up = server.App()

    @up.route("POST", "/v1/chat/completions")
    def chat(req):
        hits.append(1)
        return server.JsonResponse(OAI)

    @up.route("POST", "/v1beta/models/{mo}")
    def gem(req):
        hits.append(1)
        return server.JsonResponse(GEM)

    upstream = server.Server(up, port=0).start()
    table = {"fake": {"base_url": upstream.url + "/v1", "kind": "openai", "key_names": ["K"], "auth": "bearer"},
             "fakeg": {"base_url": upstream.url, "kind": "gemini", "key_names": ["K"], "auth": "header:x-goog-api-key"}}
    resolver = lite.SimpleResolver(aliases={}, classes={}, env={}, providers=table)
    app = lite.build_app(resolver, "mk", providers=table, secret=lambda n: "sk")
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()

    class E:
        pass

    e = E()
    e.hits = hits

    def post(body, path="/v1/messages"):
        r = client.request("POST", srv.url + path, {"x-api-key": "mk", "content-type": "application/json"},
                           json.dumps(body).encode())
        with r:
            r.read()
            return r.status, (r.json() if r.status != 200 else None)

    e.post = post
    yield e
    client.close()
    srv.stop()
    upstream.stop()


def req(model, **kw):
    return {"model": model, "max_tokens": 16, "messages": copy.deepcopy(MSG), **kw}


BAD = {
    "message_not_dict": {"messages": ["hi"]},
    "messages_not_list": {"messages": "hi"},
    "messages_dict": {"messages": {"role": "user", "content": "x"}},
    "bad_role": {"messages": [{"role": "system", "content": "x"}]},
    "content_int": {"messages": [{"role": "user", "content": 5}]},
    "block_not_dict": {"messages": [{"role": "user", "content": ["x"]}]},
    "stop_string": {"stop_sequences": "x"},
    "stop_nonstr": {"stop_sequences": [1]},
    "tool_id_int": {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": 5, "name": "t", "input": {}}]}]},
    "tool_result_id_int": {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": 5, "content": "x"}]}]},
    "system_int": {"system": 5},
    "system_bad_block": {"system": ["x"]},
    "tools_not_list": {"tools": "x"},
    "tool_no_name": {"tools": [{"input_schema": {}}]},
    "max_tokens_str": {"max_tokens": "9"},
    "max_tokens_zero": {"max_tokens": 0},
    "max_tokens_bool": {"max_tokens": True},
    "image_source_str": {"messages": [{"role": "user", "content": [{"type": "image", "source": "x"}]}]},
    "image_source_list": {"messages": [{"role": "user", "content": [{"type": "image", "source": ["x"]}]}]},
    "image_source_missing": {"messages": [{"role": "user", "content": [{"type": "image"}]}]},
    "document_source_str": {"messages": [{"role": "user", "content": [{"type": "document", "source": "x"}]}]},
    "document_source_missing": {"messages": [{"role": "user", "content": [{"type": "document"}]}]},
    "image_source_notype": {"messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
    "tool_result_image_source_str": {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "image", "source": "x"}]}]}]},
    "tool_result_text_int": {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": 5}]}]}]},
    "tool_result_block_str": {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": ["x"]}]}]},
    "tool_use_input_str": {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "n", "input": "x"}]}]},
    "system_image_source_str": {"system": [{"type": "image", "source": "x"}]},
}


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("name", sorted(BAD))
def test_malformed_body_is_400(env, model, name):
    st, data = env.post(req(model, **BAD[name]))
    assert st == 400, (name, st, data)
    assert data["type"] == "error" and data["error"]["type"] == "invalid_request_error"
    assert env.hits == []


@pytest.mark.parametrize("model", MODELS)
def test_count_tokens_validates_too(env, model):
    st, data = env.post(req(model, messages=["hi"]), "/v1/messages/count_tokens")
    assert st == 400 and data["error"]["type"] == "invalid_request_error"


def claude_code_body(model):
    return {
        "model": model, "max_tokens": 32000, "stream": False, "metadata": {"user_id": "u"},
        "temperature": 1, "thinking": {"type": "enabled", "budget_tokens": 1024},
        "system": [{"type": "text", "text": "You are Claude Code", "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "more"}],
        "tool_choice": {"type": "auto"}, "stop_sequences": ["END"],
        "tools": [{"name": "Bash", "description": "run", "input_schema": {
            "type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            "cache_control": {"type": "ephemeral"}},
            {"type": "web_search_20250305", "name": "web_search"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
                                         {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                     "data": "AAAA"}},
                                         {"type": "document", "source": {"type": "text", "data": "d"}}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hm", "signature": "s"},
                {"type": "redacted_thinking", "data": "x"},
                {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"cmd": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "file", "is_error": False},
                {"type": "tool_result", "tool_use_id": "toolu_01",
                 "content": [{"type": "text", "text": "a"}]}]},
        ],
    }


@pytest.mark.parametrize("model", MODELS)
def test_claude_code_shape_passes(env, model):
    st, data = env.post(claude_code_body(model))
    assert st == 200, data
    assert env.hits == [1]


def test_gemini_translator_is_defensive():
    out = gemini.to_gemini({"model": "gemini-2.5-flash", "messages": ["x", {"role": "user", "content": "a"}],
                            "stop_sequences": "x"})
    assert out["contents"] == [{"role": "user", "parts": [{"text": "a"}]}]
    assert out["generationConfig"]["stopSequences"] == ["x"]
    gemini.to_gemini({"model": "gemini-3-pro", "messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": 5, "name": "t", "input": {}}]}]})


@pytest.mark.parametrize("model", MODELS)
def test_translator_exception_becomes_400(env, monkeypatch, model):
    def boom(*a, **k):
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(gemini, "to_gemini", boom)
    monkeypatch.setattr(lite.oc, "to_openai", boom)
    st, data = env.post(req(model))
    assert st == 400 and data["error"]["type"] == "invalid_request_error"
    assert data["error"]["message"] == "malformed request body"
    assert env.hits == []


# ---- bodies that must PASS: url image source, tool_result holding an image list ----

def _passing_bodies():
    url_image = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    b64_image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    return {
        "url_image": {"messages": [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                                                url_image]}]},
        "tool_result_with_image_list": {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "shot", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                          "content": [{"type": "text", "text": "screenshot"}, b64_image]}]}]},
        "tool_result_with_url_image": {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "shot", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                          "content": [url_image]}]}]},
    }


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("name", sorted(_passing_bodies()))
def test_image_bodies_pass(env, model, name):
    st, data = env.post(req(model, **_passing_bodies()[name]))
    assert st == 200, (name, st, data)
    assert env.hits == [1]


def test_deeply_nested_tool_result_is_400_not_a_crash():
    inner = [{"type": "text", "text": "x"}]
    for _ in range(5000):
        inner = [{"type": "tool_result", "tool_use_id": "t", "content": inner}]
    body = {"model": "m", "messages": [{"role": "user", "content": inner}]}
    with pytest.raises(server.HTTPError) as ei:
        lite.validate_request(body)
    assert ei.value.status == 400 and ei.value.error_type == "invalid_request_error"


@pytest.mark.parametrize("model", MODELS)
def test_translator_failure_is_logged_at_warning_without_the_body(env, monkeypatch, caplog, model):
    def boom(*a, **k):
        raise KeyError("some-field")

    monkeypatch.setattr(gemini, "to_gemini", boom)
    monkeypatch.setattr(lite.oc, "to_openai", boom)
    caplog.set_level("WARNING", logger="kiko_lite.app")
    secret_text = "PRIVATE-PROMPT-TEXT"
    st, _ = env.post(req(model, messages=[{"role": "user", "content": secret_text}]))
    assert st == 400
    rec = [r for r in caplog.records if "translation failed" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelname == "WARNING" and rec[0].exc_info[0] is KeyError
    assert secret_text not in caplog.text
