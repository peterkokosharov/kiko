# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""RFC 6455 WebSocket server for ``kikolib.server`` (stdlib only, no extensions, no TLS).

Usage::

    from kikolib import server, websocket

    app = server.App()

    @websocket.route(app, "/ws", max_message=1 << 20, origins=("http://example.test",))
    def echo(ws: websocket.WebSocket, req: server.Request) -> None:
        while (msg := ws.recv()) is not None:      # str (text) | bytes (binary) | None (closed)
            ws.send_text(msg) if isinstance(msg, str) else ws.send_bytes(msg)

``route`` registers a GET route; ``upgrade(req, fn, **opts)`` does the same from inside your own handler
(after your own auth). The function runs in the connection's thread, which is the single reader: call
``recv()`` only from it. Sends (``send_text`` / ``send_bytes`` / ``ping`` / ``close``) may be called from any
thread; they are serialised by a lock and time-bounded (``send_timeout``): a client that does not read is
dropped (``WebSocketClosed``), so memory never grows with a slow peer. When the function returns, the
connection is closed (1000) with a bounded close handshake. ``Server.request_stop`` closes every live
WebSocket with 1001; the connection counts as an active request until it ends (``Server.drain``).

WebSocket object: ``recv()``, ``send_text(str)``, ``send_bytes(bytes)``, ``send(str|bytes)``,
``ping(payload=b"")``, ``close(code=1000, reason="")``, and attributes ``closed``, ``close_code`` (peer's code,
our failure code, or 1006 when the connection vanished), ``close_reason``, ``subprotocol``, ``request``.

Options (keyword arguments of ``route`` / ``upgrade``): ``max_frame`` / ``max_message`` (default 1 MiB; bigger
-> close 1009), ``send_timeout`` (10 s), ``close_timeout`` (5 s), ``ping_interval`` (30 s, None = off),
``idle_timeout`` (90 s without any received byte -> close 1008, None = off), ``origins`` (extra allowed Origin
values), ``subprotocols`` (server preference order).

Handshake rules: GET, HTTP/1.1, ``Upgrade: websocket``, ``Connection: upgrade``, 16-byte base64 key, version 13
(else 426 + ``Sec-WebSocket-Version: 13``), allowed Host (``Server.host_allowed``). A request without Origin
(non-browser clients) is accepted; with Origin it must be the same origin (``http://<Host>``) or in
``origins`` (else 403). Note ``App.browser_guard`` refuses any Origin before routing; browser pages need it off
or ``App.allow_same_origin`` on.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import logging
import select
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .server import App, HTTPError, Request, Takeover, _error_response

logger = logging.getLogger("kikolib.websocket")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_MAX = 1024 * 1024
_TICK = 0.2  # reader wake-up for ping / idle / close deadlines (state is never lost on a wake-up)
_OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = 0, 1, 2, 8, 9, 10
_RECEIVABLE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014})


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept for a Sec-WebSocket-Key (RFC 6455 section 1.3)."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def _valid_code(code: int) -> bool:
    return code in _RECEIVABLE_CODES or 3000 <= code <= 4999


