# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""orjson_compat: stdlib fallback and orjson-present behave the same."""

import importlib
import json
import sys

import pytest

MOD = "litellm.proxy.common_utils.orjson_compat"


@pytest.fixture
def fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "orjson", None)  # import orjson -> ImportError
    sys.modules.pop(MOD, None)
    mod = importlib.import_module(MOD)
    yield mod
    monkeypatch.undo()
    sys.modules.pop(MOD, None)
    importlib.import_module(MOD)


def _check(mod):
    out = mod.dumps({"a": [1, "é"]})
    assert isinstance(out, bytes)
    assert json.loads(out) == {"a": [1, "é"]}
    assert mod.loads(out) == {"a": [1, "é"]}
    assert mod.loads(out.decode()) == {"a": [1, "é"]}
    assert mod.loads(bytearray(out)) == {"a": [1, "é"]}
    assert isinstance(mod.dumps({"k": 1}, option=mod.OPT_NON_STR_KEYS), bytes)
    assert mod.dumps({1: "x"}, option=mod.OPT_NON_STR_KEYS) == b'{"1":"x"}'
    assert mod.dumps(object(), default=str).startswith(b'"<object')
    with pytest.raises(mod.JSONDecodeError):
        mod.loads(b"{bad")
    with pytest.raises(ValueError):  # both implementations subclass ValueError
        mod.loads("{bad")


def test_fallback_without_orjson(fallback):
    assert fallback.JSONDecodeError is json.JSONDecodeError
    _check(fallback)


def test_with_orjson_present():
    pytest.importorskip("orjson")
    sys.modules.pop(MOD, None)
    mod = importlib.import_module(MOD)
    import orjson

    assert mod.dumps is orjson.dumps
    _check(mod)


def test_orjson_response_fallback(fallback):
    resp = fallback.ORJSONResponse(content={"a": 1})
    assert resp.body == b'{"a":1}'
    assert fallback.ORJSONResponse(content={"é": 1}).body == '{"é":1}'.encode()

    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()

    @app.get("/x", response_class=fallback.ORJSONResponse)
    async def x():
        return {"a": 1}

    r = TestClient(app).get("/x")
    assert r.status_code == 200
    assert r.json() == {"a": 1}
