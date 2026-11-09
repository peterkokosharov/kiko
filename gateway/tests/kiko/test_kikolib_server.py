# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.server: routing, errors, SSE streaming, disconnects, graceful stop, serve() plumbing."""
import http.client
import json
import logging
import os
import signal
import sys
import threading
import time

import pytest

from kikolib import factory, posix, server
from kikolib.server import App, HTTPError, JsonResponse, Server, StreamResponse

T = 5.0


def make_app(**kw):
    app = App(**kw)

    @app.route("GET", "/hello")
    def hello(req):
        return JsonResponse({"hello": "world", "q": req.query, "h": req.headers.get("X-Foo")})

    @app.route("POST", "/echo")
    def echo(req):
        return JsonResponse({"got": req.json(), "len": len(req.body)}, status=201, headers={"X-Extra": "1"})

    @app.route("GET", "/models/{id}")
    def model(req):
        return JsonResponse({"id": req.params["id"]})

    @app.route("POST", "/v1beta/models/{id}:generateContent")
    def gen(req):
        return JsonResponse({"id": req.params["id"], "client": list(req.client)[0]})

    @app.route("GET", "/boom")
    def boom(req):
        raise RuntimeError("kaboom")

    @app.route("GET", "/denied")
    def denied(req):
        raise HTTPError(429, None, "slow down", {"Retry-After": "3"})

    @app.route("GET", "/port")
    def port(req):
        return JsonResponse({"port": req.client[1]})

    return app


@pytest.fixture
def srv():
    started = []

    def make(app=None, **kw):
        s = Server(app or make_app(), port=0, **kw).start()
        started.append(s)
        return s
    yield make
    for s in started:
        s.stop()


def conn(s):
    return http.client.HTTPConnection("127.0.0.1", s.port, timeout=T)


def call(s, method, path, body=None, headers=None):
    c = conn(s)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def test_json_route_query_headers(srv):
    s = srv()
    st, hd, body = call(s, "GET", "/hello?a=1&b=x%20y", headers={"x-FOO": "bar"})
    assert st == 200 and hd["Content-Type"] == "application/json"
    assert json.loads(body) == {"hello": "world", "q": {"a": "1", "b": "x y"}, "h": "bar"}
    assert int(hd["Content-Length"]) == len(body)


def test_post_json_status_and_headers(srv):
    st, hd, body = call(srv(), "POST", "/echo", b'{"a":[1]}', {"Content-Type": "application/json"})
    assert st == 201 and hd["X-Extra"] == "1"
    assert json.loads(body) == {"got": {"a": [1]}, "len": 9}


def test_path_params(srv):
    s = srv()
    assert json.loads(call(s, "GET", "/models/abc-1")[2]) == {"id": "abc-1"}
    st, _, body = call(s, "POST", "/v1beta/models/gemini-x:generateContent", b"{}")
    assert st == 200 and json.loads(body)["id"] == "gemini-x"
    assert call(s, "GET", "/models/a/b")[0] == 404


def test_404_405_shapes(srv):
    s = srv()
    st, _, body = call(s, "GET", "/nope")
    assert st == 404
    assert json.loads(body) == {"type": "error", "error": {"type": "not_found_error", "message": "no route for /nope"}}
    st, hd, body = call(s, "POST", "/hello", b"{}")
    assert st == 405 and hd["Allow"] == "GET"
    assert json.loads(body)["error"]["type"] == "invalid_request_error"


def test_411_when_no_content_length(srv):
    s = srv()
    c = conn(s)
    c.putrequest("POST", "/echo")
    c.endheaders()
    r = c.getresponse()
    assert r.status == 411 and json.loads(r.read())["type"] == "error"
    c.close()
    c = conn(s)
    c.putrequest("POST", "/echo")
    c.putheader("Transfer-Encoding", "chunked")
    c.endheaders()
    assert c.getresponse().status == 411
    c.close()


def test_413_and_bad_content_length(srv):
    s = srv(make_app(max_body=10))
    st, _, body = call(s, "POST", "/echo", b"x" * 100)
    assert st == 413 and json.loads(body)["error"]["type"] == "request_too_large"
    assert call(s, "POST", "/echo", b'{"a":1}')[0] == 201
    c = conn(s)
    c.putrequest("POST", "/echo")
    c.putheader("Content-Length", "abc")
    c.endheaders()
    assert c.getresponse().status == 400
    c.close()


