# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""A models row with an unknown provider prefix must never reach api.openai.com; mistral/ is a real provider."""

import io
import json

import pytest

from kiko_lite import db as kdb
from kiko_lite.resolver import Catalog, DBProviders, DBResolver, MappingError, build_db_app, map_row
from kikolib import http as khttp
from kikolib import retry, server


@pytest.fixture(autouse=True)
def _isolated_default_db(monkeypatch, tmp_path):
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "isolated-default" / "kiko.db"))


def row(provider, api_base=None, key="MY_KEY"):
    return {"provider": provider, "api_base": api_base, "api_key_env": key}


@pytest.mark.parametrize("provider", ["anthropic/claude-x", "openrouter/some/model", "weird/m", "Mistral2/x"])
@pytest.mark.parametrize("api_base", [None, "", "  "])
def test_unknown_prefix_without_api_base_is_refused(provider, api_base):
    with pytest.raises(MappingError, match="not supported"):
        map_row(row(provider, api_base))


def test_unknown_prefix_with_https_api_base_is_custom():
    m = map_row(row("weird/m", "https://llm.example.org/v1/"))
    assert (m.provider, m.model_id) == ("custom", "weird/m")
    assert m.entry["base_url"] == "https://llm.example.org/v1" and m.entry["key_names"] == ["MY_KEY"]


def test_unknown_prefix_with_bad_api_base_is_refused():
    with pytest.raises(MappingError):
        map_row(row("weird/m", "http://evil.example.org/v1"))


def test_mistral_prefix_maps_to_mistral_provider():
    m = map_row(row("mistral/mistral-large-latest", None, "MISTRAL_API_KEY"))
    assert (m.provider, m.model_id) == ("mistral", "mistral-large-latest")
    assert m.entry["base_url"] == "https://api.mistral.ai/v1" and m.entry["key_names"] == ["MISTRAL_API_KEY"]


def test_openai_and_nvidia_unchanged():
    m = map_row(row("openai/gpt-4o"))
    assert m.provider == "openai" and m.entry["base_url"] == "https://api.openai.com/v1"
    m = map_row(row("gpt-4.1", None, "OPENAI_API_KEY"))
    assert m.provider == "openai" and m.entry["base_url"] == "https://api.openai.com/v1"
    m = map_row(row("openai/nvidia/nemotron", "https://integrate.api.nvidia.com/v1"))
    assert (m.provider, m.model_id) == ("nvidia", "nvidia/nemotron")
    assert m.entry["base_url"] == "https://integrate.api.nvidia.com/v1"


def test_openai_only_with_openai_host_for_every_row():
    for prov, base in [("openai/x", None), ("gpt-4o", None), ("openai/x", "https://api.openai.com/v1"),
                       ("weird/x", "https://api.openai.com/v1"), ("openai/x", "https://llm.example.org/v1")]:
        m = map_row(row(prov, base, "OPENAI_API_KEY"))
        if m.provider == "openai":
            assert m.entry["base_url"].startswith("https://api.openai.com/")


def test_refused_row_is_listed_with_reason_and_not_routed(tmp_path):
    db = kdb.Database(tmp_path / "t" / "kiko.db")
    db.upsert_model("mi", "anthropic/claude-x", "ANTHROPIC_API_KEY", None, "SONNET", 1, status="ok")
    cat = Catalog(db, 0.0)
    (info,) = cat.snapshot().models
    assert info.mapped is None and "not supported" in info.error
    assert DBResolver(cat, {})("mi") is None and "db/mi" not in list(DBProviders(cat))


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, body=None, timeout=0, stream=False):
        self.calls.append({"url": url, "headers": {k.lower(): v for k, v in (headers or {}).items()}})
        data = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return khttp.Response(200, "x", khttp.Headers([("content-type", "application/json")]),
                              io.BytesIO(json.dumps(data).encode()))

    def close(self):
        pass


@pytest.fixture
def wired(tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    db = kdb.Database(tmp_path / "t" / "kiko.db")
    fake = FakeHttp()
    policy = retry.RetryPolicy(attempts=1, base_delay=0.01, max_delay=0.02, budget=1.0, jitter=False)
    secrets = {"MISTRAL_API_KEY": "MISTRAL-SECRET", "OTHER_KEY": "OTHER-SECRET"}
    app = build_db_app(db, "master", secret=secrets.get, env={}, ttl=0.0, http=fake, policy=policy,
                       ping_interval=0.2, upstream_timeout=2.0)
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()

    def post(model):
        body = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        return client.request("POST", srv.url + "/v1/messages", {"x-api-key": "master", "content-type":
                              "application/json"}, json.dumps(body).encode())

    class W:
        pass

    w = W()
    w.db, w.fake, w.post, w.secrets = db, fake, post, secrets
    yield w
    client.close()
    srv.stop()


def test_mistral_row_goes_to_mistral_with_its_key(wired):
    wired.db.upsert_model("mi", "mistral/mistral-large-latest", "MISTRAL_API_KEY", None, "SONNET", 1, status="ok")
    assert wired.post("claude-sonnet-4").status == 200
    (call,) = wired.fake.calls
    assert call["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer MISTRAL-SECRET"


def test_unknown_prefix_row_sends_nothing_to_openai(wired):
    wired.db.upsert_model("an", "anthropic/claude-x", "MISTRAL_API_KEY", None, "SONNET", 1, status="ok")
    wired.db.upsert_model("or", "openrouter/x/y", "OTHER_KEY", None, "OPUS", 1, status="ok")
    for model in ("claude-sonnet-4", "claude-opus-4", "an", "or"):
        assert wired.post(model).status >= 400
    assert wired.fake.calls == []


# ---- bare ids, bad URLs, mistral request quirks ----

@pytest.mark.parametrize("model,key", [("claude-3-5-sonnet-20241022", "ANTHROPIC_API_KEY"),
                                       ("mistral-large-latest", "MISTRAL_API_KEY"),
                                       ("llama-3.3-70b", ""), ("gpt-4o", "MISTRAL_API_KEY"),
                                       ("gpt-4o", "MY_KEY")])
def test_bare_id_not_openai_or_foreign_key_is_refused(model, key):
    with pytest.raises(MappingError):
        map_row(row(model, None, key))


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1", "o1-mini", "o3", "chatgpt-4o-latest", "ft:gpt-4o:org::id",
                                   "text-embedding-3-small", "davinci-002", "babbage-002"])
