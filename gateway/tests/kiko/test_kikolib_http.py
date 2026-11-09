# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.http against a real local server (127.0.0.1, no internet)."""
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kikolib import factory, http
from kikolib.http import ConnectError, HttpError, HttpTimeout, StdlibBackend

SECRET = "sk-very-secret-key"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/stale":
            self._send(200, b"{}")
            self.close_connection = True  # socket closed without "Connection: close"
        elif self.path == "/drop":  # read the request, then hang up without answering
            self.server.drops += 1
            self.close_connection = True
        elif self.path.split("?")[0] == "/slow":
            time.sleep(1.5)
            self._send(200, b"{}")
        elif self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b"data: one\r\n\r\n", b"data: tw", b"o\n\n", b"data: [DONE]\n\n"):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/ua":
            self._send(200, json.dumps({"ua": self.headers["User-Agent"],
                                        "ae": self.headers["Accept-Encoding"]}).encode(),
                       extra=[("X-Mixed-Case", "yes")])
        else:
            self._send(200, json.dumps({"path": self.path}).encode(), extra=[("X-Mixed-Case", "yes")])

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(201, json.dumps({"echo": json.loads(data)}).encode())


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.connections = 0
        self.drops = 0

    def get_request(self):
        r = super().get_request()
        self.connections += 1
        return r


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "KIKO_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def server():
    srv = Server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def backend():
    b = StdlibBackend()
    yield b
    b.close()


def url(srv, path="/"):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def test_get_and_post_json(server, backend):
    r = backend.request("GET", url(server, "/x?a=1"))
    assert (r.status, r.reason) == (200, "OK")
    assert r.json() == {"path": "/x?a=1"}
    r = backend.request("POST", url(server), {"Content-Type": "application/json"}, b'{"k": [1, 2]}')
    assert r.status == 201 and r.json() == {"echo": {"k": [1, 2]}}


def test_default_headers_and_case_insensitive_lookup(server, backend):
    r = backend.request("GET", url(server, "/ua"))
    seen = r.json()
    assert seen["ua"].startswith("kiko-lite/") and seen["ae"] == "identity"
    assert r.headers.get("x-mixed-case") == "yes"
    assert r.headers["X-MIXED-CASE"] == "yes" and "content-TYPE" in r.headers
    assert r.headers.get("nope") is None
    r2 = backend.request("GET", url(server, "/ua"), {"user-agent": "mine"})
    assert r2.json()["ua"] == "mine"


def test_keepalive_reuses_connection(server, backend):
    for _ in range(4):
        assert backend.request("GET", url(server)).status == 200
    assert server.connections == 1


def test_stale_connection_is_retried_once(server, backend):
    assert backend.request("GET", url(server, "/stale")).status == 200
    time.sleep(0.2)  # server has closed its side
    r = backend.request("GET", url(server, "/ok"))
    assert r.status == 200 and r.json() == {"path": "/ok"}
    assert server.connections == 2


def test_sse_streaming_lines_and_pool_return(server, backend):
    with backend.request("GET", url(server, "/sse"), stream=True) as r:
        assert r.headers.get("transfer-encoding") == "chunked"
        lines = list(r.iter_lines())
    assert lines == ["data: one", "", "data: two", "", "data: [DONE]", ""]
    backend.request("GET", url(server)).read()
    assert server.connections == 1  # connection went back to the pool after full read


def test_unfinished_stream_close_discards_connection(server, backend):
    r = backend.request("GET", url(server, "/sse"), stream=True)
    next(r.iter_lines())
    r.close()
    backend.request("GET", url(server)).read()
    assert server.connections == 2


def test_read_timeout(server):
    b = StdlibBackend()
    with pytest.raises(HttpTimeout):
        b.request("GET", url(server, "/slow"), timeout=0.3)
    b.close()


def test_refused_port_is_connect_error(backend):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConnectError) as ei:
        backend.request("GET", f"http://127.0.0.1:{port}/", {"Authorization": f"Bearer {SECRET}"})
    assert isinstance(ei.value, HttpError)


