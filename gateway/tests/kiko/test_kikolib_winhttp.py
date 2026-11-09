# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.winhttp: the ctypes layer is replaced by a fake (runs on any OS); one live loopback test on Windows."""
import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kikolib import factory, http, winhttp
from kikolib.http import ConnectError, ConnectionLost, HttpError, HttpTimeout, TLSVerifyError
from kikolib.winhttp import WinHttpBackend, WinHttpError

SECRET = "sk-very-secret-key"


class FakeApi:
    """Stands in for winhttp._Api: records calls, serves a canned response, can fail a stage."""

    def __init__(self, head="HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nX-Mixed-Case: yes\r\n",
                 chunks=(b"hello",), fail=None):
        self.head = head
        self.chunks = deque(chunks)
        self.fail = dict(fail or {})  # stage -> error code (int) or list of codes (consumed in order)
        self.next = 100
        self.opened: dict[int, str] = {}
        self.closed: list[int] = []
        self.calls: list[tuple] = []
        self.sent_headers, self.body = "", b""
        self.opts: list[tuple] = []

    def _h(self, kind):
        self.next += 1
        self.opened[self.next] = kind
        return self.next

    def _maybe(self, stage):
        f = self.fail.get(stage)
        if isinstance(f, list):
            f = f.pop(0) if f else None
        if f:
            raise WinHttpError(f)

    def open(self, agent, access, proxy):
        self.calls.append(("open", access, proxy))
        self._maybe("open")
        return self._h("session")

    def connect(self, session, host, port):
        self.calls.append(("connect", host, port))
        self._maybe("connect")
        return self._h("connect")

    def open_request(self, conn, method, target, secure):
        self.calls.append(("open_request", method, target, secure))
        return self._h("request")

    def set_timeouts(self, h, *ms):
        self.calls.append(("timeouts",) + ms)

    def set_option_dword(self, h, option, value):
        self.opts.append((option, value))
        if option == winhttp._OPT_SECURE_PROTOCOLS and value & winhttp._PROTO_TLS13 and "tls13" in self.fail:
            raise WinHttpError(87)

    def set_option_str(self, h, option, value):
        self.opts.append((option, value))

    def send(self, req, headers, total):
        self.sent_headers, self.total, self.body = headers, total, b""
        self._maybe("send")

    def write(self, req, data):
        self.body += data

    @property
    def sent(self):
        return self.sent_headers, (self.body or None)

    def receive(self, req):
        self._maybe("receive")

    def raw_headers(self, req):
        return self.head

    def data_available(self, req):
        self._maybe("read")
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, req, size):
        return self.chunks.popleft() if self.chunks else b""

    def close(self, h):
        self.closed.append(h)

    def all_closed(self):
        return set(self.closed) == set(self.opened) and len(self.closed) == len(self.opened)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
              "KIKO_CA_BUNDLE", "KIKO_HTTP_IMPL"):
        monkeypatch.delenv(v, raising=False)
    factory.reset()
    yield
    factory.reset()


def fake(monkeypatch, **kw):
    api = FakeApi(**kw)
    monkeypatch.setattr(winhttp, "_api", lambda: api)
    return api


# --- probe and registration -------------------------------------------------------------------

def _winver(monkeypatch, major, build):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion",
                        lambda: type("V", (), {"major": major, "minor": 0, "build": build})(), raising=False)


def test_probe_not_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert winhttp._probe() == (False, "not Windows")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert winhttp._probe()[0] is False


def test_probe_old_windows(monkeypatch):
    _winver(monkeypatch, 6, 9600)
    fake(monkeypatch)
    ok, reason = winhttp._probe()
    assert not ok and "need 10" in reason


@pytest.mark.parametrize("build,tag", [(19045, "w10"), (21999, "w10"), (22000, "w11"), (26200, "w11")])
def test_probe_ok_names_generation_and_build(monkeypatch, build, tag):
    _winver(monkeypatch, 10, build)
    fake(monkeypatch)
    ok, reason = winhttp._probe()
    assert ok and tag in reason and str(build) in reason


def test_probe_dll_missing(monkeypatch):
    _winver(monkeypatch, 10, 22631)

    def boom():
        raise OSError("no dll")

    monkeypatch.setattr(winhttp, "_api", boom)
    ok, reason = winhttp._probe()
    assert not ok and "winhttp.dll" in reason