def test_400_invalid_json(srv):
    st, _, body = call(srv(), "POST", "/echo", b"{not json")
    assert st == 400 and json.loads(body)["error"]["type"] == "invalid_request_error"


def test_http_error_shape_and_headers(srv):
    st, hd, body = call(srv(), "GET", "/denied")
    assert st == 429 and hd["Retry-After"] == "3"
    assert json.loads(body) == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}


def test_500_shape_and_log_has_no_secrets(srv, caplog):
    s = srv()
    with caplog.at_level(logging.ERROR, logger="kikolib.server"):
        st, _, body = call(s, "GET", "/boom?key=QUERYSECRET", headers={"Authorization": "Bearer HDRSECRET"})
    assert st == 500
    assert json.loads(body) == {"type": "error", "error": {"type": "api_error", "message": "internal server error"}}
    assert "kaboom" in caplog.text and "Traceback" in caplog.text
    assert "HDRSECRET" not in caplog.text and "QUERYSECRET" not in caplog.text


def test_keep_alive_reuses_connection(srv):
    s = srv()
    c = conn(s)
    ports = []
    for _ in range(3):
        c.request("GET", "/port")
        r = c.getresponse()
        ports.append(json.loads(r.read())["port"])
    c.close()
    assert len(set(ports)) == 1


def test_before_hook(srv):
    app = make_app()

    @app.before
    def auth(req):
        if req.path != "/hello" and req.headers.get("authorization") != "ok":
            raise HTTPError(401, None, "bad key")

    s = srv(app)
    assert call(s, "GET", "/hello")[0] == 200
    st, _, body = call(s, "GET", "/models/x")
    assert st == 401 and json.loads(body)["error"]["type"] == "authentication_error"
    assert call(s, "GET", "/models/x", headers={"Authorization": "ok"})[0] == 200
    assert call(s, "GET", "/missing")[0] == 404  # routing happens before the before-hooks


def test_deeply_nested_json_is_400():
    from kikolib.server import Headers, Request
    deep = "[" * 100000 + "]" * 100000
    with pytest.raises(HTTPError) as ei:
        Request("POST", "/x", {}, Headers(), deep.encode()).json()
    assert ei.value.status == 400


def stream_app(gen):
    app = App()

    @app.route("GET", "/s")
    def s(req):
        return StreamResponse(gen(req))
    return app


def test_sse_arrives_incrementally(srv):
    gate, finished = threading.Event(), threading.Event()

    def gen(req):
        yield b"event: a\ndata: 1\n\n"
        gate.wait(T)
        yield b"event: b\ndata: 2\n\n"
        finished.set()

    s = srv(stream_app(gen))
    c = conn(s)
    c.request("GET", "/s")
    r = c.getresponse()
    assert r.status == 200
    assert r.getheader("Content-Type") == "text/event-stream"
    assert r.getheader("Transfer-Encoding") == "chunked"
    assert r.readline() == b"event: a\n"
    assert r.readline() == b"data: 1\n"
    assert not finished.is_set() and not gate.is_set()  # handler is still running
    gate.set()
    assert r.read() == b"\nevent: b\ndata: 2\n\n"
    c.close()


def test_stream_then_keep_alive_reuse(srv):
    s = srv(stream_app(lambda req: iter([b"x", b"", b"y"])))
    c = conn(s)
    for _ in range(2):
        c.request("GET", "/s")
        assert c.getresponse().read() == b"xy"
    c.close()


def test_stream_error_before_first_chunk_is_real_error(srv):
    def gen(req):
        raise HTTPError(502, "api_error", "upstream down")
        yield b""

    st, _, body = call(srv(stream_app(gen)), "GET", "/s")
    assert st == 502 and json.loads(body)["error"]["message"] == "upstream down"


def test_client_disconnect_closes_iterator(srv):
    closed, started = threading.Event(), threading.Event()

    def gen(req):
        try:
            started.set()
            for _ in range(2000):
                yield b"x" * 4096
                time.sleep(0.005)
        finally:
            closed.set()

    s = srv(stream_app(gen))
    c = conn(s)
    c.request("GET", "/s")
    r = c.getresponse()
    r.read(10)
    assert started.is_set()
    r.close()
    c.close()  # both the response file and the socket must go for the TCP close to happen
    assert closed.wait(T)


def test_concurrent_streams(srv):
    barrier = threading.Barrier(3, timeout=T)

    def gen(req):
        yield b"a"
        barrier.wait()  # passes only if all three handlers run at the same time
        yield b"b"

    s = srv(stream_app(gen))
    out, errs = [], []

    def client():
        try:
            out.append(call(s, "GET", "/s")[2])
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=client) for _ in range(3)]
    [t.start() for t in ts]
    [t.join(T * 2) for t in ts]
    assert not errs and out == [b"ab"] * 3