def _unmask(data: bytes, key: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def _frame(op: int, payload: bytes) -> bytes:
    n, b0 = len(payload), 0x80 | op
    if n < 126:
        return bytes((b0, n)) + payload
    if n < 65536:
        return struct.pack("!BBH", b0, 126, n) + payload
    return struct.pack("!BBQ", b0, 127, n) + payload


class WebSocketClosed(ConnectionError):
    """Sending on a closed or dropped WebSocket."""


class _Fail(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


class _Gone(Exception):
    """Connection ended (EOF, error, or the close deadline passed)."""


@dataclass(frozen=True)
class _Config:
    max_frame: int = DEFAULT_MAX
    max_message: int = DEFAULT_MAX
    send_timeout: float = 10.0
    close_timeout: float = 5.0
    ping_interval: float | None = 30.0
    idle_timeout: float | None = 90.0
    origins: frozenset[str] = frozenset()
    subprotocols: tuple[str, ...] = ()


class WebSocket:
    """One upgraded connection (see the module docstring)."""

    def __init__(self, sock: socket.socket, cfg: _Config, request: Request, leftover: bytes = b"",
                 subprotocol: str | None = None) -> None:
        self._sock, self._cfg = sock, cfg
        self.request, self.subprotocol = request, subprotocol
        self._buf = bytearray(leftover)
        self._lock = threading.Lock()
        self._dead = False
        self._close_sent = False
        self._recv_done = False
        self._ready = False  # True once the 101 response is fully written (no frame may precede it)
        self._deadline: float | None = None  # close handshake deadline once we sent a close
        self.close_code: int | None = None
        self.close_reason = ""
        now = time.monotonic()
        self._last_rx = self._last_ping = now
        sock.settimeout(cfg.send_timeout)  # applies to sends; reads wait in select() first

    @property
    def closed(self) -> bool:
        return self._dead or self._close_sent or self._recv_done

    # --- sending ---

    def send_text(self, text: str) -> None:
        self._send(_OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send(_OP_BIN, bytes(data))

    def send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            self.send_text(data)
        else:
            self.send_bytes(data)

    def ping(self, payload: bytes = b"") -> None:
        if len(payload) > 125:
            raise ValueError("ping payload is limited to 125 bytes")
        self._send(_OP_PING, bytes(payload))

    def close(self, code: int = 1000, reason: str = "", timeout: float | None = None) -> None:
        """Send the close frame (once). The peer's reply is read by ``recv`` / the end of the handler."""
        if not _valid_code(code):
            raise ValueError(f"invalid close code {code}")
        r = reason.encode("utf-8")[:123]
        while r:  # cut on a character boundary
            try:
                r.decode("utf-8")
                break
            except UnicodeDecodeError:
                r = r[:-1]
        try:
            self._send(_OP_CLOSE, struct.pack("!H", code) + r, timeout)
        except WebSocketClosed:
            pass

    def abort_on_stop(self) -> None:
        wait = min(self._cfg.send_timeout, 2.0)
        if not self._lock.acquire(timeout=wait):  # a stuck writer: the peer is not reading
            self._abort()
            return
        try:
            ready = self._ready
        finally:
            self._lock.release()
        if not ready:  # the 101 response is not out yet: never write a frame before it, just drop the socket
            self._abort()
            return
        self.close(1001, "going away", timeout=wait)

    def _send(self, op: int, payload: bytes, timeout: float | None = None) -> None:
        if not self._lock.acquire(timeout=self._cfg.send_timeout if timeout is None else timeout):
            self._abort()  # a stuck writer holds the lock: the peer is not reading
            raise WebSocketClosed("send timed out")
        try:
            if self._dead:
                raise WebSocketClosed("connection is closed")
            if op == _OP_CLOSE:
                if self._close_sent:
                    return
                self._close_sent = True
                self._deadline = time.monotonic() + self._cfg.close_timeout
            elif self._close_sent:
                raise WebSocketClosed("close already sent")
            data = _frame(op, payload)
            try:
                self._sock.sendall(data)
            except (OSError, ValueError) as e:  # timeout (slow client) or reset: a partial frame cannot be repaired
                self._abort()
                raise WebSocketClosed(f"send failed: {e}") from None
        finally:
            self._lock.release()

    def _abort(self) -> None:
        self._dead = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    # --- receiving ---

    def recv(self) -> str | bytes | None:
        """Next message: str for text, bytes for binary, None once the connection is closed."""
        if self._recv_done:
            return None
        try:
            return self._recv_message()
        except _Fail as f:
            self._fail(f.code, f.reason)
        except _Gone:
            self._recv_done = True
            if self.close_code is None:
                self.close_code = 1006
        return None

    def _fail(self, code: int, reason: str) -> None:
        self._recv_done = True
        self.close_code, self.close_reason = code, reason
        self.close(code, reason)

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise _Gone  # one absolute close deadline, whatever the peer sends

    def _wait_readable(self) -> None:
        sock, cfg = self._sock, self._cfg
        while True:
            if self._dead:
                raise _Gone
            self._check_deadline()
            try:
                ready, _, _ = select.select([sock], [], [], _TICK)
            except (OSError, ValueError):
                raise _Gone from None
            if ready:
                return
            now = time.monotonic()
            if cfg.idle_timeout is not None and now - self._last_rx >= cfg.idle_timeout:
                self._fail(1008, "idle timeout")
                raise _Gone
            if (cfg.ping_interval is not None and not self._close_sent
                    and now - self._last_ping >= cfg.ping_interval):
                self._last_ping = now
                try:
                    self._send(_OP_PING, b"")
                except WebSocketClosed:
                    raise _Gone from None

    def _read_exact(self, n: int) -> bytes:
        buf = self._buf
        while len(buf) < n:
            self._wait_readable()
            try:
                chunk = self._sock.recv(65536)
            except (OSError, ValueError):
                raise _Gone from None
            if not chunk:
                raise _Gone
            self._last_rx = time.monotonic()
            buf += chunk
        out = bytes(buf[:n])
        del buf[:n]
        return out

    def _read_frame(self, room: int | None = None) -> tuple[bool, int, bytes]:
        self._check_deadline()
        b0, b1 = self._read_exact(2)
        fin, rsv, op = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
        if rsv:
            raise _Fail(1002, "RSV bits set without a negotiated extension")
        if op not in (_OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG):
            raise _Fail(1002, "unknown opcode")
        if not b1 & 0x80:
            raise _Fail(1002, "client frames must be masked")
        n = b1 & 0x7F
        control = op >= 8
        if control and (n > 125 or not fin):
            raise _Fail(1002, "invalid control frame")
        if n == 126:
            (n,) = struct.unpack("!H", self._read_exact(2))
            if n < 126:
                raise _Fail(1002, "non-minimal length encoding")
        elif n == 127:
            (n,) = struct.unpack("!Q", self._read_exact(8))
            if n >> 63:
                raise _Fail(1002, "invalid frame length")
            if n < 65536:
                raise _Fail(1002, "non-minimal length encoding")
        if n > self._cfg.max_frame:
            raise _Fail(1009, "frame too large")
        if room is not None and not control and n > room:
            raise _Fail(1009, "message too large")  # declared length checked before the payload is read
        key = self._read_exact(4)
        return fin, op, _unmask(self._read_exact(n), key)

    def _recv_message(self) -> str | bytes | None:
        cfg = self._cfg
        kind: int | None = None
        parts: list[Any] = []
        total = 0
        dec: codecs.IncrementalDecoder | None = None
        while True:
            fin, op, payload = self._read_frame(cfg.max_message - total)
            if op == _OP_PING:
                try:
                    self._send(_OP_PONG, payload)
                except WebSocketClosed:
                    pass
                continue
            if op == _OP_PONG:
                continue
            if op == _OP_CLOSE:
                self._on_close(payload)
                return None
            if op == _OP_CONT:
                if kind is None:
                    raise _Fail(1002, "unexpected continuation frame")
            else:
                if kind is not None:
                    raise _Fail(1002, "new data frame inside a fragmented message")
                kind = op
                if kind == _OP_TEXT:
                    dec = codecs.getincrementaldecoder("utf-8")("strict")
            total += len(payload)
            if total > cfg.max_message:
                raise _Fail(1009, "message too large")
            if kind == _OP_TEXT:
                assert dec is not None
                try:
                    parts.append(dec.decode(payload, fin))
                except UnicodeDecodeError:
                    raise _Fail(1007, "invalid UTF-8 in text message") from None
            else:
                parts.append(payload)
            if not fin:
                continue
            msg = "".join(parts) if kind == _OP_TEXT else b"".join(parts)
            kind, parts, total, dec = None, [], 0, None
            if self._close_sent:  # closing: data from the peer is dropped until its close frame
                continue
            return msg

    def _on_close(self, payload: bytes) -> None:
        self._recv_done = True
        if not payload:
            self.close_code = 1005
            self.close(1000)
            return
        if len(payload) == 1:
            self._fail(1002, "invalid close payload")
            return
        (code,) = struct.unpack("!H", payload[:2])
        if not _valid_code(code):
            self._fail(1002, "invalid close code")
            return
        try:
            reason = payload[2:].decode("utf-8")
        except UnicodeDecodeError:
            self._fail(1007, "invalid UTF-8 in close reason")
            return
        self.close_code, self.close_reason = code, reason
        self.close(code, reason)  # echo

    # --- end of life ---

    def _finish(self) -> None:
        """Bounded close handshake, then shutdown and close the socket (runs once, in the reader thread)."""
        try:
            if not self._dead:
                self.close(1000)
                while not self._recv_done and not self._dead:
                    try:
                        self._recv_message()  # data frames are dropped (we sent a close)
                    except _Fail as f:
                        self._fail(f.code, f.reason)
                    except _Gone:
                        self._recv_done = True
                if not self._dead:
                    try:
                        self._sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    self._drain_input()
        finally:
            self._abort()

    def _drain_input(self) -> None:
        # Read until the peer's EOF (bounded): closing with unread input would RST and could eat our close frame.
        end = self._deadline if self._deadline is not None else time.monotonic() + self._cfg.close_timeout
        while time.monotonic() < end and not self._dead:
            try:
                ready, _, _ = select.select([self._sock], [], [], min(_TICK, max(0.0, end - time.monotonic())))
                if ready and not self._sock.recv(65536):
                    return
            except (OSError, ValueError):
                return


def _tokens(value: str) -> set[str]:
    return {t.strip().lower() for t in value.split(",") if t.strip()}


class Upgrade(Takeover):
    """Response of a WebSocket route: validates the handshake, switches protocol, runs ``fn(ws, request)``."""

    def __init__(self, request: Request, fn: Callable[[WebSocket, Request], Any], cfg: _Config) -> None:
        self.request, self.fn, self.cfg = request, fn, cfg

    def _check(self, h: Any) -> tuple[str, str | None]:
        req, cfg = self.request, self.cfg
        hd = req.headers
        if req.method != "GET":
            raise HTTPError(405, None, "WebSocket requires GET", {"Allow": "GET"})
        if h.request_version != "HTTP/1.1":
            raise HTTPError(400, None, "WebSocket requires HTTP/1.1")
        if "websocket" not in _tokens(hd.get("upgrade", "")) or "upgrade" not in _tokens(hd.get("connection", "")):
            raise HTTPError(400, None, "not a WebSocket upgrade request")
        if hd.get("sec-websocket-version", "").strip() != "13":
            raise HTTPError(426, None, "unsupported WebSocket version", {"Sec-WebSocket-Version": "13"})
        key = hd.get("sec-websocket-key", "").strip()
        try:
            ok = len(base64.b64decode(key, validate=True)) == 16 and key.isascii()
        except (binascii.Error, ValueError):
            ok = False
        if not ok:
            raise HTTPError(400, None, "invalid Sec-WebSocket-Key")
        host = hd.get("host")
        if host is None:
            raise HTTPError(400, None, "missing Host header")
        if not h.server.host_allowed(host):
            raise HTTPError(403, "permission_error", "Host header not allowed")
        if "origin" in hd:
            origin = hd["origin"].strip().lower()
            if origin != f"http://{host.strip().lower()}" and origin not in cfg.origins:
                raise HTTPError(403, "permission_error", "Origin not allowed")
        chosen = None
        offered = [p.strip() for p in hd.get("sec-websocket-protocol", "").split(",") if p.strip()]
        for p in cfg.subprotocols:
            if p in offered:
                chosen = p
                break
        return key, chosen

    def run(self, h: Any) -> int:
        srv = h.server
        try:
            key, proto = self._check(h)
        except HTTPError as e:
            return h._send_json(_error_response(e))
        sock: socket.socket = h.connection
        leftover = self._take_buffered(h.rfile, sock)
        ws = WebSocket(sock, self.cfg, self.request, leftover, proto)
        with srv.cond:
            if srv.draining:
                refused = True
            else:
                refused = False
                srv.takeovers.add(ws)
        if refused:
            return h._send_json(_error_response(HTTPError(503, "overloaded_error", "server is stopping")))
        try:
            with ws._lock:  # abort_on_stop waits here: no frame can precede the 101 response
                h.send_response(101, "Switching Protocols")
                h.send_header("Upgrade", "websocket")
                h.send_header("Connection", "Upgrade")
                h.send_header("Sec-WebSocket-Accept", accept_key(key))
                if proto:
                    h.send_header("Sec-WebSocket-Protocol", proto)
                h.end_headers()
                ws._ready = True
            try:
                self.fn(ws, self.request)
            except WebSocketClosed:
                logger.debug("WebSocket closed during handler for %s", self.request.path, exc_info=True)
            except Exception:
                logger.error("WebSocket handler failed for %s", self.request.path, exc_info=True)
                ws.close(1011, "internal error")
        except OSError:
            return 499
        finally:
            with srv.cond:
                srv.takeovers.discard(ws)
            ws._finish()
        return 101

    @staticmethod
    def _take_buffered(rfile: Any, sock: socket.socket) -> bytes:
        # Bytes the HTTP layer already buffered (a client may send frames right behind the handshake). The
        # socket is made non-blocking so peek() never waits (a timed-out SocketIO would be unusable).
        sock.settimeout(0.0)
        try:
            data = rfile.peek(1)
            return rfile.read(len(data)) if data else b""
        except (BlockingIOError, OSError, ValueError):
            return b""


def upgrade(request: Request, fn: Callable[[WebSocket, Request], Any], *, max_frame: int = DEFAULT_MAX,
            max_message: int = DEFAULT_MAX, send_timeout: float = 10.0, close_timeout: float = 5.0,
            ping_interval: float | None = 30.0, idle_timeout: float | None = 90.0,
            origins: Iterable[str] = (), subprotocols: Iterable[str] = ()) -> Upgrade:
    """Return the response that upgrades ``request`` and runs ``fn(ws, request)`` (use inside a route)."""
    cfg = _Config(max_frame, max_message, send_timeout, close_timeout, ping_interval, idle_timeout,
                  frozenset(o.strip().lower() for o in origins), tuple(subprotocols))
    return Upgrade(request, fn, cfg)


def route(app: App, path: str, **opts: Any) -> Callable[[Callable[[WebSocket, Request], Any]],
                                                       Callable[[WebSocket, Request], Any]]:
    """Decorator: serve ``fn(ws, request)`` as a WebSocket endpoint at the exact ``path`` (GET only)."""
    def deco(fn: Callable[[WebSocket, Request], Any]) -> Callable[[WebSocket, Request], Any]:
        app.route("GET", path)(lambda req: upgrade(req, fn, **opts))
        return fn
    return deco