def test_probe_ca_bundle_prefers_stdlib(monkeypatch):
    _winver(monkeypatch, 10, 22631)
    fake(monkeypatch)
    monkeypatch.setenv("KIKO_CA_BUNDLE", "/some/ca.pem")
    ok, reason = winhttp._probe()
    assert not ok and "KIKO_CA_BUNDLE" in reason and "stdlib" in reason


def test_registered_before_stdlib():
    winhttp.register()
    http._register()
    rep = {e["kind"]: e for e in factory.report()}["http"]
    names = [c["name"] for c in rep["candidates"]]
    assert names == ["winhttp", "stdlib"]
    assert factory._impls["http"]["winhttp"]["priority"] == 5
    assert factory._impls["http"]["stdlib"]["priority"] == 10


def test_factory_picks_winhttp_when_probe_ok_and_stdlib_when_forced(monkeypatch):
    _winver(monkeypatch, 10, 22631)
    fake(monkeypatch)
    winhttp.register()
    http._register()
    assert isinstance(factory.choose("http"), WinHttpBackend)
    factory.reset()
    monkeypatch.setenv("KIKO_HTTP_IMPL", "stdlib")
    assert isinstance(factory.choose("http"), http.StdlibBackend)


def test_factory_falls_back_to_stdlib_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    winhttp.register()
    http._register()
    assert isinstance(factory.choose("http"), http.StdlibBackend)
    rep = {e["kind"]: e for e in factory.report()}["http"]
    assert rep["chosen"] == "stdlib"
    assert rep["candidates"][0] == {"name": "winhttp", "ok": False, "reason": "not Windows"}


def test_import_is_safe_without_windows_dll():
    # winhttp.dll is only touched by _Api(); importing the module never loads it
    assert winhttp._api_obj is None or sys.platform == "win32"


# --- request / response mapping ----------------------------------------------------------------

def test_get_maps_status_headers_body(monkeypatch):
    api = fake(monkeypatch)
    be = WinHttpBackend(user_agent="ua/1")
    r = be.request("GET", "http://example.test:8080/a/b?x=1", headers={"X-Api": "v"})
    assert (r.status, r.reason) == (200, "OK")
    assert r.read() == b"hello" and r.read() == b"hello"
    assert r.headers["content-type"] == "text/plain"
    assert r.headers["X-Mixed-Case"] == "yes"
    assert ("connect", "example.test", 8080) in api.calls
    assert ("open_request", "GET", "/a/b?x=1", False) in api.calls
    head, body = api.sent
    assert "User-Agent: ua/1\r\n" in head and "Accept-Encoding: identity\r\n" in head and "X-Api: v\r\n" in head
    assert body is None
    assert api.all_closed() is False  # the session stays open for reuse
    be.close()
    assert api.all_closed()


def test_https_flag_default_port_and_caller_header_wins(monkeypatch):
    api = fake(monkeypatch)
    be = WinHttpBackend()
    be.request("GET", "https://example.test/", headers={"user-agent": "mine"})
    assert ("connect", "example.test", 443) in api.calls
    assert ("open_request", "GET", "/", True) in api.calls
    assert "mine" in api.sent[0] and "kiko-lite" not in api.sent[0]


def test_post_body_and_redirects_cookies_disabled(monkeypatch):
    api = fake(monkeypatch, head="HTTP/1.1 302 Found\r\nLocation: /x\r\n")
    r = WinHttpBackend().request("POST", "http://example.test/p", body=b'{"a":1}')
    assert r.status == 302 and r.headers["location"] == "/x"  # redirects are not followed
    assert api.sent[1] == b'{"a":1}'
    assert (winhttp._OPT_DISABLE_FEATURE, winhttp._DISABLE_REDIRECTS | winhttp._DISABLE_COOKIES) in api.opts


def test_http2_status_line_and_empty_reason(monkeypatch):
    fake(monkeypatch, head="HTTP/2 404 \r\ncontent-type: application/json\r\n", chunks=(b"{}",))
    r = WinHttpBackend().request("GET", "https://example.test/")
    assert r.status == 404 and r.json() == {}


def test_timeouts_in_ms(monkeypatch):
    api = fake(monkeypatch)
    WinHttpBackend(connect_timeout=3).request("GET", "http://example.test/", timeout=7.5)
    assert ("timeouts", 3000, 3000, 7500, 7500) in api.calls