def test_graceful_stop_drains_inflight(srv):
    entered, release = threading.Event(), threading.Event()
    app = App()

    @app.route("GET", "/slow")
    def slow(req):
        entered.set()
        release.wait(T)
        return JsonResponse({"done": True})

    s = srv(app)
    result = []
    t = threading.Thread(target=lambda: result.append(call(s, "GET", "/slow")))
    t.start()
    assert entered.wait(T)
    stopper = threading.Thread(target=s.stop)
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive()  # still waiting for the in-flight request
    release.set()
    t.join(T)
    stopper.join(T)
    assert not stopper.is_alive()
    assert result[0][0] == 200 and json.loads(result[0][2]) == {"done": True}
    assert result[0][1].get("Connection") == "close"


def test_idle_keepalive_does_not_block_stop(srv):
    s = srv()
    c = conn(s)
    c.request("GET", "/hello")
    c.getresponse().read()
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < T
    c.close()


def test_stop_without_start_does_not_hang():
    s = Server(make_app(), port=0)
    s.stop()


def test_factory_registers_server_threads():
    server._register()  # other tests may have cleared the registry
    rep = {r["kind"]: r for r in factory.report()}
    assert rep["server"]["chosen"] == "threads"
    assert factory.choose("server") is server.serve


# --- serve() plumbing ---

