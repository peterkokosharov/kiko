# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""RFC 6455 WebSocket server (kikolib.websocket) over real sockets on 127.0.0.1 ephemeral ports."""

from __future__ import annotations

import base64
import os
import socket
import struct
import threading
import time

import pytest

from kikolib import server, websocket

KEY = "dGhlIHNhbXBsZSBub25jZQ=="


def mask(data: bytes, key: bytes = b"\x01\x02\x03\x04") -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def frame(op: int, payload: bytes = b"", *, fin=True, masked=True, rsv=0) -> bytes:
    n = len(payload)
    out = bytes([(0x80 if fin else 0) | rsv | op])
    mbit = 0x80 if masked else 0
    if n < 126:
        out += bytes([mbit | n])
    elif n < 65536:
        out += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        out += bytes([mbit | 127]) + struct.pack("!Q", n)
    if masked:
        return out + b"\x01\x02\x03\x04" + mask(payload)
    return out + payload


class Client:
    def __init__(self, port: int, path="/ws", headers=None, method="GET", handshake=True):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""
        if handshake:
            h = {"Host": f"127.0.0.1:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
                 "Sec-WebSocket-Key": KEY, "Sec-WebSocket-Version": "13"}
            h.update(headers or {})
            h = {k: v for k, v in h.items() if v is not None}
            req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
            self.s.sendall(req.encode())
            self.status, self.headers = self.read_head()

    def read_head(self):
        while b"\r\n\r\n" not in self.buf:
            d = self.s.recv(4096)
            if not d:
                break
            self.buf += d
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        hd = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hd[k.strip().lower()] = v.strip()
        return int(lines[0].split()[1]), hd

    def need(self, n: int) -> bytes:
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_frame(self):
        b0, b1 = self.need(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self.need(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self.need(8))
        return b0 & 0x80 != 0, b0 & 0x0F, self.need(n)

    def eof(self) -> bool:
        try:
            return self.s.recv(10) == b""
        except ConnectionResetError:
            return True
        except socket.timeout:
            return False

    def send(self, data: bytes):
        self.s.sendall(data)

    def close(self):
        self.s.close()


@pytest.fixture
def make():
    started: list[server.Server] = []

    def _make(handler=None, *, app=None, srv_kw=None, **opts):
        app = app or server.App()
        seen: list[websocket.WebSocket] = []

        def echo(ws, req):
            seen.append(ws)
            while (m := ws.recv()) is not None:
                ws.send(m)

        websocket.route(app, "/ws", **opts)(handler or echo)
        srv = server.Server(app, "127.0.0.1", 0, drain_seconds=5, **(srv_kw or {})).start()
        started.append(srv)
        srv.seen = seen  # type: ignore[attr-defined]
        return srv

    yield _make
    for s in started:
        s.stop()


def expect_close(c: Client, code: int):
    fin, op, p = c.read_frame()
    assert (fin, op) == (True, 8)
    assert struct.unpack("!H", p[:2])[0] == code
    return p[2:]


def test_rfc_vector():
    assert websocket.accept_key(KEY) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_and_echo(make):
    c = Client(make(subprotocols=("b", "a")).port, headers={"Sec-WebSocket-Protocol": "a, b"})
    assert c.status == 101
    assert c.headers["sec-websocket-accept"] == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert c.headers["upgrade"].lower() == "websocket" and c.headers["sec-websocket-protocol"] == "b"
    c.send(frame(1, "héllo".encode()))
    assert c.read_frame() == (True, 1, "héllo".encode())
    c.send(frame(2, b"\x00\xff"))
    assert c.read_frame() == (True, 2, b"\x00\xff")


@pytest.mark.parametrize("hdrs,status", [
    ({"Sec-WebSocket-Key": "short"}, 400),
    ({"Sec-WebSocket-Key": base64.b64encode(b"x" * 15).decode()}, 400),
    ({"Sec-WebSocket-Key": None}, 400),
    ({"Upgrade": None}, 400),
    ({"Connection": "keep-alive"}, 400),
    ({"Sec-WebSocket-Version": "8"}, 426),
    ({"Sec-WebSocket-Version": None}, 426),
    ({"Origin": "http://evil.example"}, 403),
    ({"Origin": "null"}, 403),
    ({"Host": "evil.example"}, 403),
    ({"Host": None}, 400),
])
def test_bad_handshake(make, hdrs, status):
    c = Client(make().port, headers=hdrs)
    assert c.status == status
    if status == 426:
        assert c.headers["sec-websocket-version"] == "13"


def test_origin_rules(make):
    srv = make(origins=("http://ok.example",))
    assert Client(srv.port, headers={"Origin": "http://ok.example"}).status == 101
    assert Client(srv.port, headers={"Origin": f"http://127.0.0.1:{srv.port}"}).status == 101  # same origin
    assert Client(srv.port, headers={"Origin": None}).status == 101  # non-browser client


def test_method_and_path(make):
    srv = make()
    assert Client(srv.port, method="POST", headers={"Content-Length": "0"}).status == 405
    assert Client(srv.port, method="DELETE").status == 405
    assert Client(srv.port, path="/wsx").status == 404
    assert Client(srv.port, path="/ws/").status == 404


def test_unmasked_frame_closes_1002(make):
    c = Client(make().port)
    c.send(frame(1, b"hi", masked=False))
    expect_close(c, 1002)
    assert c.eof()


@pytest.mark.parametrize("n", [0, 125, 126, 65535, 65536, 70000])
def test_length_encodings(make, n):
    c = Client(make().port)
    data = os.urandom(n)
    c.send(frame(2, data))
    assert c.read_frame() == (True, 2, data)


def test_64bit_length_msb_rejected(make):
    c = Client(make().port)
    c.send(bytes([0x82, 0xFF]) + struct.pack("!Q", 1 << 63) + b"\0\0\0\0")
    expect_close(c, 1002)


def test_rsv_and_unknown_opcode(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(1, b"x", rsv=0x40))
    expect_close(c, 1002)
    c = Client(srv.port)
    c.send(frame(3, b"x"))
    expect_close(c, 1002)
    c = Client(srv.port)
    c.send(frame(0xB, b""))
    expect_close(c, 1002)


def test_fragmentation(make):
    c = Client(make().port)
    c.send(frame(1, b"he", fin=False) + frame(0, b"l", fin=False) + frame(0, b"lo"))
    assert c.read_frame() == (True, 1, b"hello")
    # a ping may interleave a fragmented message
    c.send(frame(2, b"ab", fin=False) + frame(9, b"p") + frame(0, b"cd"))
    assert c.read_frame() == (True, 10, b"p")
    assert c.read_frame() == (True, 2, b"abcd")


def test_fragmentation_errors(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(0, b"x"))  # continuation without start
    expect_close(c, 1002)
    c = Client(srv.port)
    c.send(frame(1, b"x", fin=False) + frame(1, b"y"))  # new data frame mid-message
    expect_close(c, 1002)


def test_oversize_frame_and_message(make):
    srv = make(max_frame=1000, max_message=1500)
    c = Client(srv.port)
    c.send(frame(2, b"x" * 1001))
    expect_close(c, 1009)
    c = Client(srv.port)
    c.send(frame(2, b"x" * 800, fin=False) + frame(0, b"x" * 800))
    expect_close(c, 1009)
    c = Client(srv.port)  # exactly at the limit is fine
    c.send(frame(2, b"x" * 1000, fin=False) + frame(0, b"x" * 500))
    assert c.read_frame()[2] == b"x" * 1500


def test_huge_declared_length_not_allocated(make):
    c = Client(make().port)
    c.send(bytes([0x82, 0xFF]) + struct.pack("!Q", 1 << 40))  # rejected on the header alone
    expect_close(c, 1009)


def test_bad_utf8(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(1, b"\xff\xfe"))
    expect_close(c, 1007)
    c = Client(srv.port)  # a 3-byte char split across fragments is valid
    euro = "€".encode()
    c.send(frame(1, euro[:1], fin=False) + frame(0, euro[1:2], fin=False) + frame(0, euro[2:]))
    assert c.read_frame() == (True, 1, euro)
    c = Client(srv.port)  # split and truncated at the end
    c.send(frame(1, euro[:2], fin=False) + frame(0, b""))
    expect_close(c, 1007)
    c = Client(srv.port)  # invalid byte in the first fragment fails at once, before FIN
    c.send(frame(1, b"ok\xc0\xaf", fin=False))
    expect_close(c, 1007)
    c = Client(srv.port)  # surrogate halves are not UTF-8
    c.send(frame(1, b"\xed\xa0\x80"))
    expect_close(c, 1007)


def test_control_frame_rules(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(9, b"x" * 126))
    expect_close(c, 1002)
    c = Client(srv.port)
    c.send(frame(9, b"x", fin=False))
    expect_close(c, 1002)
    c = Client(srv.port)
    c.send(frame(8, b"x" * 126))
    expect_close(c, 1002)


def test_ping_pong_echo(make):
    c = Client(make().port)
    c.send(frame(9, b"payload"))
    assert c.read_frame() == (True, 10, b"payload")
    c.send(frame(10, b"unsolicited"))  # ignored
    c.send(frame(1, b"after"))
    assert c.read_frame() == (True, 1, b"after")


def test_server_ping_and_idle_timeout(make):
    c = Client(make(ping_interval=0.3, idle_timeout=1.2, close_timeout=0.5).port)
    assert c.read_frame() == (True, 9, b"")
    c.send(frame(10, b""))
    deadline = time.monotonic() + 5
    while True:  # we stay silent after that: the server closes with 1008
        fin, op, p = c.read_frame()
        if op == 8:
            assert struct.unpack("!H", p[:2])[0] == 1008
            break
        assert op == 9 and time.monotonic() < deadline


def test_close_echo_and_socket_closed(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(8, struct.pack("!H", 4001) + b"bye"))
    assert expect_close(c, 4001) == b"bye"
    assert c.eof()
    assert srv.seen[0].close_code == 4001 and srv.seen[0].close_reason == "bye"
    c = Client(srv.port)
    c.send(frame(8))  # empty close
    fin, op, p = c.read_frame()
    assert op == 8
    assert c.eof()


@pytest.mark.parametrize("payload", [b"\x03", struct.pack("!H", 1005), struct.pack("!H", 999),
                                     struct.pack("!H", 1004), struct.pack("!H", 5000)])
def test_invalid_close_code(make, payload):
    c = Client(make().port)
    c.send(frame(8, payload))
    expect_close(c, 1002)
    assert c.eof()


def test_close_bad_reason_utf8(make):
    c = Client(make().port)
    c.send(frame(8, struct.pack("!H", 1000) + b"\xff"))
    expect_close(c, 1007)


def test_server_initiated_close(make):
    def h(ws, req):
        ws.recv()
        ws.close(1008, "nope")
        assert ws.recv() is None  # waits for the peer's close
        assert ws.close_code == 1000

    c = Client(make(h).port)
    c.send(frame(1, b"x"))
    assert expect_close(c, 1008) == b"nope"
    c.send(frame(1, b"dropped"))  # data after our close is ignored
    c.send(frame(8, struct.pack("!H", 1000)))
    assert c.eof()


def test_handler_return_closes_1000(make):
    c = Client(make(lambda ws, req: ws.send_text("only")).port)
    assert c.read_frame() == (True, 1, b"only")
    expect_close(c, 1000)
    c.send(frame(8, struct.pack("!H", 1000)))
    assert c.eof()


def test_handler_exception_closes_1011(make):
    def h(ws, req):
        raise RuntimeError("boom")
    c = Client(make(h).port)
    expect_close(c, 1011)


def test_unresponsive_peer_close_bounded(make):
    t0 = time.monotonic()
    c = Client(make(lambda ws, req: None, close_timeout=0.5).port)
    expect_close(c, 1000)
    assert c.eof()  # the peer never answered: the server still shut the socket in about close_timeout
    assert time.monotonic() - t0 < 4


def test_abrupt_disconnect(make):
    got = []

    def h(ws, req):
        got.append(ws.recv())
        got.append(ws.close_code)

    srv = make(h)
    c = Client(srv.port)
    c.send(frame(1, b"x", fin=False)[:3])  # half a frame, then vanish
    c.close()
    deadline = time.monotonic() + 5
    while len(got) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert got == [None, 1006]


def test_idle_connection_survives_quiet_period(make):
    c = Client(make(ping_interval=None, idle_timeout=None).port)
    time.sleep(0.6)  # several reader ticks with no data (the old 0.1 s SocketIO timeout bug)
    c.send(frame(1, b"still here"))
    assert c.read_frame() == (True, 1, b"still here")


def test_frame_split_across_reads(make):
    c = Client(make().port)
    raw = frame(1, b"x" * 300)
    for i in range(0, len(raw), 50):
        c.send(raw[i:i + 50])
        time.sleep(0.01)
    assert c.read_frame() == (True, 1, b"x" * 300)


def test_frames_right_behind_handshake(make):
    srv = make()
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
    s.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode() + frame(1, b"early"))
    c = Client(srv.port, handshake=False)
    c.s.close()
    c.s, c.buf = s, b""
    c.read_head()
    assert c.read_frame() == (True, 1, b"early")


