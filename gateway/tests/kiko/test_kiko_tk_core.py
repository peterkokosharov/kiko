# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""kiko_tk: JSON client against a mock HTTP server, state/icon mapping, panel shaping, GUI smoke (skips without display)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kiko_tk import client, model
from kiko_tk.app import poll

STATUS = {
    "server": {"name": "kiko-lite", "version": "0.1.0", "uptime_seconds": 3700, "port": 4100, "db": "x.db"},
    "models": [
        {"alias": "a", "target": "p/a", "class": "HAIKU", "enabled": True, "key_name": "K", "key_found": "env",
         "health": "ok", "last_check": "t", "mapping_error": None, "state": "ok"},
        {"alias": "b", "target": "p/b", "class": "HAIKU", "enabled": False, "key_name": "", "key_found": "NOT FOUND",
         "health": "unknown", "last_check": None, "mapping_error": None, "state": "off"},
    ],
    "requests": [{"time": "t1", "model": "m", "upstream": "u", "route": "messages", "ok": True, "latency_ms": 12,
                  "error": None},
                 {"time": "t0", "model": "m", "upstream": None, "route": "chat", "ok": False, "latency_ms": None,
                  "error": "boom"}],
}


class _H(BaseHTTPRequestHandler):
    need_key = ""

    def do_GET(self):
        if self.path == "/health":
            body, code = {"status": "ok"}, 200
        elif self.path == "/ui/api/status":
            if self.need_key and self.headers.get("x-api-key") != self.need_key:
                body, code = {"error": "no"}, 401
            else:
                body, code = STATUS, 200
        else:
            body, code = {}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_client_ok(server):
    assert client.health(server)
    assert client.fetch_status(server)["server"]["port"] == 4100
    assert poll(server, "") == (STATUS, "")


def test_client_auth(server):
    _H.need_key = "secret"
    try:
        assert poll(server, "") == (None, "auth")
        assert poll(server, "secret")[1] == ""
    finally:
        _H.need_key = ""


def test_client_unreachable():
    assert poll("http://127.0.0.1:9", "") == (None, "down")  # port 9: nothing listens
    assert not client.health("http://127.0.0.1:9", 1.0)


def test_state_and_icon_mapping():
    assert model.overall_state(None) == "unknown"
    assert model.overall_state(None, "down") == "down"
    assert model.overall_state(None, "auth") == "auth"
    assert model.overall_state(STATUS) == "errors"  # newest-5 contains a failed request
    clean = {"models": STATUS["models"], "requests": STATUS["requests"][:1]}
    assert model.overall_state(clean) == "ok"
    bad = {"models": [{"state": "bad"}], "requests": []}
    assert model.overall_state(bad) == "errors"
    assert set(model.COLORS) >= {"ok", "errors", "down", "auth", "unknown"}
    assert "not reachable" in model.tooltip("down", None, "http://h")
    assert "1 ok / 0 bad of 2" in model.tooltip("ok", clean | {"models": STATUS["models"]}, "")


def test_panel_shaping():
    assert ("uptime", "1 h 1 min") in model.server_rows(STATUS)
    rows = model.model_rows(STATUS)
    assert rows[0][:4] == ("a", "p/a", "HAIKU", "yes") and rows[1][3] == "no" and rows[1][5] == "NOT FOUND"
    reqs = model.request_rows(STATUS)
    assert reqs[0] == ("ok", "t1", "m > u", "messages", "12 ms", "")
    assert reqs[1][0] == "ERR" and reqs[1][4] == "" and reqs[1][5] == "boom"
    assert model.class_rows(STATUS) == [("HAIKU", "2", "1", "a, b")]


def test_gui_smoke(server):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from kiko_tk.app import App
        app = App(root, server)
        app.show(STATUS, "")
        assert len(app.trees["Models"].get_children()) == 2
        app.show(None, "down")
        assert "not reachable" in app.banner.get()
        app.stop.set()
        app.wake.set()
    finally:
        root.destroy()


def test_redirect_refused_and_key_not_resent():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("x-api-key"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redir(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    redir = HTTPServer(("127.0.0.1", 0), Redir)
    for s in (target, redir):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with pytest.raises(client.Unreachable, match="redirect"):
            client.get_json(f"http://127.0.0.1:{redir.server_port}", "/ui/api/status", "secret")
        assert seen == []  # the target was never contacted, so the key was not re-sent
    finally:
        for s in (target, redir):
            s.shutdown()
            s.server_close()


def test_model_tolerates_bad_data():
    bad = {"server": "x", "models": [{"state": "bad"}, "junk", None], "requests": "nope"}
    assert model.overall_state(bad) == "errors"
    assert model.model_rows(bad)[0][0] == "?"
    assert model.request_rows(bad) == []
    assert model.class_rows(bad) == [("-", "1", "0", "?")]
    assert ("uptime", "0 h 0 min") in model.server_rows(bad)
    assert "of 1 models" in model.tooltip("errors", bad, "")


def test_drain_reschedules_when_show_raises():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from kiko_tk.app import App
        app = App(root, "http://127.0.0.1:9")
        app.stop.set()
        app.wake.set()
        calls = []
        root.after = lambda ms, fn=None: calls.append(ms)  # type: ignore[method-assign]
        app.show = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        app.q.put((None, "down"))
        with pytest.raises(RuntimeError):
            app._drain()
        assert calls == [200]
    finally:
        root.destroy()
