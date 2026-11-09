# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Cancelling a streamed request must never block the server's request thread, even when the upstream socket
is silent (200 + SSE headers, then no bytes) and upstream_timeout is large. Fully offline (127.0.0.1)."""
import json
import socket
import threading
import time

import pytest

from kiko_lite import app as lite
from kikolib import http as khttp
from kikolib import retry, server

MASTER = "master-key"


class SilentUpstream:
    """Raw TCP server: reads the request, answers 200 + SSE headers, then says nothing and keeps the socket open."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.sent = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
            head, _, rest = buf.partition(b"\r\n\r\n")
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":")[1])
            while len(rest) < n:
                rest += c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                      b"Transfer-Encoding: chunked\r\n\r\n")
            self.sent.set()
        except OSError:
            pass

    def stop(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


def worker_threads():
    return [t for t in threading.enumerate() if t.name == "kiko-upstream" and t.is_alive()]


@pytest.fixture
def env(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    up = SilentUpstream()
    table = {"fake": {"base_url": f"http://127.0.0.1:{up.port}/v1", "kind": "openai",
                      "key_names": ["FAKE_KEY"], "auth": "bearer"}}
    resolver = lite.SimpleResolver(aliases={}, classes={}, env={}, providers=table)
    results = []
    policy = retry.RetryPolicy(attempts=1, base_delay=0.02, max_delay=0.05, budget=5.0, jitter=False)
    app = lite.build_app(resolver, MASTER, providers=table, policy=policy, secret={"FAKE_KEY": "k"}.get,
                         on_result=results.append, ping_interval=0.2, upstream_timeout=60.0)
    srv = server.Server(app, port=0).start()

    class Env:
        pass

    e = Env()
    e.up, e.results, e.url = up, results, srv.url
    yield e
    up.stop()
    srv.stop()


def test_cancel_with_silent_upstream_does_not_block(env):
    body = {"model": "fake/m", "max_tokens": 8, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    client = khttp.StdlibBackend()
    r = client.request("POST", env.url + "/v1/messages",
                       {"x-api-key": MASTER, "content-type": "application/json"},
                       json.dumps(body).encode(), stream=True)
    assert r.status == 200
    assert env.up.sent.wait(5)
    time.sleep(0.3)  # the worker is now blocked reading the silent upstream
    assert worker_threads()
    t0 = time.monotonic()
    r.close()  # client disconnects
    client.close()
    deadline = t0 + 2.0
    while time.monotonic() < deadline and not env.results:
        time.sleep(0.02)
    assert env.results, "request thread did not finish (on_result not called) within 2 s"
    while time.monotonic() < deadline + 2.0 and worker_threads():
        time.sleep(0.02)
    assert not worker_threads(), "worker thread still alive after cancel"
    assert time.monotonic() - t0 < 10.0