def test_bare_openai_id_with_openai_key_still_works(model):
    m = map_row(row(model, None, "OPENAI_API_KEY"))
    assert m.provider == "openai" and m.entry["key_names"] == ["OPENAI_API_KEY"]


def test_bare_rows_send_no_request(wired):
    wired.db.upsert_model("a", "claude-3-5-sonnet-20241022", "ANTHROPIC_API_KEY", None, "SONNET", 1, status="ok")
    wired.db.upsert_model("m", "mistral-large-latest", "MISTRAL_API_KEY", None, "OPUS", 1, status="ok")
    for model in ("claude-sonnet-4", "claude-opus-4", "a", "m"):
        assert wired.post(model).status >= 400
    assert wired.fake.calls == []


def test_bare_gpt_with_openai_key_goes_to_openai(wired):
    wired.db.upsert_model("g", "gpt-4o", "OPENAI_API_KEY", None, "SONNET", 1, status="ok")
    wired.secrets["OPENAI_API_KEY"] = "OPENAI-SECRET"
    assert wired.post("claude-sonnet-4").status == 200
    (call,) = wired.fake.calls
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer OPENAI-SECRET"


BAD_URLS = ["https://[::1", "https://[", "http://[::1]:99999x"]


@pytest.mark.parametrize("base", BAD_URLS)
def test_bad_api_base_is_mapping_error(base):
    with pytest.raises(MappingError):
        map_row(row("openai/m", base))
    with pytest.raises(MappingError):
        map_row(row("weird/m", base))


def test_bad_api_base_does_not_break_snapshot(tmp_path):
    db = kdb.Database(tmp_path / "t" / "kiko.db")
    db.upsert_model("bad", "openai/m", "K", "https://[::1", "SONNET", 1, status="ok")
    db.upsert_model("good", "openai/m", "K", "https://llm.example.org/v1", "OPUS", 1, status="ok")
    snap = Catalog(db, 0.0).snapshot()
    by = {i.row["alias"]: i for i in snap.models}
    assert by["bad"].mapped is None and "not a valid URL" in by["bad"].error and by["good"].mapped is not None


def test_bad_api_base_does_not_break_add_model(tmp_path, capsys):
    from kiko_lite import cli
    path = tmp_path / "d" / "kiko.db"
    code = cli.main(["add-model", "x", "--provider", "openai/m", "--api-base", "https://[::1",
                     "--api-key-env", "MY_KEY", "--db", str(path)])
    assert code != 0 and "not a valid URL" in capsys.readouterr().err


def _body(provider_row, stream=False, tools=None, tool_choice=None):
    from kiko_lite.providers import openai_compat as oc
    flags = map_row(provider_row).entry["flags"]
    body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}], "stream": stream}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    return oc.to_openai(body, "m", max_tokens_field=flags.get("max_tokens_field", "max_tokens"),
                        stream_usage=flags.get("stream_usage", True), mistral=bool(flags.get("mistral")))


def test_mistral_stream_body_has_no_stream_options_but_others_do():
    out = _body(row("mistral/m"), stream=True)
    assert out["stream"] is True and "stream_options" not in out
    assert _body(row("groq/m"), stream=True)["stream_options"] == {"include_usage": True}


TOOL = {"name": "t", "description": "d", "input_schema": {
    "$schema": "x", "$id": "i", "type": "object",
    "properties": {"a": {"$id": "deep", "type": "object", "properties": {"b": {"$schema": "y", "type": "string"}}},
                   "l": {"anyOf": [{"$id": "z", "type": "string"}]}}}}


def test_mistral_tool_choice_and_schema_quirks():
    for tc in ({"type": "any"}, {"type": "tool", "name": "t"}):
        assert _body(row("mistral/m"), tools=[TOOL], tool_choice=tc)["tool_choice"] == "any"
    assert _body(row("mistral/m"), tools=[TOOL], tool_choice={"type": "auto"})["tool_choice"] == "auto"
    params = _body(row("mistral/m"), tools=[TOOL])["tools"][0]["function"]["parameters"]
    assert "$id" not in json.dumps(params) and "$schema" not in json.dumps(params)
    assert params["properties"]["a"]["properties"]["b"] == {"type": "string"}
    assert TOOL["input_schema"]["properties"]["a"]["$id"] == "deep"  # the request was not modified


def test_other_providers_tool_quirks_unchanged():
    out = _body(row("groq/m"), tools=[TOOL], tool_choice={"type": "any"})
    assert out["tool_choice"] == "required"
    assert out["tools"][0]["function"]["parameters"]["properties"]["a"]["$id"] == "deep"
