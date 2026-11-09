# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite browser guard (home mode, auth=False): Content-Type, Origin, Host (DNS rebinding), connection cap."""
import http.client
import json
import socket
import time

import pytest

from kiko_lite import app as lite
from kikolib import server

BODY = json.dumps({"model": "fake/model-x", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]})
OK_REPLY = {"choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
JSON = {"Content-Type": "application/json"}
# The guard refuses (403/415/...) before reading the body and closes the connection. If body bytes are still in
# flight, the OS answers them with a TCP reset that can destroy the response before the client reads it (flaky
# under load). Tests that expect a rejection therefore send an empty body: nothing is left unread.
REJECTED = ""


@pytest.fixture
def env(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    hits = []
    up = server.App()

    @up.route("POST", "/v1/chat/completions")
    def chat(req):
        hits.append(req.json())
        return server.JsonResponse(OK_REPLY)

    upstream = server.Server(up, port=0).start()
    table = {"fake": {"base_url": upstream.url + "/v1", "kind": "openai", "key_names": ["K"], "auth": "bearer"}}
    resolver = lite.SimpleResolver(aliases={}, classes={}, env={}, providers=table)
    app = lite.build_app(resolver, None, providers=table, secret=lambda n: "sk", auth=False)
    srv = server.Server(app, port=0).start()

    class E:
        pass

    e = E()
    e.hits, e.srv = hits, srv

    def post(headers=None, body=BODY, path="/v1/messages", method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        try:
            c.request(method, path, body=body if method == "POST" else None, headers=headers or {})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    e.post = post
    yield e
    srv.stop()
    upstream.stop()


def test_plain_json_still_works(env):
    st, data = env.post(JSON)
    assert st == 200 and data["type"] == "message"
    assert len(env.hits) == 1


def test_charset_and_case_allowed(env):
    assert env.post({"Content-Type": "Application/JSON; charset=utf-8"})[0] == 200


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "application/jsonx", None])
def test_wrong_content_type_415(env, ctype):
    h = {"Content-Type": ctype} if ctype else {}
    st, data = env.post(h, body=REJECTED)
    assert st == 415 and data["error"]["type"] == "invalid_request_error"
    assert env.hits == []


def test_browser_request_blocked_without_upstream_call(env):
    st, data = env.post({"Content-Type": "text/plain", "Origin": "https://evil.example"}, body=REJECTED)
    assert st == 403 and data["error"]["type"] == "permission_error"
    assert env.hits == []


def test_origin_blocked_even_with_json(env):
    st, data = env.post({**JSON, "Origin": "http://localhost:3000"}, body=REJECTED)
    assert st == 403 and data["error"]["type"] == "permission_error"
    assert env.hits == []


def test_health_rejects_origin_but_is_open(env):
    assert env.post(method="GET", path="/health")[0] == 200
    assert env.post({"Origin": "https://evil.example"}, method="GET", path="/health")[0] == 403


def test_dns_rebinding_host_blocked(env):
    st, data = env.post({**JSON, "Host": "evil.example"}, body=REJECTED)
    assert st == 403 and data["error"]["type"] == "permission_error"
    assert env.hits == []
    assert env.post({**JSON, "Host": "evil.example:4000"}, body=REJECTED)[0] == 403
    assert env.post({**JSON, "Host": "127.0.0.1.evil.example"}, body=REJECTED)[0] == 403


@pytest.mark.parametrize("host", ["127.0.0.1:4000", "localhost:4000", "[::1]:4000", "localhost", "127.5.5.5", "[::1]",
                                  "LOCALHOST:80"])
def test_loopback_hosts_allowed(env, host):
    assert env.post({**JSON, "Host": host})[0] == 200


def _recv_all(sock, deadline=10.0):
    """Read until the server closes the connection (or the deadline): a split response is never mistaken."""
    end, data = time.monotonic() + deadline, b""
    while time.monotonic() < end:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def test_missing_host_http11_is_400(env):
    s = socket.create_connection(("127.0.0.1", env.srv.port), timeout=10)
    try:
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 0\r\n\r\n")
        assert _recv_all(s).startswith(b"HTTP/1.1 400")
    finally:
        s.close()


def _allowed(bound, host_header, allowed=()):
    h = server._HTTPServer.__new__(server._HTTPServer)
    h.server_address = (bound, 1)
    h.allowed_hosts = frozenset(allowed)
    return h.host_allowed(host_header)


def test_host_rules_by_bind_address():
    assert _allowed("127.0.0.1", "localhost:1") and not _allowed("127.0.0.1", "10.0.0.5")
    assert _allowed("10.0.0.5", "10.0.0.5:4000") and _allowed("10.0.0.5", "127.0.0.1")
    assert not _allowed("10.0.0.5", "evil.example") and not _allowed("10.0.0.5", "10.0.0.6")
    assert _allowed("10.0.0.5", "box.lan", allowed=["box.lan"])
    assert _allowed("0.0.0.0", "192.168.1.9:4000") and not _allowed("0.0.0.0", "evil.example")
    assert not _allowed("::1", "evil.example") and _allowed("::1", "[::1]:4000")


def test_connection_cap_503():
    app = server.App()
    app.route("GET", "/x")(lambda req: server.JsonResponse({}))
    srv = server.Server(app, port=0, max_connections=2).start()
    held = []
    try:
        for _ in range(2):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
            c.request("GET", "/x")  # answered => the server accepted it and holds a slot (keep-alive)
            r = c.getresponse()
            r.read()
            assert r.status == 200
            held.append(c)
        extra = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        try:
            # The server sends the 503 on accept, unprompted. Sending a request here would leave it unread when
            # the server closes, and the OS would then reset the connection and may drop the 503 (flaky).
            data = _recv_all(extra)
        finally:
            extra.close()
        assert data.startswith(b"HTTP/1.1 503") and b"overloaded_error" in data
        held.pop().close()  # the slot frees once the server thread sees the close: poll with a deadline
        status, end = 0, time.monotonic() + 20
        while status != 200 and time.monotonic() < end:
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
            try:
                c.request("GET", "/x")
                r = c.getresponse()
                r.read()
                status = r.status
            except (OSError, http.client.HTTPException):
                status = 0
            finally:
                c.close()
            if status != 200:
                time.sleep(0.02)
        assert status == 200
    finally:
        for c in held:
            c.close()
        srv.stop()
