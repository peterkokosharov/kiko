# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Blocking, thread-safe HTTP/1.1 client on http.client + ssl (no asyncio, no gzip, no HTTP/2).

Replaces httpx / aiohttp / requests (and certifi: the OS trust store is used).
``StdlibBackend`` keeps a keep-alive pool per (scheme, host, port), honours
HTTPS_PROXY / HTTP_PROXY / NO_PROXY, and streams bodies (SSE) line by line.
``client()`` returns the backend chosen by the factory (kind "http").

Secrets: exception messages and reprs never contain request headers or the URL
query (an API key may live in either); they name only host and port.

Platform notes:
- Windows / Linux: ssl.create_default_context uses the OS trust store.
- macOS: python.org builds may lack a CA file; then the system roots are exported
  with ``security find-certificate -a -p`` (see ``ca_context``).
- KIKO_CA_BUNDLE=<pem file> overrides the trust store everywhere.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import selectors
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from . import __version__, factory

DEFAULT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 10.0
MAX_LINE = 16 << 20  # longest SSE/body line iter_lines accepts
_MAC_KEYCHAINS = ("/System/Library/Keychains/SystemRootCertificates.keychain",
                  "/Library/Keychains/System.keychain")


class HttpError(Exception):
    """Base class of all kikolib.http errors."""


class ConnectError(HttpError):
    """DNS failure, refused connection, proxy tunnel failure or TLS handshake failure (retryable)."""


class TLSVerifyError(HttpError):
    """Certificate verification failed (wrong host, untrusted or expired certificate); not retryable."""


class ConnectionLost(HttpError):
    """The connection broke after the connect phase (request being sent, or no/partial response head).

    The request may or may not have reached the server. The backend resends once, and only
    when SENDING fails on a reused keep-alive connection; failures while waiting for the
    reply are never resent by the backend (send_with_retry in retry.py may retry them).
    """


class HttpTimeout(HttpError):
    """Connect or read timeout."""


