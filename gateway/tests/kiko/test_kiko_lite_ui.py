# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite.ui: read-only status page (GET /ui, GET /ui/api/status), loopback only, same-origin guard exception."""
import http.client
import json
import threading
import time

import pytest

from kiko_lite import ui
from kiko_lite.db import Database
from kiko_lite.resolver import build_db_app
from kikolib import keystore, server

SECRET = "sk-test-SECRET-value-123456"
JSON = {"Content-Type": "application/json"}


def _serve(tmp_path, monkeypatch, master=None, auth=False):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("KIKO_TEST_UI_KEY", SECRET)
    db = Database(tmp_path / "k.db")
    db.upsert_model("m-env", "nvidia/meta/llama-3.3-70b-instruct", "KIKO_TEST_UI_KEY", status="ok")
    db.upsert_model("m-none", "groq/llama-3.1-8b", "KIKO_TEST_UI_MISSING_KEY", status="error")
    db.add_billing("m-env", 1, 1, 0.0, latency_ms=42, upstream_model="x")
    db.add_billing("m-none", 0, 0, 0.0, error="api_error: <script>boom</script>" + "x" * 500)
    app = build_db_app(db, master, secret=lambda n: None, auth=auth)
    srv = server.Server(app, port=0).start()
    ui.install(app, db, tmp_path / "k.db", srv.port)
    return srv


