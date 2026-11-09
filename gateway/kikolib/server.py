# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Threaded HTTP/1.1 server and router for kiko-proxy (http.server + threads, no asyncio).

Replaces (long term): ``fastapi`` (+starlette), ``uvicorn`` (+h11, click) and ``python-multipart``.
One thread per connection (daemon threads); requests are long (minutes of SSE) and concurrency is
small, so threads are enough. Bind 127.0.0.1 by default.

Handlers take a ``Request`` and return ``JsonResponse`` or ``StreamResponse``; they raise
``HTTPError`` for Anthropic-shaped errors ``{"type":"error","error":{"type":..,"message":..}}``.
Streams use chunked transfer encoding over HTTP/1.1 (Node fetch handles it natively, keeps the
connection reusable and a truncated stream is detectable); HTTP/1.0 clients get close-delimited bodies.

``serve`` is the blocking entry point (signals, systemd notify/watchdog, RSS limit, graceful drain);
``Server`` is the object form (``start`` / ``stop``) used by tests.
Chunked *request* bodies are not supported (411); no TLS or HTTP/2. WebSockets: see ``kikolib.websocket``
(a handler returns a ``Takeover`` that owns the connection; it counts as an active request and is closed
with 1001 on ``request_stop``).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from . import config, factory, posix

logger = logging.getLogger("kikolib.server")

DEFAULT_MAX_BODY = 32 * 1024 * 1024
RSS_CHECK_SECONDS = 30.0
RSS_EXIT_CODE = 75  # EX_TEMPFAIL: non-zero so systemd Restart=on-failure restarts the service
_TAKEOVER_STOP_SECONDS = 3.0  # shared deadline for telling all upgraded connections to close on stop
_BODY_METHODS = ("POST", "PUT", "PATCH")
_ERROR_TYPES = {
    400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
    404: "not_found_error", 405: "invalid_request_error", 411: "invalid_request_error",
    413: "request_too_large", 415: "invalid_request_error", 426: "invalid_request_error", 429: "rate_limit_error", 529: "overloaded_error",
}


class HTTPError(Exception):
    """Raised by a handler (or a before hook) to answer with an Anthropic-shaped error."""

    def __init__(self, status: int, error_type: str | None = None, message: str = "",
                 headers: Mapping[str, str] | None = None) -> None:
        self.status = status
        self.error_type = error_type or _ERROR_TYPES.get(status, "api_error")
        self.message = message
        self.headers = dict(headers or {})
        super().__init__(f"{status} {self.error_type}: {message}")


