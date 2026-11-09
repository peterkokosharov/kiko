# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko spy: ring buffer, log tail, read-only DB poll, SSE, WebSocket, page safety, Origin/Host guard.
Everything runs on ephemeral ports 127.0.0.1 with temp files; nothing touches ~/.kiko."""

from __future__ import annotations

import http.client
import json
import re
import socket
import sqlite3
import struct
import time
from pathlib import Path

import pytest

from kiko_lite.db import Database
from kikolib import server as kserver
from spy import ui
from spy.app import Spy, default_spy
from spy.ring import Hub, RingBuffer
from spy.sources import DbPoll, LogTail

T = 5.0


# --- ring buffer ---
def test_ring_buffer_is_bounded_and_keeps_newest():
    r = RingBuffer(3)
    for i in range(1, 8):
        r.append((i, "x", str(i)))
    assert len(r) == 3
    assert [i[0] for i in r.items()] == [5, 6, 7]
    assert [i[0] for i in r.items(after=5)] == [6, 7]
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_hub_numbers_across_channels_and_bounds_each():
    h = Hub(("server", "llm"), maxlen=2)
    for i in range(5):
        h.publish("server", f"s{i}")
    h.publish("llm", "l0")
    got = h.since(0)
    assert [i[2] for i in got] == ["s3", "s4", "l0"] and [i[0] for i in got] == [4, 5, 6]
    assert h.seq == 6 and h.wait(6, 0.05) == []


# --- log tail ---
def _append(p: Path, text: str) -> None:
    with open(p, "ab") as f:
        f.write(text.encode())


def test_log_tail_backlog_then_new_lines_and_partial_line(tmp_path):
    p = tmp_path / "k.log"
    p.write_bytes(b"".join(f"old{i}\n".encode() for i in range(5)))
    t = LogTail(p, backlog=2)
    assert t.poll() == ["old3", "old4"]
    assert t.poll() == []
    _append(p, "new1\nhalf")
    assert t.poll() == ["new1"]
    _append(p, " line\n")
    assert t.poll() == ["half line"]


def test_log_tail_missing_file_then_created(tmp_path):
    p = tmp_path / "later.log"
    t = LogTail(p)
    assert t.poll() == []
    p.write_text("a\nb\n", encoding="utf-8")
    assert t.poll() == ["a", "b"]


def test_log_tail_survives_truncation(tmp_path):
    p = tmp_path / "k.log"
    p.write_text("one\ntwo\nthree\n", encoding="utf-8")
    t = LogTail(p)
    t.poll()
    p.write_text("x\n", encoding="utf-8")  # shorter than the old position
    assert t.poll() == ["x"]
    _append(p, "y\n")
    assert t.poll() == ["y"]


def test_log_tail_survives_rotation(tmp_path):
    p = tmp_path / "k.log"
    p.write_text("one\ntwo\n", encoding="utf-8")
    t = LogTail(p)
    t.poll()
    p.rename(tmp_path / "k.log.1")
    assert t.poll() == []  # the file is gone for a moment
    p.write_text("fresh-a\nfresh-b\nfresh-c\n", encoding="utf-8")  # a new, longer file
    assert t.poll() == ["fresh-a", "fresh-b", "fresh-c"]


def test_log_tail_bad_bytes_and_long_lines(tmp_path):
    p = tmp_path / "k.log"
    p.write_bytes(b"ok\xff\xfe\n" + b"z" * 10000 + b"\n")
    got = LogTail(p).poll()
    assert got[0].startswith("ok") and len(got[1]) < 4100 and got[1].endswith(" ...")


def test_log_tail_without_path():
    assert LogTail(None).poll() == []


# --- DB poll ---
def _db(tmp_path: Path) -> Path:
    path = tmp_path / "kiko.db"
    Database(path)
    return path


def _row(path: Path, model: str, error: str | None = None) -> None:
    con = sqlite3.connect(path)
    con.execute("INSERT INTO billing (model, tokens_in, tokens_out, cost_usd, ts, route, error, request_id) "
                "VALUES (?, 10, 20, 0.5, '2026-11-04T10:00:00', 'messages', ?, 'r1')", (model, error))
    con.commit()
    con.close()


def test_db_poll_backlog_and_new_rows(tmp_path):
    path = _db(tmp_path)
    _row(path, "m0")
    d = DbPoll(path)
    first = d.poll()
    assert len(first) == 1 and "m0" in first[0] and "in=10 out=20" in first[0] and "$0.5000" in first[0]
    assert d.poll() == []
    _row(path, "m1", error="boom\nsecond line")
    got = d.poll()
    assert len(got) == 1 and "m1" in got[0] and "ERROR boom second line" in got[0] and "\n" not in got[0]


def test_db_poll_missing_file_and_no_table(tmp_path):
    assert DbPoll(tmp_path / "none.db").poll() == []
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert DbPoll(empty).poll() == []


def test_db_poll_is_read_only(tmp_path, monkeypatch):
    path = _db(tmp_path)
    _row(path, "m0")
    before = path.read_bytes()
    seen: list[sqlite3.Connection] = []
    opened: list[tuple] = []
    real = sqlite3.connect

    def spy_connect(*a, **k):
        opened.append((a, k))
        con = real(*a, **k)
        seen.append(con)
        return con

    monkeypatch.setattr("spy.sources.sqlite3.connect", spy_connect)
    d = DbPoll(path)
    d.poll()
    assert seen
    # the same kind of connection (mode=ro URI) cannot write
    from kiko_lite.db import ro_uri
    con = real(ro_uri(path.resolve()), uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO billing (model, ts) VALUES ('x', 'y')")
    con.close()
    assert path.read_bytes() == before  # polling changed nothing
    assert opened and all("mode=ro" in a[0] and k.get("uri") is True for a, k in opened)


def test_db_poll_pruned_rows_start_over(tmp_path):
    path = _db(tmp_path)
    for m in ("a", "b", "c"):
        _row(path, m)
    d = DbPoll(path)
    d.poll()
    con = sqlite3.connect(path)
    con.execute("DELETE FROM billing")
    con.commit()
    con.close()
    assert d.poll() == []
    _row(path, "d")  # id continues (AUTOINCREMENT) above the old top: shown
    assert len(d.poll()) == 1


# --- server fixture ---
@pytest.fixture
def running(tmp_path):
    log = tmp_path / "k.log"
    log.write_text("boot line\n", encoding="utf-8")
    db = _db(tmp_path)
    spy = Spy(lambda: log, lambda: db, maxlen=50, poll_seconds=0.05)
    spy.log, spy.dbfile = log, db  # type: ignore[attr-defined]
    spy.start()
    srv = kserver.Server(spy.build_app(), "127.0.0.1", 0, drain_seconds=2).start()
    yield spy, srv
    spy.stop()
    srv.stop()


def _get(srv, path, headers=None):
    c = http.client.HTTPConnection(srv.host, srv.port, timeout=T)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def _wait(cond, timeout=T):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- SSE ---
def _sse_events(srv, want, path="/events", headers=None):
    """Read SSE frames until ``want`` data events arrived; returns [(id, event, data)]."""
    s = socket.create_connection((srv.host, srv.port), timeout=T)
    h = {"Host": f"127.0.0.1:{srv.port}", **(headers or {})}
    s.sendall((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode())
    buf = b""
    out: list[tuple[str, str, dict]] = []
    try:
        end = time.monotonic() + T
        while len(out) < want and time.monotonic() < end:
            buf += s.recv(65536)
            for m in re.finditer(rb"id: (\d+)\nevent: (\w+)\ndata: ([^\n]*)\n\n", buf):
                item = (m.group(1).decode(), m.group(2).decode(), json.loads(m.group(3)))
                if item not in out:
                    out.append(item)
    finally:
        s.close()
    return out


def test_sse_gets_backlog_and_new_lines(running):
    spy, srv = running
    assert _wait(lambda: spy.hub.seq >= 1)
    spy_log = spy.log
    _append(spy_log, "fresh server line\n")
    _row(spy.dbfile, "gem-flash")
    assert _wait(lambda: spy.hub.seq >= 3)
    ev = _sse_events(srv, 3)
    texts = {(e[2]["ch"], e[2]["text"]) for e in ev}
    assert ("server", "boot line") in texts and ("server", "fresh server line") in texts
    assert any(ch == "llm" and "gem-flash" in t for ch, t in texts)
    assert all(e[1] == "line" for e in ev)
    # live: a line written while the stream is open arrives
    import threading
    threading.Timer(0.3, lambda: _append(spy_log, "live!\n")).start()
    ev2 = _sse_events(srv, 4)
    assert any(e[2]["text"] == "live!" for e in ev2)


def test_sse_last_event_id_resumes(running):
    spy, srv = running
    assert _wait(lambda: spy.hub.seq >= 1)  # the boot line is in first
    spy.hub.publish("server", "a")
    spy.hub.publish("server", "b")
    n = spy.hub.seq
    ev = _sse_events(srv, 1, headers={"Last-Event-ID": str(n - 1)})
    assert [e[2]["text"] for e in ev] == ["b"]
    ev = _sse_events(srv, 1, path=f"/events?after={n + 100}")  # from before a restart: starts over
    assert ev and ev[0][0] == "1"


# --- WebSocket ---
KEY = "dGhlIHNhbXBsZSBub25jZQ=="


class WS:
    def __init__(self, port, headers=None, path="/ws"):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=T)
        h = {"Host": f"127.0.0.1:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
             "Sec-WebSocket-Key": KEY, "Sec-WebSocket-Version": "13", **(headers or {})}
        self.s.sendall((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
                        + "\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            d = self.s.recv(4096)
            if not d:
                break
            self.buf += d
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.status = int(head.split()[1])

    def need(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def text(self):
        _, b1 = self.need(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self.need(2))
        return json.loads(self.need(n))


def test_ws_gets_backlog_and_new_lines(running):
    spy, srv = running
    assert _wait(lambda: spy.hub.seq >= 1)
    c = WS(srv.port)
    assert c.status == 101
    assert c.text()["text"] == "boot line"
    _append(spy.log, "over ws\n")
    assert c.text()["text"] == "over ws"
    c.s.close()


# --- guard and page ---
def test_bad_origin_and_host_rejected(running):
    _, srv = running
    assert _get(srv, "/")[0] == 200
    assert _get(srv, "/", {"Origin": "http://evil.example"})[0] == 403
    assert _get(srv, "/events", {"Origin": "null"})[0] == 403
    assert _get(srv, "/", {"Host": "evil.example"})[0] == 403
    assert _get(srv, "/", {"Origin": f"http://127.0.0.1:{srv.port}"})[0] == 200  # its own page
    assert WS(srv.port, {"Origin": "http://evil.example"}).status == 403
    assert WS(srv.port, {"Host": "evil.example"}).status == 403


def test_no_write_endpoints(running):
    _, srv = running
    c = http.client.HTTPConnection(srv.host, srv.port, timeout=T)
    c.request("POST", "/events", body=b"{}", headers={"Content-Type": "application/json"})
    assert c.getresponse().status in (404, 405)
    c.close()


def test_page_headers_and_routes(running):
    _, srv = running
    st, h, body = _get(srv, "/")
    assert st == 200 and h["Content-Type"].startswith("text/html") and b"/app.js" in body
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"
    assert _get(srv, "/app.js")[0] == 200 and _get(srv, "/app.css")[0] == 200
    assert _get(srv, "/nope")[0] == 404


def test_ui_uses_textcontent_and_no_innerhtml_for_data():
    for name in ("HTML", "JS", "CSS"):
        text = getattr(ui, name)
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function",
                             text), name
    assert "textContent" in ui.JS
    assert "<script>" not in ui.HTML  # only an external script (CSP: no inline)
    assert not re.search(r"\son\w+\s*=", ui.HTML)


def test_default_spy_uses_config_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "x.db"))
    (tmp_path / "kiko.toml").write_text('[logs]\nfile = "spy.log"\n', encoding="utf-8")
    (tmp_path / "spy.log").write_text("from toml\n", encoding="utf-8")
    spy = default_spy()
    assert spy.poll_once() == 1
    assert [i[2] for i in spy.hub.since(0)] == ["from toml"]