def run_serve(monkeypatch, notes, **kw):
    """Run serve() in a thread (no signal handlers there); returns (thread, holder with srv/exc)."""
    monkeypatch.setattr(posix, "sd_notify", lambda state: notes.append(state) or True)
    holder = {"srv": None, "exc": None}
    ready = threading.Event()

    def on_ready(s):
        holder["srv"] = s
        ready.set()

    def target():
        try:
            server.serve(make_app(), port=0, ready=on_ready, **kw)
        except SystemExit as e:
            holder["exc"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    assert ready.wait(T)
    return t, holder


def test_serve_ready_and_stop(monkeypatch):
    notes = []
    t, h = run_serve(monkeypatch, notes)
    assert notes[0] == "READY=1"
    assert call(h["srv"], "GET", "/hello")[0] == 200
    h["srv"].request_stop()
    t.join(T)
    assert not t.is_alive() and h["exc"] is None


def test_serve_watchdog(monkeypatch):
    notes = []
    monkeypatch.setattr(posix, "watchdog_interval", lambda: 0.01)
    t, h = run_serve(monkeypatch, notes)
    deadline = time.monotonic() + T
    while "WATCHDOG=1" not in notes and time.monotonic() < deadline:
        time.sleep(0.01)
    h["srv"].request_stop()
    t.join(T)
    assert "WATCHDOG=1" in notes


def test_serve_rss_limit_exits_with_restart_code(monkeypatch):
    notes = []
    monkeypatch.setattr(server, "RSS_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(posix, "rss_bytes", lambda: 10 * 1024 * 1024 * 1024)
    t, h = run_serve(monkeypatch, notes, rss_limit_mb=100)
    t.join(T)
    assert not t.is_alive()
    assert h["exc"] is not None and h["exc"].code == server.RSS_EXIT_CODE
    assert "STOPPING=1" in notes


def test_serve_rss_under_limit_keeps_running(monkeypatch):
    notes = []
    monkeypatch.setattr(server, "RSS_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(posix, "rss_bytes", lambda: 1024)
    t, h = run_serve(monkeypatch, notes, rss_limit_mb=100)
    time.sleep(0.1)
    assert t.is_alive()
    h["srv"].request_stop()
    t.join(T)
    assert h["exc"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM delivery to the own process is POSIX-only")
def test_serve_sigterm_graceful(monkeypatch):
    notes = []
    monkeypatch.setattr(posix, "sd_notify", lambda state: notes.append(state) or True)
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    def on_ready(s):
        threading.Thread(target=lambda: (time.sleep(0.05), os.kill(os.getpid(), signal.SIGTERM)),
                         daemon=True).start()

    try:
        server.serve(make_app(), port=0, ready=on_ready)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    assert notes == ["READY=1", "STOPPING=1"]


# --- review fixes ---

def test_body_read_timeout_closes_quietly(srv, caplog):
    import socket
    s = srv(make_app(), idle_timeout=0.3)
    with caplog.at_level(logging.DEBUG, logger="kikolib.server"):
        sk = socket.create_connection(("127.0.0.1", s.port), timeout=T)
        sk.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nabc")
        assert sk.recv(100) == b""  # server closed without a response
        sk.close()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_handler_headers_merge_case_insensitively(srv):
    app = App()

    @app.route("GET", "/j")
    def j(req):
        return JsonResponse({"a": 1}, headers={"content-type": "application/x-test", "Content-Length": "999"})

    @app.route("GET", "/s")
    def st(req):
        return StreamResponse([b"hi"], headers={"cache-control": "no-store", "Content-Length": "5",
                                                "transfer-encoding": "identity"})

    s = srv(app)
    c = conn(s)
    c.request("GET", "/j")
    r = c.getresponse()
    assert r.getheader("Content-Type") == "application/x-test"
    assert [k.lower() for k, _ in r.getheaders()].count("content-type") == 1
    assert r.read() == b'{"a":1}'
    c.request("GET", "/s")
    r = c.getresponse()
    assert r.getheader("Cache-Control") == "no-store"
    assert r.getheader("Content-Length") is None and r.getheader("Transfer-Encoding") == "chunked"
    assert r.read() == b"hi"
    c.close()


def test_crlf_in_response_header_is_500_without_values(srv, caplog):
    app = App()

    @app.route("GET", "/j")
    def j(req):
        return JsonResponse({}, headers={"X-A": "evil\r\nSet-Cookie: SECRETVAL=1"})

    @app.route("GET", "/s")
    def st(req):
        return StreamResponse([b"x"], headers={"X-B\r\nX": "v"})

    s = srv(app)
    with caplog.at_level(logging.ERROR, logger="kikolib.server"):
        for path in ("/j", "/s"):
            c = conn(s)
            c.request("GET", path)
            r = c.getresponse()
            assert r.status == 500 and r.getheader("Set-Cookie") is None
            assert json.loads(r.read())["error"]["type"] == "api_error"
            c.close()
    assert "SECRETVAL" not in caplog.text and "evil" not in caplog.text


def test_midstream_failure_truncates_chunked_stream(srv, caplog):
    def gen(req):
        yield b"first"
        raise RuntimeError("upstream died")

    s = srv(stream_app(gen))
    c = conn(s)
    with caplog.at_level(logging.ERROR, logger="kikolib.server"):
        c.request("GET", "/s")
        r = c.getresponse()
        assert r.status == 200
        with pytest.raises(http.client.IncompleteRead) as ei:
            r.read()
    assert ei.value.partial == b"first"
    assert "mid-stream" in caplog.text
    c.close()


def test_poll_hook_runs_in_accept_thread(srv):
    s = srv()
    hit = threading.Event()
    s.on_poll(hit.set)
    assert hit.wait(T)


def test_drain_timeout_warns(srv, caplog):
    entered, release = threading.Event(), threading.Event()
    app = App()

    @app.route("GET", "/slow")
    def slow(req):
        entered.set()
        release.wait(T)
        return JsonResponse({})

    s = srv(app, drain_seconds=0.1)
    t = threading.Thread(target=lambda: call(s, "GET", "/slow"))
    t.start()
    assert entered.wait(T)
    with caplog.at_level(logging.WARNING, logger="kikolib.server"):
        s.request_stop()
        assert s.drain() is False
    assert "drain timed out" in caplog.text
    release.set()
    t.join(T)


def test_rejected_request_with_large_body_gets_its_answer(srv):
    """#129: a 403/415/411-style early rejection leaves the body unread; the lingering close must not turn
    into a TCP reset that eats the response."""
    import socket
    app = make_app()
    app.browser_guard = True
    s = srv(app)
    body = b"x" * 600_000
    for hdr, want in (("Origin: http://evil.example\r\n", 403), ("Content-Type: text/plain\r\n", 415)):
        for _ in range(20):
            sk = socket.create_connection(("127.0.0.1", s.port), timeout=T)
            try:
                sk.sendall(("POST /echo HTTP/1.1\r\nHost: 127.0.0.1\r\n" + hdr +
                            f"Content-Length: {len(body)}\r\n\r\n").encode())
                try:
                    sk.sendall(body)
                except OSError:
                    pass  # the server may close while we still send: the answer must still be readable
                data = b""
                while True:
                    chunk = sk.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            finally:
                sk.close()
            assert data.startswith(b"HTTP/1.1 %d" % want), data[:80]
            assert data.endswith(b"}")