def test_session_reused_and_tls_floor_set(monkeypatch):
    api = fake(monkeypatch)
    be = WinHttpBackend()
    for _ in range(3):
        be.request("GET", "http://example.test/")
    assert [c for c in api.calls if c[0] == "open"] == [("open", winhttp._ACCESS_AUTOMATIC_PROXY, None)]
    assert (winhttp._OPT_SECURE_PROTOCOLS, winhttp._PROTO_TLS12 | winhttp._PROTO_TLS13) in api.opts
    # every per-request handle (connect + request) was closed; only the session is left
    assert [api.opened[h] for h in set(api.opened) - set(api.closed)] == ["session"]


def test_tls13_flag_unsupported_falls_back_to_tls12(monkeypatch):
    api = fake(monkeypatch, fail={"tls13": 1})
    WinHttpBackend().request("GET", "http://example.test/")
    assert (winhttp._OPT_SECURE_PROTOCOLS, winhttp._PROTO_TLS12) in api.opts


def test_thread_safe_single_session(monkeypatch):
    api = fake(monkeypatch)
    be = WinHttpBackend()
    ts = [threading.Thread(target=lambda: be.request("GET", "http://example.test/")) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len([c for c in api.calls if c[0] == "open"]) == 1


def test_bad_inputs(monkeypatch):
    fake(monkeypatch)
    be = WinHttpBackend()
    for url in ("ftp://example.test/", "http:///x", "nonsense"):
        with pytest.raises(HttpError):
            be.request("GET", url)
    with pytest.raises(HttpError):
        be.request("GET", "http://example.test/", headers={"X": "a\r\nInjected: 1"})
    with pytest.raises(HttpError):
        be.request("GE T", "http://example.test/")


def test_unavailable_dll(monkeypatch):
    def boom():
        raise OSError("x")

    monkeypatch.setattr(winhttp, "_api", boom)
    with pytest.raises(HttpError):
        WinHttpBackend().request("GET", "http://example.test/")


# --- proxy -------------------------------------------------------------------------------------

def test_env_proxy_named(monkeypatch):
    api = fake(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy.test:3128")
    WinHttpBackend().request("GET", "https://example.test/")
    assert ("open", winhttp._ACCESS_NAMED_PROXY, "proxy.test:3128") in api.calls
    assert (winhttp._OPT_PROXY_USERNAME, "user") in api.opts and (winhttp._OPT_PROXY_PASSWORD, "pw") in api.opts


def test_no_proxy_goes_direct_and_other_scheme_uses_system(monkeypatch):
    api = fake(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("NO_PROXY", "internal.test")
    be = WinHttpBackend()
    be.request("GET", "https://internal.test/")
    be.request("GET", "http://example.test/")  # no HTTP_PROXY: Windows settings decide
    modes = [c[1] for c in api.calls if c[0] == "open"]
    assert modes == [winhttp._ACCESS_NO_PROXY, winhttp._ACCESS_AUTOMATIC_PROXY]


# --- streaming ---------------------------------------------------------------------------------

def test_stream_lines_incremental_and_handles_closed(monkeypatch):
    api = fake(monkeypatch, head="HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n",
               chunks=(b"data: one\r\n\r\n", b"data: tw", b"o\n\n", b"data: [DONE]\n\n"))
    be = WinHttpBackend()
    r = be.request("GET", "http://example.test/sse", stream=True)
    assert api.chunks  # nothing was read yet
    it = r.iter_lines()
    assert next(it) == "data: one"
    assert len(api.chunks) == 3  # only the first chunk was pulled
    assert list(it) == ["", "data: two", "", "data: [DONE]", ""]
    be.close()
    assert api.all_closed()


def test_stream_close_early_closes_handles(monkeypatch):
    api = fake(monkeypatch, chunks=(b"a\n", b"b\n"))
    be = WinHttpBackend()
    with be.request("GET", "http://example.test/", stream=True) as r:
        assert next(r.iter_lines()) == "a"
    be.close()
    assert api.all_closed()


def test_stream_line_longer_than_limit_is_split_not_lost(monkeypatch):
    big = b"x" * 70000 + b"\n"
    fake(monkeypatch, chunks=(big[:65536], big[65536:]))
    r = WinHttpBackend().request("GET", "http://example.test/", stream=True)
    assert list(r.iter_lines()) == ["x" * 70000]


def test_stream_read_error_mid_body(monkeypatch):
    api = fake(monkeypatch, chunks=(b"a\n", b"b\n"), fail={"read": [None, 12002]})
    be = WinHttpBackend()
    r = be.request("GET", "http://example.test/", stream=True)
    it = r.iter_lines()
    assert next(it) == "a"
    with pytest.raises(HttpTimeout):
        next(it)
    be.close()
    assert api.all_closed()


def test_abort_wakes_reader(monkeypatch):
    api = fake(monkeypatch, chunks=(b"a\n",))
    be = WinHttpBackend()
    r = be.request("GET", "http://example.test/", stream=True)
    r.abort()
    with pytest.raises(ConnectionLost):
        r.read()
    r.close()
    be.close()
    assert api.all_closed()
    assert len(api.closed) == len(set(api.closed))  # no handle closed twice


# --- errors ------------------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["connect", "send", "receive"])
@pytest.mark.parametrize("code,cls", [
    (12002, HttpTimeout), (12007, ConnectError), (12029, ConnectError), (12030, ConnectionLost),
    (12175, TLSVerifyError), (12045, TLSVerifyError), (12038, TLSVerifyError), (12157, ConnectError),
    (99999, HttpError),
])
def test_error_mapping_closes_handles_and_hides_secrets(monkeypatch, stage, code, cls):
    api = fake(monkeypatch, fail={stage: code})
    be = WinHttpBackend()
    with pytest.raises(cls) as ei:
        be.request("POST", f"https://example.test:8443/v1?key={SECRET}", headers={"Authorization": SECRET},
                   body=SECRET.encode())
    exc = ei.value
    assert type(exc) is cls or cls is HttpError
    text = str(exc) + repr(exc) + repr(exc.args)
    assert SECRET not in text and "key=" not in text and "Authorization" not in text
    assert "example.test:8443" in text
    assert exc.__cause__ is None and exc.__context__ is None
    be.close()
    assert api.all_closed()


def test_error_in_session_open_closes_nothing_leaked(monkeypatch):
    api = fake(monkeypatch, fail={"open": 12178})
    with pytest.raises(ConnectError):
        WinHttpBackend().request("GET", "http://example.test/")
    assert api.all_closed()


def test_resend_requested_is_retried_once(monkeypatch):
    api = fake(monkeypatch, fail={"receive": [12032]})
    r = WinHttpBackend().request("GET", "http://example.test/")
    assert r.status == 200
    assert len([c for c in api.calls if c[0] == "connect"]) == 2


def test_resend_twice_is_an_error(monkeypatch):
    fake(monkeypatch, fail={"receive": [12032, 12032]})
    with pytest.raises(HttpError):
        WinHttpBackend().request("GET", "http://example.test/")


def test_malformed_status_line(monkeypatch):
    api = fake(monkeypatch, head="garbage\r\n")
    be = WinHttpBackend()
    with pytest.raises(HttpError):
        be.request("GET", "http://example.test/")
    be.close()
    assert api.all_closed()


# --- live (Windows only): real WinHTTP against a loopback server ---------------------------------

class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i, part in enumerate((b"data: one\n\n", b"data: two\n\n")):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.flush()
                if i == 0:
                    self.server.first_sent.set()
                    self.server.release.wait(10)
            self.wfile.write(b"0\r\n\r\n")
            return
        body = json.dumps({"path": self.path, "ua": self.headers["User-Agent"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        body = json.dumps({"len": n, "echo": data.decode(), "auth": self.headers["Authorization"]}).encode()
        self.send_response(201, "Made")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.skipif(sys.platform != "win32", reason="WinHTTP exists only on Windows")
def test_live_loopback_get_post_stream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    srv.first_sent = threading.Event()
    srv.release = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    be = WinHttpBackend(user_agent="kiko-test/1")
    try:
        r = be.request("GET", base + "/hello?a=1")
        assert r.status == 200 and r.json() == {"path": "/hello?a=1", "ua": "kiko-test/1"}
        assert r.headers["content-type"] == "application/json"

        payload = json.dumps({"k": "v" * 5000})
        r = be.request("POST", base + "/p", headers={"Authorization": "Bearer t"}, body=payload.encode())
        assert (r.status, r.reason) == (201, "Made")
        assert r.json() == {"len": len(payload), "echo": payload, "auth": "Bearer t"}

        r = be.request("GET", base + "/sse", stream=True)
        it = r.iter_lines()
        t0 = time.monotonic()
        assert next(it) == "data: one"  # arrives while the server still holds the stream open
        assert srv.first_sent.is_set() and not srv.release.is_set() and time.monotonic() - t0 < 5
        srv.release.set()
        assert [x for x in it if x] == ["data: two"]

        with pytest.raises(ConnectError):
            be.request("GET", "http://127.0.0.1:1/", timeout=5)
    finally:
        srv.release.set()
        be.close()
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(sys.platform != "win32", reason="WinHTTP exists only on Windows")
def test_live_probe_ok_on_this_windows():
    ok, reason = winhttp._probe()
    assert ok and ("w10" in reason or "w11" in reason)


# --- own deadline (WinHTTP's timers are too coarse) -------------------------------------------------

class BlockingApi(FakeApi):
    """A call that blocks until its handle is closed, like WinHTTP does."""

    def __init__(self, block, **kw):
        super().__init__(**kw)
        self.block = block
        self.woke = threading.Event()

    def _block(self, stage):
        if self.block == stage:
            assert self.woke.wait(5), "watchdog never closed the handle"
            raise WinHttpError(12017)

    def receive(self, req):
        self._block("receive")

    def data_available(self, req):
        self._block("read")
        return super().data_available(req)

    def close(self, h):
        super().close(h)
        if self.opened.get(h) == "request":
            self.woke.set()


def test_head_timeout_is_ours_and_prompt(monkeypatch):
    api = BlockingApi("receive")
    monkeypatch.setattr(winhttp, "_api", lambda: api)
    be = WinHttpBackend()
    t0 = time.monotonic()
    with pytest.raises(HttpTimeout):
        be.request("GET", "http://example.test/", timeout=0.2)
    assert time.monotonic() - t0 < 3
    be.close()
    assert api.all_closed()


def test_stream_silence_timeout(monkeypatch):
    api = BlockingApi("read", chunks=(b"a\n",))
    monkeypatch.setattr(winhttp, "_api", lambda: api)
    be = WinHttpBackend()
    r = be.request("GET", "http://example.test/", stream=True, timeout=0.2)
    with pytest.raises(HttpTimeout):
        list(r.iter_lines())
    be.close()
    assert api.all_closed()


@pytest.mark.skipif(sys.platform != "win32", reason="WinHTTP exists only on Windows")
def test_live_short_timeout_is_prompt():
    class Slow(_H):
        def do_GET(self):
            if self.path == "/slow":
                time.sleep(1.5)
            super().do_GET()

        def do_POST(self):
            time.sleep(1.5)
            super().do_POST()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    be = WinHttpBackend()
    try:
        assert be.request("GET", f"http://127.0.0.1:{srv.server_address[1]}/fast").status == 200  # warms the host
        t0 = time.monotonic()
        with pytest.raises(HttpTimeout):  # no body: WinHTTP blocks send() until the head, so send() is limited too
            be.request("GET", f"http://127.0.0.1:{srv.server_address[1]}/slow", timeout=0.3)
        assert time.monotonic() - t0 < 1.4
        t0 = time.monotonic()
        with pytest.raises(HttpTimeout):  # a POST with a body: the wait for the head is still ours to limit
            be.request("POST", f"http://127.0.0.1:{srv.server_address[1]}/x", body=b"{}", timeout=0.3)
        assert time.monotonic() - t0 < 1.4
    finally:
        be.close()
        srv.shutdown()
        srv.server_close()


def test_large_body_is_written_in_chunks(monkeypatch):
    api = fake(monkeypatch)
    data = bytes(range(256)) * 700  # 179200 bytes: three writes
    WinHttpBackend().request("POST", "http://example.test/", body=data)
    assert api.total == len(data) and api.body == data


def test_bodyless_send_blocking_on_a_warm_host_uses_the_short_limit(monkeypatch):
    api = BlockingApi("send")
    api.send = lambda req, headers, total: api._block("send")
    monkeypatch.setattr(winhttp, "_api", lambda: api)
    be = WinHttpBackend()
    be._warm[("example.test", 80)] = time.monotonic()
    t0 = time.monotonic()
    with pytest.raises(HttpTimeout):
        be.request("GET", "http://example.test/", timeout=0.2)
    assert time.monotonic() - t0 < 3