def test_bad_url_and_no_headers_or_query_in_errors(backend):
    with pytest.raises(HttpError):
        backend.request("GET", "ftp://example.com/")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConnectError) as ei:
        backend.request("GET", f"http://127.0.0.1:{port}/p?key={SECRET}", {"Authorization": f"Bearer {SECRET}"})
    assert SECRET not in str(ei.value) + repr(ei.value)
    t = StdlibBackend()
    srv = Server()
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        with pytest.raises(HttpTimeout) as ei2:
            t.request("GET", url(srv, f"/slow?key={SECRET}"), {"X-Api-Key": SECRET}, timeout=0.2)
        assert SECRET not in str(ei2.value) + repr(ei2.value)
    finally:
        srv.shutdown()
        srv.server_close()
        t.close()


def test_response_repr_has_no_headers(server, backend):
    r = backend.request("GET", url(server))
    assert repr(r) == "<Response 200 OK>"


def test_threads_share_backend(server, backend):
    out = []

    def work(i):
        out.append(backend.request("GET", url(server, f"/t{i}")).json()["path"])

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == sorted(f"/t{i}" for i in range(8))


def test_factory_report_shows_stdlib(monkeypatch):
    monkeypatch.setenv("KIKO_HTTP_IMPL", "stdlib")  # winhttp (priority 5) wins on Windows otherwise
    http._register()  # idempotent; keeps other kinds untouched
    factory.reset()
    try:
        rep = {e["kind"]: e for e in factory.report()}
        assert rep["http"]["chosen"] == "stdlib"
        assert {"name": "stdlib", "ok": True, "reason": "http.client + ssl"} in rep["http"]["candidates"]
        assert isinstance(http.client(), StdlibBackend)
    finally:
        factory.reset()


def _no_secret(exc):
    seen = []
    e = exc
    while e is not None and e not in seen:
        seen.append(e)
        assert SECRET not in str(e) + repr(e) + repr(e.args)
        e = e.__cause__ or e.__context__
    assert exc.__cause__ is None and exc.__context__ is None


def test_invalid_header_value_does_not_leak_secret(server, backend):
    with pytest.raises(HttpError) as ei:
        backend.request("GET", url(server), {"Authorization": "Bearer " + SECRET + "\n"})
    assert str(ei.value) == "invalid request header or URL"
    _no_secret(ei.value)
    with pytest.raises(HttpError) as ei:
        backend.request("GET", url(server), {"X-Key": SECRET + "é中"})
    _no_secret(ei.value)


def test_response_lost_is_not_resent_fresh_connection(server, backend):
    with pytest.raises(http.ConnectionLost):
        backend.request("GET", url(server, "/drop"))
    assert server.drops == 1


def test_response_lost_is_not_resent_on_reused_connection(server, backend):
    assert backend.request("GET", url(server)).status == 200
    with pytest.raises(http.ConnectionLost):
        backend.request("GET", url(server, "/drop"))
    assert server.drops == 1  # no silent resend (would double-bill a provider)
    assert server.connections == 1


def test_dead_pooled_connection_is_detected_before_reuse(server, backend):
    backend.request("GET", url(server, "/stale")).read()
    time.sleep(0.2)
    conns = list(backend._idle.values())[0]
    assert http._is_dropped(conns[0][0])
    assert backend.request("GET", url(server, "/ok")).status == 200


def test_live_pooled_connection_is_not_dropped(server, backend):
    backend.request("GET", url(server)).read()
    assert not http._is_dropped(list(backend._idle.values())[0][0][0])


def test_cert_verify_error_is_not_retryable_class():
    assert not issubclass(http.TLSVerifyError, ConnectError)
    e = StdlibBackend._map_error(ssl.SSLCertVerificationError(1, "secret-ish detail"), "h", 443, "connect")
    assert isinstance(e, http.TLSVerifyError) and "detail" not in str(e)


