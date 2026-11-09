# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.server: {name:path} routes, App.after hooks, App.allow_same_origin (guard)."""
import http.client
import json

import pytest

from kikolib.server import App, HTTPError, JsonResponse, Server

T = 5.0


def _call(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection(srv.host, srv.port, timeout=T)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_path_param_matches_rest_of_path():
    app = App()

    @app.route("GET", "/files/{p:path}")
    def files(req):
        return JsonResponse({"p": req.params["p"]})

    @app.route("GET", "/one/{x}")
    def one(req):
        return JsonResponse({"x": req.params["x"]})

    with Server(app, port=0) as srv:
        st, body = _call(srv, "GET", "/files/a/b/c.txt")
        assert st == 200 and json.loads(body) == {"p": "a/b/c.txt"}
        assert _call(srv, "GET", "/files/")[0] == 404  # path needs at least one character
        assert _call(srv, "GET", "/one/a/b")[0] == 404  # plain {x} stays one segment
        assert _call(srv, "POST", "/files/x", body=b"{}")[0] == 405


def test_custom_regex_pattern_rejected():
    app = App()
    with pytest.raises(ValueError):
        app.route("GET", "/x/{id:(a+)+}")
    with pytest.raises(ValueError):
        app.route("GET", "/x/{id:[0-9]+}")


def test_after_hooks_get_request_status_seconds_and_errors_are_swallowed():
    app = App()
    seen = []

    @app.route("GET", "/ok")
    def ok(req):
        return JsonResponse({})

    @app.route("GET", "/deny")
    def deny(req):
        raise HTTPError(403, None, "no")

    @app.route("GET", "/boom")
    def boom(req):
        raise RuntimeError("x")

    @app.after
    def bad(req, status, seconds):
        raise RuntimeError("hook failure")

    @app.after
    def rec(req, status, seconds):
        seen.append((req.method, req.path, status, seconds))

    with Server(app, port=0) as srv:
        assert _call(srv, "GET", "/ok")[0] == 200
        assert _call(srv, "GET", "/deny")[0] == 403
        assert _call(srv, "GET", "/boom")[0] == 500
        assert _call(srv, "GET", "/nope")[0] == 404
    assert [(m, p, s) for m, p, s, _ in seen] == [("GET", "/ok", 200), ("GET", "/deny", 403),
                                                  ("GET", "/boom", 500), ("GET", "/nope", 404)]
    assert all(isinstance(t, float) and t >= 0 for *_, t in seen)


def _guarded(same_origin):
    app = App()
    app.browser_guard = True
    app.allow_same_origin = same_origin

    @app.route("GET", "/x")
    def x(req):
        return JsonResponse({"ok": True})

    @app.route("POST", "/x")
    def px(req):
        return JsonResponse({"ok": True})
    return app


def test_guard_default_still_rejects_every_origin():
    with Server(_guarded(False), port=0) as srv:
        o = f"http://127.0.0.1:{srv.port}"
        assert _call(srv, "GET", "/x", headers={"Origin": o})[0] == 403
        assert _call(srv, "GET", "/x")[0] == 200


def test_allow_same_origin_accepts_only_own_origin():
    with Server(_guarded(True), port=0) as srv:
        own = f"127.0.0.1:{srv.port}"
        js = {"Content-Type": "application/json"}
        assert _call(srv, "GET", "/x", headers={"Origin": f"http://{own}"})[0] == 200
        assert _call(srv, "POST", "/x", b"{}", {"Origin": f"http://{own}", **js})[0] == 200
        assert _call(srv, "POST", "/x", b"{}", {"Origin": f"http://{own}"})[0] == 415  # still JSON only
        for bad in ("http://evil.example", "null", f"https://{own}", f"http://{own}.evil.example",
                    f"http://127.0.0.1:{srv.port + 1}"):
            assert _call(srv, "GET", "/x", headers={"Origin": bad})[0] == 403, bad
        # rebinding: same Host and Origin, but a name that is not loopback -> Host check refuses
        assert _call(srv, "GET", "/x", headers={"Host": "evil.example", "Origin": "http://evil.example"})[0] == 403