def test_slow_client_is_dropped(make):
    result = {}

    def h(ws, req):
        chunk = b"x" * 262144
        t0 = time.monotonic()
        sent = 0
        try:
            while sent < 10_000:  # would be 2.5 GiB; a bounded sender must fail long before
                ws.send_bytes(chunk)
                sent += 1
        except websocket.WebSocketClosed:
            result["sent"] = sent
        result["secs"] = time.monotonic() - t0

    srv = make(h, send_timeout=0.5, close_timeout=0.3)
    c = Client(srv.port)  # never reads
    deadline = time.monotonic() + 15
    while "secs" not in result and time.monotonic() < deadline:
        time.sleep(0.05)
    assert result["sent"] < 10_000 and result["secs"] < 10
    c.close()


def test_other_thread_send_is_serialised(make):
    def h(ws, req):
        ts = [threading.Thread(target=lambda: [ws.send_text("a" * 70000) for _ in range(5)]) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    c = Client(make(h).port)
    for _ in range(20):
        assert c.read_frame() == (True, 1, b"a" * 70000)
    expect_close(c, 1000)


def test_server_stop_closes_with_1001(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(1, b"x"))
    assert c.read_frame()[2] == b"x"
    t = threading.Thread(target=srv.stop)
    t.start()
    expect_close(c, 1001)
    c.send(frame(8, struct.pack("!H", 1001)))
    assert c.eof()
    t.join(10)
    assert not t.is_alive()


def test_active_counter_and_drain(make):
    srv = make()
    c = Client(srv.port)
    c.send(frame(1, b"x"))
    c.read_frame()
    assert srv._httpd.active == 1
    assert srv.drain(0.2) is False  # the open WebSocket is an active request
    c.send(frame(8, struct.pack("!H", 1000)))
    expect_close(c, 1000)
    c.close()
    assert srv.drain(5) is True


def test_connection_cap_counts_websockets(make):
    srv = make(srv_kw={"max_connections": 1})
    c = Client(srv.port)
    assert c.status == 101
    try:
        assert Client(srv.port).status == 503
    except ConnectionError:  # the server answers 503 and closes without reading: Windows may RST first
        pass
    c.close()


def test_http_still_served_after_ws_close(make):
    app = server.App()

    @app.route("GET", "/ping")
    def ping(req):
        return server.JsonResponse({"ok": True})

    srv = make(app=app)
    c = Client(srv.port)
    c.send(frame(8, struct.pack("!H", 1000)))
    expect_close(c, 1000)
    assert c.eof()  # the HTTP handler did not keep reading the upgraded connection
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
    s.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    assert b'{"ok":true}' in data


def test_close_deadline_not_bypassed_by_chatty_peer(make):
    c = Client(make(lambda ws, req: None, close_timeout=0.5, ping_interval=None).port)
    expect_close(c, 1000)
    t0 = time.monotonic()
    c.s.settimeout(0.05)
    gone = False
    while time.monotonic() - t0 < 5 and not gone:
        try:
            c.s.sendall(frame(9, b"x"))  # a peer that keeps talking and never answers the close
            gone = c.s.recv(100) == b""
        except socket.timeout:
            pass
        except OSError:
            gone = True
    assert gone and time.monotonic() - t0 < 3


def test_stop_time_independent_of_socket_count(make):
    def h(ws, req):
        try:
            while True:
                ws.send_bytes(b"x" * 262144)  # the client never reads: this blocks in sendall
        except websocket.WebSocketClosed:
            pass

    n = 8
    srv = make(h)
    clients = [Client(srv.port) for _ in range(n)]
    time.sleep(1.5)  # let every send buffer fill up
    t0 = time.monotonic()
    srv.stop()
    assert time.monotonic() - t0 < 8  # serial stop would be about n * 2 s or more
    for c in clients:
        c.close()


def test_no_frame_before_handshake_response():
    a, b = socket.socketpair()
    try:
        ws = websocket.WebSocket(a, websocket._Config(), None)  # type: ignore[arg-type]
        ws.abort_on_stop()  # 101 not sent yet: the socket is just closed, no frame is written
        b.settimeout(2)
        assert b.recv(100) == b""
    finally:
        a.close()
        b.close()
    a, b = socket.socketpair()
    try:
        ws = websocket.WebSocket(a, websocket._Config(), None)  # type: ignore[arg-type]
        ws._ready = True
        ws.abort_on_stop()
        b.settimeout(2)
        assert struct.unpack("!H", b.recv(100)[2:4])[0] == 1001
    finally:
        a.close()
        b.close()


def test_declared_length_checked_before_payload(make):
    srv = make(max_frame=10000, max_message=1500)
    c = Client(srv.port)
    c.send(bytes([0x82, 0xFE]) + struct.pack("!H", 5000))  # header only: rejected without waiting for the payload
    expect_close(c, 1009)
    c = Client(srv.port)  # running total of fragments
    c.send(frame(2, b"x" * 1000, fin=False) + bytes([0x00, 0xFE]) + struct.pack("!H", 600))
    expect_close(c, 1009)


@pytest.mark.parametrize("hdr", [
    bytes([0x82, 0xFE]) + struct.pack("!H", 5) + b"\1\2\3\4" + b"\0" * 5,
    bytes([0x82, 0xFF]) + struct.pack("!Q", 100) + b"\1\2\3\4" + b"\0" * 100,
])
def test_non_minimal_length_rejected(make, hdr):
    c = Client(make().port)
    c.send(hdr)
    expect_close(c, 1002)


def test_closed_handler_is_not_logged_as_error(make, caplog):
    done = threading.Event()

    def h(ws, req):
        done.set()
        raise websocket.WebSocketClosed("peer went away")

    srv = make(h)
    with caplog.at_level("DEBUG", logger="kikolib.websocket"):
        c = Client(srv.port)
        assert done.wait(5)
        c.close()
        assert srv.drain(5)
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