def _get(srv, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        c.request(method, path, body="" if method == "POST" else None, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(), r
    finally:
        c.close()


def _settled(srv):
    """GET /ui/api/status until the background key lookup has answered (no "checking" left)."""
    for _ in range(200):
        st, body, r = _get(srv, "/ui/api/status")
        if st != 200 or b'"checking"' not in body:
            break
        time.sleep(0.05)
    return st, body, r


@pytest.fixture
def srv(tmp_path, monkeypatch):
    s = _serve(tmp_path, monkeypatch)
    yield s
    s.stop()


def test_page_is_served_and_self_contained(srv):
    for path in ("/ui", "/ui/"):
        st, body, r = _get(srv, path)
        assert st == 200 and r.getheader("Content-Type").startswith("text/html")
        text = body.decode()
        assert "/ui/api/status" in text and "http://" not in text and "https://" not in text
        assert r.getheader("Access-Control-Allow-Origin") is None


def test_status_json_has_no_key_value(srv):
    st, body, r = _settled(srv)
    assert st == 200 and r.getheader("Access-Control-Allow-Origin") is None
    assert SECRET.encode() not in body and SECRET[:8].encode() not in body
    d = json.loads(body)
    models = {m["alias"]: m for m in d["models"]}
    assert models["m-env"]["key_name"] == "KIKO_TEST_UI_KEY" and models["m-env"]["key_found"] == "env"
    assert models["m-env"]["state"] == "ok" and models["m-env"]["target"] == "nvidia/meta/llama-3.3-70b-instruct"
    assert models["m-none"]["key_found"] == "NOT FOUND" and models["m-none"]["state"] == "bad"
    assert d["server"]["port"] == srv.port and d["server"]["db"].endswith("k.db")
    assert isinstance(d["server"]["uptime_seconds"], int)
    reqs = d["requests"]
    assert len(reqs) == 2 and reqs[0]["ok"] is False and len(reqs[0]["error"]) <= ui.ERROR_CHARS
    assert reqs[1]["ok"] is True and reqs[1]["latency_ms"] == 42


def test_same_origin_get_ok_foreign_origin_refused(srv):
    own = f"http://127.0.0.1:{srv.port}"
    assert _get(srv, "/ui", {"Origin": own})[0] == 200
    assert _get(srv, "/ui/api/status", {"Origin": own})[0] == 200
    for bad in ("http://evil.example", "null", f"http://127.0.0.1:{srv.port + 1}", f"https://127.0.0.1:{srv.port}"):
        assert _get(srv, "/ui", {"Origin": bad})[0] == 403, bad
        assert _get(srv, "/ui/api/status", {"Origin": bad})[0] == 403, bad


def test_guard_not_weakened_for_v1_and_post(srv):
    own = f"http://127.0.0.1:{srv.port}"
    assert _get(srv, "/v1/messages", {"Origin": own, **JSON}, "POST")[0] == 403
    assert _get(srv, "/ui/api/status", {"Origin": own, **JSON}, "POST")[0] == 403
    assert _get(srv, "/health", {"Origin": own})[0] == 403


def test_non_loopback_host_refused(srv):
    for host in ("evil.example", "192.168.1.5:4000", "evil.example:80"):
        assert _get(srv, "/ui", {"Host": host})[0] == 403, host
        assert _get(srv, "/ui/api/status", {"Host": host})[0] == 403, host
    assert _get(srv, "/ui", {"Host": f"localhost:{srv.port}"})[0] == 200


def test_auth_on_status_needs_key_page_does_not(tmp_path, monkeypatch):
    s = _serve(tmp_path, monkeypatch, master="master-key-1", auth=True)
    try:
        assert _get(s, "/ui")[0] == 200
        assert _get(s, "/ui/api/status")[0] == 401
        st, body, _ = _get(s, "/ui/api/status", {"x-api-key": "master-key-1"})
        assert st == 200 and b"master-key-1" not in body and SECRET.encode() not in body
    finally:
        s.stop()


def test_uix_is_not_a_same_origin_path(srv):
    own = f"http://127.0.0.1:{srv.port}"
    assert _get(srv, "/uix", {"Origin": own})[0] == 403
    assert _get(srv, "/ui/", {"Origin": own})[0] == 200


def test_peer_must_be_loopback():
    class Req:
        headers = {"host": "localhost"}
        client = ("10.1.2.3", 5555)

    with pytest.raises(server.HTTPError) as e:
        ui._loopback(Req())
    assert e.value.status == 403
    Req.client = ("127.0.0.1", 5555)
    ui._loopback(Req())
    Req.client = ("::1", 5555)
    ui._loopback(Req())


def test_csp_frame_ancestors_and_xfo(srv):
    r = _get(srv, "/ui")[2]
    assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Frame-Options") == "DENY"


def test_slow_store_does_not_block_and_is_single_flight(tmp_path, monkeypatch):
    gate, calls = threading.Event(), []

    def slow(name, env_file=None):
        calls.append(name)
        gate.wait(10)
        return "store"

    s = _serve(tmp_path, monkeypatch)
    monkeypatch.setattr(ui.keys, "key_source", slow)
    try:
        t0 = time.monotonic()
        results = [_get(s, "/ui/api/status") for _ in range(4)]
        assert time.monotonic() - t0 < 5
        for st, body, _ in results:
            assert st == 200
            assert {m["key_found"] for m in json.loads(body)["models"]} == {"checking"}
        time.sleep(0.2)
        assert len(calls) == 1  # one worker, one lookup at a time
        gate.set()
        st, body, _ = _settled(s)
        assert {m["key_found"] for m in json.loads(body)["models"]} == {"store"}
        assert len(calls) == 2  # one per key name, each once
        _get(s, "/ui/api/status")
        assert len(calls) == 2  # inside the cache time: no new lookup
    finally:
        gate.set()
        s.stop()


def test_store_timeout_is_not_not_found(tmp_path, monkeypatch):
    def timed_out(name, env_file=None):
        keystore._timeouts += 1
        return None

    s = _serve(tmp_path, monkeypatch)
    monkeypatch.setattr(keystore, "_timeouts", keystore._timeouts)
    monkeypatch.setattr(ui.keys, "key_source", timed_out)
    try:
        st, body, _ = _settled(s)
        models = {m["alias"]: m for m in json.loads(body)["models"]}
        assert models["m-none"]["key_found"] == "store timeout"
        assert models["m-env"]["key_found"] == "store timeout" and models["m-env"]["state"] == "off"
    finally:
        s.stop()