def ca_context() -> ssl.SSLContext:
    """SSL context (verifying, hostname-checking) trusting the OS store; KIKO_CA_BUNDLE overrides."""
    bundle = os.environ.get("KIKO_CA_BUNDLE")
    if bundle:
        return ssl.create_default_context(cafile=bundle)
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and not _has_default_cas(ctx):
        pems = []
        for kc in _MAC_KEYCHAINS:
            try:
                out = subprocess.run(["security", "find-certificate", "-a", "-p", kc],
                                     capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode == 0:
                pems.append(out.stdout)
        data = "".join(pems)
        if data.strip():
            ctx.load_verify_locations(cadata=data)
    return ctx


def _has_default_cas(ctx: ssl.SSLContext) -> bool:
    if ctx.cert_store_stats().get("x509_ca", 0) > 0:
        return True
    capath = ssl.get_default_verify_paths().capath
    return bool(capath and os.path.isdir(capath) and os.listdir(capath))


class Headers:
    """Case-insensitive, read-only response headers (repeated fields joined with ", ")."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._d: dict[str, str] = {}
        self._names: dict[str, str] = {}
        for k, v in pairs:
            lk = k.lower()
            self._d[lk] = f"{self._d[lk]}, {v}" if lk in self._d else v
            self._names.setdefault(lk, k)

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._d.get(name.lower(), default)

    def __getitem__(self, name: str) -> str:
        return self._d[name.lower()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._d

    def __iter__(self) -> Iterator[str]:
        return iter(self._names.values())

    def __len__(self) -> int:
        return len(self._d)

    def items(self) -> list[tuple[str, str]]:
        return [(self._names[k], v) for k, v in self._d.items()]


class Response:
    """Status, headers and a body that is read at once (``read``) or by line (``iter_lines``).

    With ``stream=True`` the connection is returned to the pool only once the body
    was read to its end; ``close()`` (or leaving the ``with`` block) discards it.
    """

    def __init__(self, status: int, reason: str, headers: Headers, src: Any, release: Any = None,
                 sock: Any = None) -> None:
        self.status = status
        self.reason = reason
        self.headers = headers
        self._src = src
        self._release = release  # callable(reuse: bool) or None
        self._sock = sock  # the underlying socket, only used by abort()
        self._aborted = False
        self._body: bytes | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.reason}>"

    def _finish(self, reuse: bool) -> None:
        with self._lock:
            rel, self._release = self._release, None
            reuse = reuse and not self._aborted
        if rel is not None:
            rel(reuse)

    def abort(self) -> None:
        """Wake a thread blocked in a read of this response; safe to call from another thread.

        Shuts the socket down (no buffer lock is taken, so it cannot block behind the reader). The blocked
        read then ends with ConnectionLost. The connection is never returned to the pool; the thread that
        reads still owns ``close()``, which releases the socket.

        A no-op once the response is finished or closed (its socket may be back in the pool or in use by
        another request). Known limits: on Windows a TLS socket is only shut down, not closed (closing the
        handle under a thread inside SSL_read risks a handle-reuse race), so a blocked reader may live until
        the read timeout; on Linux shutdown() wakes the read. Not applicable before the response head arrived.
        """
        with self._lock:
            if self._release is None or self._sock is None:
                return
            self._aborted = True
            sock = self._sock
            # Shut down under the lock: _finish (which may return the socket to the pool) waits for us.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass
            if sys.platform == "win32" and not isinstance(sock, ssl.SSLSocket):
                # Winsock does not wake a recv/poll blocked in another thread on shutdown() alone; only closing
                # the handle does. sock.close() is deferred while makefile() holds a reference, so close the
                # handle directly (the reader's later close() is then a harmless no-op).
                try:
                    sock._real_close()
                except (AttributeError, OSError):
                    pass

    def _lost(self) -> HttpError:
        return ConnectionLost("connection aborted")

    def read(self) -> bytes:
        """The whole body (cached; a second call returns the same bytes)."""
        if self._body is None:
            try:
                self._body = self._src.read()
            except BaseException as exc:
                self._finish(False)
                raise (self._lost() if self._aborted else _map_io_error(exc)) from None
            self._finish(True)
        return self._body

    def iter_lines(self) -> Iterator[str]:
        """Decoded lines without the line terminator (\\n or \\r\\n), as they arrive.

        A line may be any length up to MAX_LINE (then HttpError); it is decoded only once complete.
        """
        buf = bytearray()
        while True:
            try:
                raw = self._src.readline(1 << 20)
            except BaseException as exc:
                self._finish(False)
                err = self._lost() if self._aborted else _map_io_error(exc)
            else:
                err = self._lost() if self._aborted else None
            if err is not None:
                self._finish(False)
                raise err from None
            buf += raw
            if raw and not raw.endswith(b"\n"):
                if len(buf) > MAX_LINE:
                    self._finish(False)
                    raise HttpError("response line too long")
                continue
            if buf:
                yield bytes(buf).rstrip(b"\r\n").decode("utf-8", errors="replace")
                buf.clear()
            if not raw:
                self._finish(True)
                return

    def json(self) -> Any:
        return json.loads(self.read().decode("utf-8"))

    def close(self) -> None:
        self._finish(False)
        try:
            self._src.close()
        except Exception:
            pass

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _map_io_error(exc: BaseException) -> BaseException:
    """Translate socket-level errors into HttpError; pass anything else through."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return HttpTimeout("read timed out")
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return HttpError(f"connection error: {type(exc).__name__}")
    return exc


class HttpBackend(Protocol):
    """What the rest of kiko-lite needs from an HTTP client."""

    def request(self, method: str, url: str, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, timeout: float = DEFAULT_TIMEOUT,
                stream: bool = False) -> Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    auth: str | None = field(default=None, repr=False)  # "Basic ..." header value (a secret)


def proxy_for(scheme: str, host: str) -> Proxy | None:
    """Proxy from HTTPS_PROXY / HTTP_PROXY (either case), honouring NO_PROXY; None if direct."""
    proxies = urllib.request.getproxies_environment()
    raw = proxies.get(scheme)
    if not raw or urllib.request.proxy_bypass_environment(host, proxies):
        return None
    u = urlsplit(raw if "://" in raw else f"http://{raw}")
    if not u.hostname:
        return None
    auth = None
    if u.username is not None:
        cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
        auth = "Basic " + base64.b64encode(cred).decode("ascii")
    return Proxy(u.hostname, u.port or (443 if u.scheme == "https" else 80), auth)


def _is_dropped(conn: http.client.HTTPConnection) -> bool:
    """True if an idle pooled connection is unusable: closed, EOF from the peer, or stray bytes."""
    sock = conn.sock
    if sock is None:
        return True
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(sock, selectors.EVENT_READ)
            return bool(sel.select(0))
    except (OSError, ValueError):
        return True


class StdlibBackend:
    """Pooled http.client backend; safe to share between threads."""

    def __init__(self, connect_timeout: float = CONNECT_TIMEOUT, max_idle: int = 4,
                 idle_ttl: float = 30.0, user_agent: str | None = None) -> None:
        self.connect_timeout = connect_timeout
        self.max_idle = max_idle
        self.idle_ttl = idle_ttl
        self.user_agent = user_agent or f"kiko-lite/{__version__}"
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, int], list[tuple[http.client.HTTPConnection, float]]] = {}
        self._ctx: ssl.SSLContext | None = None
        self._ctx_lock = threading.Lock()
        self._closed = False

    def __repr__(self) -> str:
        with self._lock:
            return f"<StdlibBackend idle={sum(len(v) for v in self._idle.values())}>"

    # -- pool ---------------------------------------------------------------
    def _checkout(self, key: tuple[str, str, int]) -> http.client.HTTPConnection | None:
        now = time.monotonic()
        stale: list[http.client.HTTPConnection] = []
        conn = None
        with self._lock:
            lst = self._idle.get(key, [])
            while lst:
                c, ts = lst.pop()
                if now - ts <= self.idle_ttl and not _is_dropped(c):
                    conn = c
                    break
                stale.append(c)
        for c in stale:
            c.close()
        return conn

    def _checkin(self, key: tuple[str, str, int], conn: http.client.HTTPConnection) -> None:
        with self._lock:
            lst = self._idle.setdefault(key, [])
            if not self._closed and len(lst) < self.max_idle:
                lst.append((conn, time.monotonic()))
                return
        conn.close()

    def _new_conn(self, scheme: str, host: str, port: int, proxy: Proxy | None) -> http.client.HTTPConnection:
        if scheme == "https":
            with self._ctx_lock:
                if self._ctx is None:
                    self._ctx = ca_context()
            if proxy:
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    proxy.host, proxy.port, timeout=self.connect_timeout, context=self._ctx)
                conn.set_tunnel(host, port, {"Proxy-Authorization": proxy.auth} if proxy.auth else None)
            else:
                conn = http.client.HTTPSConnection(host, port, timeout=self.connect_timeout, context=self._ctx)
        elif proxy:
            conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=self.connect_timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
        return conn

    # -- request ------------------------------------------------------------
    def request(self, method: str, url: str, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, timeout: float = DEFAULT_TIMEOUT,
                stream: bool = False) -> Response:
        """One request. What ``timeout`` (seconds) covers:

        * connecting (TCP and TLS handshake) uses ``connect_timeout`` (10 s), not ``timeout``;
        * after that ``timeout`` is a per-operation socket timeout: it limits sending the request, then the wait
          for the response head (time to first byte), then every single read of the body (idle time between two
          chunks). It is NOT a total limit: a streaming answer that keeps sending bytes is never cut, however long
          it takes; a silent upstream is cut ``timeout`` seconds after its last byte.
        """
        u = urlsplit(url)
        scheme = u.scheme.lower()
        if scheme not in ("http", "https") or not u.hostname:
            raise HttpError("unsupported or malformed URL (need http:// or https:// and a host)")
        host = u.hostname
        port = u.port or (443 if scheme == "https" else 80)
        target = (u.path or "/") + (f"?{u.query}" if u.query else "")
        hdrs = {"Accept-Encoding": "identity", "User-Agent": self.user_agent}
        for k, v in (headers or {}).items():
            for hk in [h for h in hdrs if h.lower() == k.lower()]:
                del hdrs[hk]
            hdrs[k] = v
        proxy = proxy_for(scheme, host)
        via_proxy = proxy if scheme == "http" else None
        key = (scheme, host, port)
        for attempt in (0, 1):
            conn = self._checkout(key)
            reused = conn is not None
            phase = "connect"
            err: BaseException | None = None
            try:
                if conn is None:
                    conn = self._new_conn(scheme, host, port, proxy)
                    conn.connect()
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                req_target, req_hdrs = target, hdrs
                if via_proxy:  # plain http through a proxy: absolute-form target
                    req_target = f"http://{u.netloc.rpartition('@')[2]}{target}"
                    req_hdrs = dict(hdrs)
                    req_hdrs.setdefault("Host", u.netloc.rpartition("@")[2])
                    if via_proxy.auth:
                        req_hdrs.setdefault("Proxy-Authorization", via_proxy.auth)
                phase = "send"
                conn.request(method, req_target, body=body, headers=req_hdrs)
                phase = "recv"
                resp = conn.getresponse()
            except BaseException as exc:
                if conn is not None:
                    conn.close()
                if (reused and attempt == 0 and phase == "send" and isinstance(
                        exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                              http.client.CannotSendRequest, ssl.SSLEOFError))):
                    continue  # send failed on a pooled connection: use a fresh one
                err = self._map_error(exc, host, port, phase)
            if err is not None:
                raise err  # raised outside the except block: no __context__ that could hold a secret
            return self._wrap(key, conn, resp, stream, method)
        raise HttpError("unreachable")  # pragma: no cover

    @staticmethod
    def _map_error(exc: BaseException, host: str, port: int, phase: str) -> BaseException:
        """Map to an HttpError naming only host:port; never the original message (may hold a secret)."""
        where = f"{host}:{port}"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return HttpTimeout(f"timed out talking to {where}")
        if isinstance(exc, ssl.SSLCertVerificationError):
            return TLSVerifyError(f"certificate verification failed for {where}")
        if isinstance(exc, ssl.SSLError):
            return ConnectError(f"TLS error with {where}: {type(exc).__name__}")
        if isinstance(exc, socket.gaierror):
            return ConnectError(f"cannot resolve {where}")
        if isinstance(exc, OSError):
            if phase == "connect":
                return ConnectError(f"cannot connect to {where}: {type(exc).__name__}")
            return ConnectionLost(f"connection to {where} lost: {type(exc).__name__}")
        if isinstance(exc, http.client.HTTPException):
            return HttpError(f"malformed response from {where}: {type(exc).__name__}")
        if isinstance(exc, Exception):  # ValueError / UnicodeEncodeError from a bad header or URL
            return HttpError("invalid request header or URL")
        return exc

    def _wrap(self, key: tuple[str, str, int], conn: http.client.HTTPConnection,
              resp: http.client.HTTPResponse, stream: bool, method: str) -> Response:
        headers = Headers(resp.getheaders())

        def release(reuse: bool) -> None:
            if reuse and not resp.will_close and resp.isclosed():
                self._checkin(key, conn)
            else:
                conn.close()

        if stream:
            return Response(resp.status, resp.reason, headers, resp, release, sock=conn.sock)
        try:
            data = resp.read()
        except BaseException as exc:
            conn.close()
            raise _map_io_error(exc) from None
        release(True)
        return Response(resp.status, resp.reason, headers, io.BytesIO(data))

    def close(self) -> None:
        """Close all idle connections (in-flight ones are closed by their responses)."""
        with self._lock:
            self._closed = True
            conns = [c for lst in self._idle.values() for c, _ in lst]
            self._idle.clear()
        for c in conns:
            c.close()


def _register() -> None:
    factory.register("http", "stdlib", probe=lambda: (True, "http.client + ssl"),
                     load=StdlibBackend, priority=10)


_register()
from . import winhttp as _winhttp  # noqa: E402,F401  (registers the "winhttp" backend; import is safe off Windows)


def client() -> HttpBackend:
    """The HTTP backend chosen by the factory (kind "http")."""
    return factory.choose("http")