class Headers(Mapping[str, str]):
    """Read-only case-insensitive header mapping (repeated headers are joined with ", ")."""

    def __init__(self, pairs: Iterable[tuple[str, str]] = ()) -> None:
        self._d: dict[str, str] = {}
        for k, v in pairs:
            k = k.lower()
            self._d[k] = f"{self._d[k]}, {v}" if k in self._d else v

    def __getitem__(self, key: str) -> str:
        return self._d[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._d


@dataclass
class Request:
    """Parsed HTTP request. ``params`` holds ``{name}`` path parameters."""

    method: str
    path: str
    query: dict[str, str]
    headers: Headers
    body: bytes = b""
    client: tuple[str, int] = ("", 0)
    params: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Decode the body as JSON; HTTPError 400 when it is not valid JSON."""
        try:
            return json.loads(self.body)
        except (ValueError, RecursionError):  # a deeply nested body raises RecursionError
            raise HTTPError(400, "invalid_request_error", "request body is not valid JSON") from None


class JsonResponse:
    def __init__(self, data: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> None:
        self.data, self.status, self.headers = data, status, dict(headers or {})


class StreamResponse:
    """Streamed body: ``chunks`` yields bytes (str is utf-8 encoded); its close() runs when done or aborted."""

    def __init__(self, chunks: Iterable[bytes | str], status: int = 200,
                 headers: Mapping[str, str] | None = None, content_type: str = "text/event-stream") -> None:
        self.chunks, self.status, self.headers, self.content_type = chunks, status, dict(headers or {}), content_type


class Takeover:
    """Response that takes over the connection (WebSocket). ``run(handler)`` owns the socket until it
    returns the status for the ``after`` hooks; the connection is closed afterwards. Objects added to
    ``_HTTPServer.takeovers`` get ``abort_on_stop()`` called by ``Server.request_stop``."""

    def run(self, handler: Any) -> int:
        raise NotImplementedError

    def abort_on_stop(self) -> None:
        pass


Handler = Callable[[Request], "JsonResponse | StreamResponse | Takeover"]


class App:
    """Route table plus before hooks."""

    def __init__(self, max_body: int = DEFAULT_MAX_BODY) -> None:
        self.max_body = max_body
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self._before: list[Callable[[Request], None]] = []
        self._after: list[Callable[[Request, int, float], None]] = []
        self.browser_guard = False  # True: reject Origin / foreign Host / non-JSON POST (see _Handler._guard)
        self.allow_same_origin = False  # with browser_guard: accept an Origin equal to http://<Host> (own web UI)
        self.same_origin_paths: tuple[str, ...] = ()  # with browser_guard: same, but only for GET of these prefixes

    def route(self, method: str, path: str) -> Callable[[Handler], Handler]:
        """Decorator: exact path, or ``{name}`` segments (``/v1/models/{id}:generateContent``).

        ``{name}`` matches one segment ([^/]+); ``{name:path}`` matches the rest of the path (.+).
        Custom ``{name:regex}`` patterns are rejected (ValueError): user regexes risk ReDoS and surprises.
        """
        pattern = ""
        for p in re.split(r"(\{[A-Za-z_]\w*(?::[^}]*)?\})", path):
            if not p:
                continue
            if p.startswith("{") and p.endswith("}"):
                name, colon, kind = p[1:-1].partition(":")
                if not colon:
                    pattern += f"(?P<{name}>[^/]+)"
                elif kind == "path":
                    pattern += f"(?P<{name}>.+)"
                else:
                    raise ValueError(f"unsupported route pattern {{{name}:{kind}}} (only :path)")
            else:
                pattern += re.escape(p)
        rx = re.compile(pattern + r"\Z")

        def deco(fn: Handler) -> Handler:
            self._routes.append((method.upper(), rx, fn))
            return fn
        return deco

    def before(self, fn: Callable[[Request], None]) -> Callable[[Request], None]:
        """Run ``fn(request)`` before the handler (auth); it may raise HTTPError."""
        self._before.append(fn)
        return fn

    def after(self, fn: Callable[[Request, int, float], None]) -> Callable[[Request, int, float], None]:
        """Run ``fn(request, status, seconds)`` after every request (errors are logged, not raised)."""
        self._after.append(fn)
        return fn

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        allowed: list[str] = []
        for m, rx, fn in self._routes:
            hit = rx.match(path)
            if hit is None:
                continue
            if m == method:
                return fn, hit.groupdict()
            allowed.append(m)
        if allowed:
            raise HTTPError(405, None, f"method {method} not allowed", {"Allow": ", ".join(sorted(set(allowed)))})
        raise HTTPError(404, None, f"no route for {path}")


def _error_response(e: HTTPError) -> JsonResponse:
    body = {"type": "error", "error": {"type": e.error_type, "message": e.message}}
    return JsonResponse(body, e.status, e.headers)


def _merge_headers(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """Overlay handler headers case-insensitively; framing headers are ours; CR/LF is rejected."""
    out = dict(base)
    for k, v in extra.items():
        k, v = str(k), str(v)
        if any(c in k or c in v for c in "\r\n"):
            raise ValueError("response header contains CR or LF")
        lk = k.lower()
        if lk in ("content-length", "transfer-encoding"):
            continue
        for old in [o for o in out if o.lower() == lk]:
            del out[old]
        out[k] = v
    return out


class _ClientGone(OSError):
    """The client vanished or timed out while its body was being read."""


def _next_chunk(it: Iterator[Any]) -> bytes | None:
    for c in it:
        if c:
            return c.encode("utf-8") if isinstance(c, str) else bytes(c)
    return None


def _host_only(value: str) -> str:
    """Host header -> bare lowercase host (port stripped, IPv6 brackets removed)."""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    if v.count(":") == 1:
        v = v.split(":", 1)[0]
    return v.rstrip(".")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("closing stream source failed", exc_info=True)


LINGER_SECONDS = 1.0  # a rejection that left the request body unread: how long we read it off before closing
LINGER_BYTES = 64 * 1024


def _lingering_close(sock: Any, seconds: float = LINGER_SECONDS, limit: int = LINGER_BYTES) -> None:
    """Close without a TCP reset: the answer is sent; send our FIN, then read (and drop) what the client is still
    sending, for at most ``seconds`` and ``limit`` bytes. Closing a socket that holds unread data makes the OS send
    an RST, which can destroy the response before the client has read it. Never raises."""
    try:
        sock.shutdown(socket.SHUT_WR)
        end, got = time.monotonic() + seconds, 0
        while got < limit:
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            chunk = sock.recv(min(8192, limit - got))
            if not chunk:
                break
            got += len(chunk)
    except (OSError, ValueError):
        pass


_LINGERERS = 16  # cap on concurrent lingering closes of 503 connections


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name == "posix"  # on Windows SO_REUSEADDR lets another process share the port
    request_queue_size = 64

    def __init__(self, addr: tuple[str, int], app: App, idle_timeout: float, max_connections: int = 64,
                 allowed_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed_hosts = frozenset(h.strip().lower().strip("[]") for h in allowed_hosts if h.strip())
        self._slots = threading.BoundedSemaphore(max(1, max_connections))
        self._lingerers = threading.BoundedSemaphore(_LINGERERS)  # concurrent 503 lingering closes
        self.idle_timeout = idle_timeout
        self.draining = False
        self.hook: Callable[[], None] | None = None
        self.active = 0
        self.takeovers: set[Takeover] = set()  # live upgraded connections (guarded by cond)
        self.cond = threading.Condition()
        super().__init__(addr, _Handler)

    def service_actions(self) -> None:  # runs in the serve_forever thread on every poll
        if self.hook is not None:
            self.hook()

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("connection error", exc_info=True)

    # Connection cap: one thread per connection is unbounded, so a semaphore limits live connections (idle
    # keep-alive ones included); an excess connection gets a quick 503 and is closed.
    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            body = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                          "message": "too many connections"}}).encode()
            try:
                request.settimeout(1.0)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Retry-After: 1\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%b"
                                % (len(body), body))
            except OSError:
                self.shutdown_request(request)
                return
            # The accept thread never waits for the client: the lingering close (needed so unread request bytes do
            # not turn the close into an RST that destroys the 503) runs on a short-lived daemon thread. At most
            # _LINGERERS run at once; beyond that the socket is just closed (a flood gets no courtesy).
            if self._lingerers.acquire(blocking=False):
                try:
                    threading.Thread(target=self._linger_and_close, args=(request,), name="kiko-503-linger",
                                     daemon=True).start()
                    return
                except BaseException:  # could not start a thread
                    self._lingerers.release()
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def _linger_and_close(self, request: Any) -> None:
        try:
            _lingering_close(request, 0.2)
        finally:
            self._lingerers.release()
            self.shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def host_allowed(self, header: str) -> bool:
        """DNS-rebinding check. Loopback bind: only loopback names. Other bind: loopback, the bound address, a
        name in ``allowed_hosts``; a wildcard bind (0.0.0.0 / ::) also takes any IP literal."""
        host = _host_only(header)
        if _is_loopback(host):
            return True
        bound = str(self.server_address[0]).lower().strip("[]")
        if _is_loopback(bound):
            return False
        if host == bound or host in self.allowed_hosts:
            return True
        return _is_ip(host) and bound in ("", "0.0.0.0", "::")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kiko-lite"
    sys_version = ""
    disable_nagle_algorithm = True  # TCP_NODELAY: every SSE chunk goes out at once
    server: _HTTPServer

    def setup(self) -> None:
        self.timeout = self.server.idle_timeout
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        try:
            self._send_json(_error_response(HTTPError(code, None, message or self.responses.get(code, ("", ""))[0])))
        except OSError:
            pass

    def do_GET(self) -> None:
        self._dispatch()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    # --- request flow ---

    def _dispatch(self) -> None:
        srv, t0 = self.server, time.monotonic()
        split = urlsplit(self.path)
        req = Request(self.command, unquote(split.path), dict(parse_qsl(split.query, keep_blank_values=True)),
                      Headers(self.headers.items()), b"", self.client_address)
        status = 500
        self._body_unread = False
        with srv.cond:
            srv.active += 1
        try:
            status = self._run(req)
        except OSError:  # client went away or timed out
            self.close_connection = True
            status = 499
        except Exception:
            logger.error("unhandled error in %s %s", req.method, req.path, exc_info=True)
            self.close_connection = True
        finally:
            for fn in srv.app._after:
                try:
                    fn(req, status, time.monotonic() - t0)
                except Exception:
                    logger.error("after hook failed", exc_info=True)
            with srv.cond:
                srv.active -= 1
                srv.cond.notify_all()
        if srv.draining:
            self.close_connection = True
        if self._body_unread and self.close_connection:
            _lingering_close(self.connection)  # the answer is out; let the client finish sending before we close

    def _run(self, req: Request) -> int:
        app = self.server.app
        try:
            self._body_unread = ("transfer-encoding" in req.headers
                                 or req.headers.get("content-length", "0").strip() not in ("", "0"))
            if app.browser_guard:
                self._guard(req)
            req.body = self._read_body(req, app.max_body)
            self._body_unread = False
            fn, req.params = app.match(req.method, req.path)
            for hook in app._before:
                hook(req)
            resp = fn(req)
            if not isinstance(resp, (JsonResponse, StreamResponse, Takeover)):
                raise TypeError(f"handler returned {type(resp).__name__}")
        except HTTPError as e:
            resp = _error_response(e)
        except _ClientGone:
            raise
        except Exception:  # includes handler-side OSError (e.g. upstream failure): a 500, not a client disconnect
            logger.error("unhandled error in %s %s", req.method, req.path, exc_info=True)
            resp = _error_response(HTTPError(500, "api_error", "internal server error"))
        if isinstance(resp, Takeover):
            self.close_connection = True
            self._body_unread = False  # the Takeover owns the socket
            return resp.run(self)
        if isinstance(resp, StreamResponse):
            return self._send_stream(resp)
        return self._send_json(resp)

    # Browser/DNS-rebinding guard. It lives here (not in a before hook) because the Host check needs the bound
    # address, and it runs once for every route, before the body is read. Apps opt in via ``App.browser_guard``.
    def _guard(self, req: Request) -> None:
        h = req.headers
        try:
            host = h.get("host")
            if "origin" in h:  # Claude Code, curl and the SDKs send none; any browser page does
                # Only an app that serves its own browser UI (App.allow_same_origin) accepts the Origin of
                # its own page: scheme http and exactly the Host the request was sent to (Host is checked
                # below, so a rebinding name is still refused). Any other origin, and "null", stays 403.
                app = self.server.app
                own = ((app.allow_same_origin or (req.method == "GET" and any(
                           req.path == p or req.path.startswith(p.rstrip("/") + "/") for p in app.same_origin_paths)))
                       and host is not None and h["origin"].strip().lower() == f"http://{host.strip().lower()}")
                if not own:
                    raise HTTPError(403, "permission_error", "browser requests (Origin header) are not allowed")
            if host is None:
                if self.request_version == "HTTP/1.1":
                    raise HTTPError(400, "invalid_request_error", "Host header required")
            elif not self.server.host_allowed(host):
                raise HTTPError(403, "permission_error", "Host header not allowed")
            if req.method == "POST":
                ctype = h.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    raise HTTPError(415, "invalid_request_error", "Content-Type must be application/json")
        except HTTPError:
            self.close_connection = True  # the body is not read: do not reuse the connection
            raise

    def _read_body(self, req: Request, max_body: int) -> bytes:
        h = req.headers
        if "transfer-encoding" in h:
            self.close_connection = True
            raise HTTPError(411, None, "chunked request bodies are not supported; send Content-Length")
        raw = h.get("content-length")
        if raw is None:
            if req.method in _BODY_METHODS:
                self.close_connection = True
                raise HTTPError(411, None, "Content-Length required")
            return b""
        if not raw.isascii() or not raw.isdigit():
            self.close_connection = True
            raise HTTPError(400, None, "invalid Content-Length")
        n = int(raw)
        if n > max_body:
            self.close_connection = True
            raise HTTPError(413, None, f"request body exceeds {max_body} bytes")
        try:
            data = self.rfile.read(n) if n else b""
        except OSError:
            self.close_connection = True
            raise _ClientGone from None
        if len(data) < n:
            self.close_connection = True
            raise HTTPError(400, None, "incomplete request body")
        return data

    # --- responses ---

    def _head(self, status: int, headers: dict[str, str]) -> None:
        if self.server.draining:
            self.close_connection = True
        if self.close_connection and not any(k.lower() == "connection" for k in headers):
            headers["Connection"] = "close"
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def _send_json(self, resp: JsonResponse) -> int:
        body = json.dumps(resp.data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        base = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        try:
            headers = _merge_headers(base, resp.headers)
        except ValueError:
            logger.error("handler returned an invalid response header (CR/LF); answering 500")
            body = json.dumps(_error_response(HTTPError(500, "api_error", "internal server error")).data,
                              separators=(",", ":")).encode("utf-8")
            resp = JsonResponse(None, 500)
            headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        self._head(resp.status, headers)
        if self.command != "HEAD":
            self.wfile.write(body)
        return resp.status

    def _send_stream(self, resp: StreamResponse) -> int:
        it = iter(resp.chunks)
        chunked = self.request_version == "HTTP/1.1"
        try:
            try:  # pull the first chunk before the status line so an early failure can still be a real error
                chunk = _next_chunk(it)
            except HTTPError as e:
                return self._send_json(_error_response(e))
            except Exception:
                logger.error("stream source failed before first chunk", exc_info=True)
                return self._send_json(_error_response(HTTPError(500, "api_error", "internal server error")))
            base = {"Content-Type": resp.content_type, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            try:
                headers = _merge_headers(base, resp.headers)
            except ValueError:
                logger.error("handler returned an invalid response header (CR/LF); answering 500")
                return self._send_json(_error_response(HTTPError(500, "api_error", "internal server error")))
            if chunked:
                headers["Transfer-Encoding"] = "chunked"
            else:
                self.close_connection = True
            self._head(resp.status, headers)
            while chunk is not None:
                self.wfile.write(b"%x\r\n%b\r\n" % (len(chunk), chunk) if chunked else chunk)
                try:
                    chunk = _next_chunk(it)
                except Exception:  # abort without the terminating chunk so the client sees a truncated stream
                    logger.error("stream source failed mid-stream", exc_info=True)
                    self.close_connection = True
                    return resp.status
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
            return resp.status
        except OSError:
            self.close_connection = True
            return 499
        finally:
            _close(it)
            if resp.chunks is not it:
                _close(resp.chunks)


class Server:
    """Threaded server object. ``start()`` runs it in a thread; ``stop()`` drains and closes.

    The socket is bound in the constructor (port 0 picks a free port; see ``port``).
    ``request_stop`` / ``stop`` must not be called from the thread running ``serve_forever``.
    """

    def __init__(self, app: App, host: str | None = None, port: int | None = None, *,
                 drain_seconds: float = 30.0, idle_timeout: float = 300.0, max_connections: int = 64,
                 allowed_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.drain_seconds = drain_seconds
        host = config.proxy_host() if host is None else host  # no value: the proxy address of kiko.toml
        port = config.proxy_port() if port is None else port
        self._httpd = _HTTPServer((host, port), app, idle_timeout, max_connections, allowed_hosts)
        self._lock = threading.Lock()
        self._stopping = False
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_forever(self) -> None:
        """Accept connections in the calling thread until ``request_stop``."""
        with self._lock:
            if self._stopping:
                return
            self._running.set()
        self._httpd.serve_forever(poll_interval=0.1)

    def start(self) -> Server:
        self._thread = threading.Thread(target=self.serve_forever, name="kikolib-server", daemon=True)
        self._thread.start()
        return self

    def request_stop(self) -> None:
        """Stop accepting (idempotent; blocks until the accept loop exited).

        Idle keep-alive connections are not closed here (their daemon threads die with the process);
        any response sent from now on carries ``Connection: close``.
        """
        with self._lock:
            self._stopping = True
            running = self._running.is_set()
        self._httpd.draining = True
        with self._httpd.cond:
            live = list(self._httpd.takeovers)
        # Upgraded connections (WebSocket) are told to close (1001) in parallel with one shared deadline, so the
        # time does not grow with their number (a stuck one is abandoned: it is dropped by its own timeouts).
        def _abort_one(t: Takeover) -> None:
            try:
                t.abort_on_stop()
            except Exception:
                logger.debug("takeover stop failed", exc_info=True)

        workers = [threading.Thread(target=_abort_one, args=(t,), daemon=True, name="kikolib-takeover-stop")
                   for t in live]
        for w in workers:
            w.start()
        end = time.monotonic() + _TAKEOVER_STOP_SECONDS
        for w in workers:
            w.join(max(0.0, end - time.monotonic()))
        if running:
            self._httpd.shutdown()

    def drain(self, timeout: float | None = None) -> bool:
        """Wait until no request is in flight; False on timeout (default: ``drain_seconds``)."""
        h = self._httpd
        with h.cond:
            ok = h.cond.wait_for(lambda: h.active == 0, self.drain_seconds if timeout is None else timeout)
            if not ok:
                logger.warning("drain timed out with %d request(s) still in flight", h.active)
            return ok

    def on_poll(self, fn: Callable[[], None] | None) -> None:
        """Call ``fn()`` from the accept-loop thread on every poll (~0.1 s)."""
        self._httpd.hook = fn

    def close(self) -> None:
        self._httpd.server_close()

    def stop(self) -> None:
        """Graceful stop: stop accepting, wait for in-flight requests, close the socket."""
        self.request_stop()
        if self._thread is not None:
            self._thread.join()
        self.drain()
        self.close()

    def __enter__(self) -> Server:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def serve(app: App, host: str | None = None, port: int | None = None, *, ready: Callable[[Server], None] | None = None,
          drain_seconds: float = 30.0, rss_limit_mb: float | None = None, max_connections: int = 64,
          allowed_hosts: Iterable[str] | None = None) -> None:
    """Run ``app`` until SIGTERM/SIGINT (blocking), then drain in-flight requests for ``drain_seconds``.

    Sends READY=1 after bind, STOPPING=1 on shutdown, WATCHDOG=1 when systemd enables it. With
    ``rss_limit_mb`` a process over the limit shuts down gracefully and exits with SystemExit(75)
    so ``Restart=on-failure`` restarts it. ``ready(server)`` is called once listening.
    Signal handlers are installed only when called from the main thread.
    """
    if allowed_hosts is None:  # extra Host names for a non-loopback bind: KIKO_ALLOWED_HOSTS=a.example,b.example
        allowed_hosts = os.environ.get("KIKO_ALLOWED_HOSTS", "").split(",")
    srv = Server(app, host, port, drain_seconds=drain_seconds, max_connections=max_connections,
                 allowed_hosts=allowed_hosts)
    done = threading.Event()
    reasons: list[str] = []
    stopper: list[threading.Thread] = []

    def graceful(why: str) -> None:
        posix.sd_notify("STOPPING=1")
        srv.request_stop()

    def trigger(why: str) -> None:  # signal-handler safe: no locks, no threads; just a flag
        if not reasons:
            reasons.append(why)

    def poll() -> None:  # accept-loop thread: turn the flag into the stop thread, once
        if reasons and not stopper:
            stopper.append(threading.Thread(target=graceful, args=(reasons[0],), name="kikolib-stop", daemon=True))
            stopper[0].start()

    def watchdog(interval: float) -> None:
        while not done.wait(interval):
            posix.sd_notify("WATCHDOG=1")

    def rss_monitor(limit: float) -> None:
        while not done.wait(RSS_CHECK_SECONDS):
            rss = posix.rss_bytes()
            if rss is not None and rss > limit:
                logger.warning("RSS %d MiB over limit, restarting gracefully", rss // (1024 * 1024))
                trigger("rss")
                return

    try:
        if threading.current_thread() is threading.main_thread():
            posix.install_signal_handlers(lambda: trigger("signal"))
        posix.sd_notify("READY=1")
        interval = posix.watchdog_interval()
        if interval:
            threading.Thread(target=watchdog, args=(interval,), name="kikolib-watchdog", daemon=True).start()
        if rss_limit_mb:
            threading.Thread(target=rss_monitor, args=(rss_limit_mb * 1024 * 1024,),
                             name="kikolib-rss", daemon=True).start()
        srv.on_poll(poll)
        if ready is not None:
            ready(srv)
        srv.serve_forever()
        srv.drain()
    finally:
        done.set()
        srv.close()
    if reasons and reasons[0] == "rss":
        raise SystemExit(RSS_EXIT_CODE)


def _register() -> None:
    factory.register("server", "threads", probe=lambda: (True, "http.server + threads"),
                     load=lambda: serve, priority=10)


_register()