def test_long_line_is_not_split_and_cap_raises(monkeypatch):
    import io

    big = "x" * (3 << 20)
    r = http.Response(200, "OK", http.Headers([]), io.BytesIO(f"data: {big}\r\nnext\n".encode()))
    assert list(r.iter_lines()) == [f"data: {big}", "next"]
    monkeypatch.setattr(http, "MAX_LINE", 1 << 20)
    r = http.Response(200, "OK", http.Headers([]), io.BytesIO(b"y" * (3 << 20)))
    with pytest.raises(HttpError):
        list(r.iter_lines())


def test_proxy_auth_not_in_repr():
    assert "Basic" not in repr(http.Proxy("h", 1, "Basic c2VjcmV0"))


def test_ca_context_verifies():
    ctx = http.ca_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_ca_bundle_env_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("KIKO_CA_BUNDLE", str(tmp_path / "missing.pem"))
    with pytest.raises(OSError):
        http.ca_context()


def test_proxy_env_parsing(monkeypatch):
    assert http.proxy_for("https", "api.example.com") is None
    monkeypatch.setenv("HTTPS_PROXY", "http://user:p%40ss@proxy.local:3128")
    p = http.proxy_for("https", "api.example.com")
    assert (p.host, p.port) == ("proxy.local", 3128) and p.auth.startswith("Basic ")
    assert http.proxy_for("http", "api.example.com") is None
    monkeypatch.setenv("HTTP_PROXY", "proxy2.local")
    assert http.proxy_for("http", "x.org").port == 80
    monkeypatch.setenv("NO_PROXY", "internal.example.com,.corp")
    assert http.proxy_for("https", "internal.example.com") is None
    assert http.proxy_for("https", "svc.corp") is None
    assert http.proxy_for("https", "api.example.com") is not None
    monkeypatch.setenv("NO_PROXY", "*")
    assert http.proxy_for("https", "api.example.com") is None


def test_https_via_proxy_uses_tunnel(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    b = StdlibBackend()
    c = b._new_conn("https", "api.example.com", 443, http.proxy_for("https", "api.example.com"))
    assert (c.host, c.port) == ("proxy.local", 3128)
    assert c._tunnel_host == "api.example.com" and c._tunnel_port == 443


def test_abort_from_another_thread_wakes_blocked_read(backend):
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(2)
    port = ls.getsockname()[1]
    accepted = []

    def serve():
        c, _ = ls.accept()
        accepted.append(c)
        c.recv(65536)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")

    threading.Thread(target=serve, daemon=True).start()
    try:
        r = backend.request("GET", f"http://127.0.0.1:{port}/", timeout=60, stream=True)
        out = []

        def reader():
            try:
                list(r.iter_lines())
                out.append("eof")
            except http.HttpError as e:
                out.append(e)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive()  # blocked on the silent socket
        t0 = time.monotonic()
        r.abort()
        t.join(3)
        assert not t.is_alive() and time.monotonic() - t0 < 2.0
        assert len(out) == 1 and isinstance(out[0], http.ConnectionLost)
        r.close()
        assert sum(len(v) for v in backend._idle.values()) == 0  # never returned to the pool
    finally:
        for c in accepted:
            c.close()
        ls.close()


def test_abort_after_completion_is_noop_and_connection_stays_reusable(server, backend):
    r = backend.request("GET", url(server, "/sse"), stream=True)
    assert list(r.iter_lines())[-2] == "data: [DONE]"
    r.abort()  # finished: its socket is back in the pool, must not be touched
    assert sum(len(v) for v in backend._idle.values()) == 1
    assert backend.request("GET", url(server, "/ok")).json() == {"path": "/ok"}
    assert server.connections == 1  # the pooled connection was reused


def test_abort_of_finished_response_does_not_break_a_concurrent_request(server, backend):
    r1 = backend.request("GET", url(server, "/sse"), stream=True)
    list(r1.iter_lines())  # connection returned to the pool
    r2 = backend.request("GET", url(server, "/sse"), stream=True)  # checks the same connection out
    lines = r2.iter_lines()
    assert next(lines) == "data: one"
    r1.abort()  # must not shut down the socket r2 is using
    assert list(lines)[-2] == "data: [DONE]"
    assert server.connections == 1
