# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Native Windows HTTP backend: WinHTTP (winhttp.dll) through ctypes, factory kind "http", name "winhttp".

Chosen automatically on Windows 10 / 11 (priority 5, before the stdlib backend at 10); everywhere else, and
whenever winhttp.dll cannot be loaded, the stdlib backend stays in charge. KIKO_HTTP_IMPL=stdlib forces it.

Why: WinHTTP uses the Windows proxy settings (system proxy, PAC / WPAD auto-config, proxy authentication by the
OS) and the Windows TLS stack (SChannel + the Windows certificate store), which http.client + ssl cannot.

Proxy: HTTPS_PROXY / HTTP_PROXY set for the request's scheme -> that proxy is used (NAMED_PROXY; NO_PROXY hosts
go direct); not set -> WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY (the Windows settings). KIKO_CA_BUNDLE set -> this
backend is not chosen (WinHTTP cannot take a PEM bundle) and the stdlib backend is used.

Same semantics as StdlibBackend: Response / Headers / error classes of kikolib.http, no redirects followed, no
cookies, no compression, ``timeout`` is a per-operation limit (send, wait for the head, every body read), the
connect phase uses ``connect_timeout``. stream=True reads the body incrementally (QueryDataAvailable + ReadData).
Error messages name only host and port, never headers or the URL query. The ctypes call layer (``_Api``,
``_api``) is the only code touching winhttp.dll; tests patch ``_api``.

Known limits: for a request WITHOUT a body on a new connection the wait for the reply head may run up to
connect_timeout beyond ``timeout`` (WinHTTP does not let send() return before the head arrives); a TLS failure is reported by WinHTTP as one code (12175) without the reason, so it is mapped to
TLSVerifyError (certificate problem is by far the commonest cause); certificate revocation is not checked
(as in the stdlib backend); WinHTTP pools the TCP/TLS connections itself, so no pool settings apply.
"""

from __future__ import annotations

import base64
import io
import os
import re
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from . import __version__, factory
from .http import (CONNECT_TIMEOUT, DEFAULT_TIMEOUT, ConnectError, ConnectionLost, Headers, HttpError,
                   HttpTimeout, Response, TLSVerifyError, proxy_for)

# WinHTTP constants
_ACCESS_NO_PROXY = 1
_ACCESS_NAMED_PROXY = 3
_ACCESS_AUTOMATIC_PROXY = 4
_FLAG_SECURE = 0x00800000
_QUERY_RAW_HEADERS_CRLF = 22
_OPT_DISABLE_FEATURE = 63
_OPT_SECURE_PROTOCOLS = 84
_OPT_PROXY_USERNAME = 0x1002
_OPT_PROXY_PASSWORD = 0x1003
_DISABLE_COOKIES = 0x00000001
_DISABLE_REDIRECTS = 0x00000080
_PROTO_TLS12 = 0x00000800
_PROTO_TLS13 = 0x00002000
_ERR_INSUFFICIENT_BUFFER = 122
_ERR_RESEND_REQUEST = 12032
_CHUNK = 64 * 1024
_WARM_SECONDS = 10.0  # WinHTTP keeps an idle connection about this long
_W11_BUILD = 22000

_TIMEOUT_CODES = {12002, 10060}
_RESOLVE_CODES = {12007}
_CONNECT_CODES = {12029, 10061, 10065, 12178, 12166, 12167, 12180}  # + proxy / auto-config failures
_TLS_VERIFY_CODES = {12175, 12045, 12037, 12038, 12169, 12170, 12179}
_TLS_CODES = {12157, 12044, 12041, 12046}
_LOST_CODES = {12030, 10054, 10053, 12152, 12017}  # connection error / reset / aborted / bad reply / cancelled
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class WinHttpError(Exception):
    """A failed WinHTTP call; ``code`` is the Win32 / WinHTTP error code (never any request data)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"WinHTTP error {code}")
        self.code = code


# --- ctypes call layer (patched in tests) ---------------------------------------------------

class _Api:
    """Thin pythonic wrapper over winhttp.dll; every failure raises WinHttpError(GetLastError)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        d = ctypes.WinDLL("winhttp", use_last_error=True)
        H, W, D, B, P = ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
        d.WinHttpOpen.argtypes, d.WinHttpOpen.restype = [W, D, W, W, D], H
        d.WinHttpConnect.argtypes, d.WinHttpConnect.restype = [H, W, wintypes.WORD, D], H
        d.WinHttpOpenRequest.argtypes, d.WinHttpOpenRequest.restype = [H, W, W, W, W, P, D], H
        d.WinHttpSetTimeouts.argtypes, d.WinHttpSetTimeouts.restype = [H] + [ctypes.c_int] * 4, B
        d.WinHttpSetOption.argtypes, d.WinHttpSetOption.restype = [H, D, P, D], B
        d.WinHttpSendRequest.argtypes, d.WinHttpSendRequest.restype = [H, W, D, P, D, D, ctypes.c_size_t], B
        d.WinHttpWriteData.argtypes, d.WinHttpWriteData.restype = [H, P, D, ctypes.POINTER(D)], B
        d.WinHttpReceiveResponse.argtypes, d.WinHttpReceiveResponse.restype = [H, P], B
        d.WinHttpQueryHeaders.argtypes = [H, D, W, P, ctypes.POINTER(D), ctypes.POINTER(D)]
        d.WinHttpQueryHeaders.restype = B
        d.WinHttpQueryDataAvailable.argtypes = [H, ctypes.POINTER(D)]
        d.WinHttpQueryDataAvailable.restype = B
        d.WinHttpReadData.argtypes, d.WinHttpReadData.restype = [H, P, D, ctypes.POINTER(D)], B
        d.WinHttpCloseHandle.argtypes, d.WinHttpCloseHandle.restype = [H], B
        self._d = d
        self._wt = wintypes

    def _fail(self) -> WinHttpError:
        return WinHttpError(self._ct.get_last_error())

    def open(self, agent: str, access: int, proxy: str | None) -> int:
        h = self._d.WinHttpOpen(agent, access, proxy, None, 0)
        if not h:
            raise self._fail()
        return h

    def connect(self, session: int, host: str, port: int) -> int:
        h = self._d.WinHttpConnect(session, host, port, 0)
        if not h:
            raise self._fail()
        return h

    def open_request(self, conn: int, method: str, target: str, secure: bool) -> int:
        h = self._d.WinHttpOpenRequest(conn, method, target, None, None, None, _FLAG_SECURE if secure else 0)
        if not h:
            raise self._fail()
        return h

    def set_timeouts(self, h: int, resolve: int, connect: int, send: int, receive: int) -> None:
        if not self._d.WinHttpSetTimeouts(h, resolve, connect, send, receive):
            raise self._fail()

    def set_option_dword(self, h: int, option: int, value: int) -> None:
        v = self._wt.DWORD(value)
        if not self._d.WinHttpSetOption(h, option, self._ct.byref(v), 4):
            raise self._fail()

    def set_option_str(self, h: int, option: int, value: str) -> None:
        buf = self._ct.create_unicode_buffer(value)
        if not self._d.WinHttpSetOption(h, option, buf, len(value)):
            raise self._fail()

    def send(self, req: int, headers: str, total: int) -> None:
        """Connect and send the request head; the ``total`` body bytes follow through write()."""
        if not self._d.WinHttpSendRequest(req, headers or None, len(headers) if headers else 0,
                                          None, 0, total, 0):
            raise self._fail()

    def write(self, req: int, data: bytes) -> None:
        got = self._wt.DWORD(0)
        if not self._d.WinHttpWriteData(req, data, len(data), self._ct.byref(got)):
            raise self._fail()

    def receive(self, req: int) -> None:
        if not self._d.WinHttpReceiveResponse(req, None):
            raise self._fail()

    def raw_headers(self, req: int) -> str:
        size = self._wt.DWORD(0)
        ok = self._d.WinHttpQueryHeaders(req, _QUERY_RAW_HEADERS_CRLF, None, None, self._ct.byref(size), None)
        if not ok and self._ct.get_last_error() != _ERR_INSUFFICIENT_BUFFER:
            raise self._fail()
        buf = self._ct.create_string_buffer(size.value + 2)
        if not self._d.WinHttpQueryHeaders(req, _QUERY_RAW_HEADERS_CRLF, None, buf, self._ct.byref(size), None):
            raise self._fail()
        return buf.raw[:size.value].decode("utf-16-le", errors="replace")

    def data_available(self, req: int) -> int:
        n = self._wt.DWORD(0)
        if not self._d.WinHttpQueryDataAvailable(req, self._ct.byref(n)):
            raise self._fail()
        return n.value

    def read(self, req: int, size: int) -> bytes:
        buf = self._ct.create_string_buffer(size)
        got = self._wt.DWORD(0)
        if not self._d.WinHttpReadData(req, buf, size, self._ct.byref(got)):
            raise self._fail()
        return buf.raw[:got.value]

    def close(self, h: int) -> None:
        self._d.WinHttpCloseHandle(h)


_api_lock = threading.Lock()
_api_obj: _Api | None = None


def _api() -> _Api:
    """The shared call layer (loads winhttp.dll on first use; raises OSError/AttributeError if unavailable)."""
    global _api_obj
    with _api_lock:
        if _api_obj is None:
            _api_obj = _Api()
        return _api_obj


# --- errors ------------------------------------------------------------------------------------

def _map_code(code: int, host: str, port: int) -> HttpError:
    """HttpError naming only host:port and the numeric code; never request data."""
    where = f"{host}:{port}"
    if code in _TIMEOUT_CODES:
        return HttpTimeout(f"timed out talking to {where}")
    if code in _RESOLVE_CODES:
        return ConnectError(f"cannot resolve {where}")
    if code in _CONNECT_CODES:
        return ConnectError(f"cannot connect to {where} (WinHTTP {code})")
    if code in _TLS_VERIFY_CODES:
        return TLSVerifyError(f"certificate verification failed for {where} (WinHTTP {code})")
    if code in _TLS_CODES:
        return ConnectError(f"TLS error with {where} (WinHTTP {code})")
    if code in _LOST_CODES:
        return ConnectionLost(f"connection to {where} lost (WinHTTP {code})")
    return HttpError(f"WinHTTP error {code} talking to {where}")


class _Handles:
    """The connect + request handles of one exchange; close() is idempotent and thread-safe."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.conn: int | None = None
        self.req: int | None = None
        self._lock = threading.Lock()
        self.closed = False
        self.timed_out = False

    def guard(self, seconds: float, fn: Any, *args: Any) -> Any:
        """Run one blocking call with our own deadline.

        WinHTTP's own timeouts are only checked on a coarse (about 4 s) timer, far too slow for the per-operation
        limits kiko needs (a 0.3 s limit fired after 4 s or never). So a timer closes the request handle at the
        deadline, which makes the blocked call fail; that failure is then reported as a timeout (12002).
        """
        timer = threading.Timer(seconds, self._expire)
        timer.daemon = True
        timer.start()
        try:
            return fn(*args)
        except WinHttpError:
            if self.timed_out:
                raise WinHttpError(12002) from None
            raise
        finally:
            timer.cancel()

    def _expire(self) -> None:
        self.timed_out = True
        self.abort()

    def abort(self) -> None:
        """Close the request handle: wakes a thread blocked in a call on it (it then fails)."""
        with self._lock:
            req, self.req = self.req, None
            self.closed = True
        if req is not None:
            self.api.close(req)

    def close(self) -> None:
        with self._lock:
            req, conn = self.req, self.conn
            self.req = self.conn = None
            self.closed = True
        for h in (req, conn):
            if h is not None:
                try:
                    self.api.close(h)
                except Exception:
                    pass


class _Abortable:
    """Stands in for the socket Response.abort() shuts down."""

    def __init__(self, handles: _Handles) -> None:
        self._h = handles

    def shutdown(self, how: int) -> None:
        self._h.abort()


class _Body:
    """File-like body (read / readline / close) over WinHttpQueryDataAvailable + WinHttpReadData."""

    def __init__(self, handles: _Handles, host: str, port: int, timeout: float) -> None:
        self._h = handles
        self._timeout = timeout
        self._where = (host, port)
        self._buf = bytearray()
        self._eof = False

    def _fill(self) -> bool:
        """Append the next chunk; False at the end of the body."""
        if self._eof:
            return False
        req = self._h.req
        if req is None:
            raise ConnectionLost(f"connection to {self._where[0]}:{self._where[1]} closed")
        try:
            data = self._h.guard(self._timeout, self._next_chunk, req)
        except WinHttpError as exc:
            code = exc.code
            if code in _TIMEOUT_CODES:
                raise HttpTimeout("read timed out") from None
            raise _map_code(code, *self._where) from None
        if not data:
            self._eof = True
            return False
        self._buf += data
        return True

    def _next_chunk(self, req: int) -> bytes:
        avail = self._h.api.data_available(req)
        return self._h.api.read(req, min(avail, _CHUNK)) if avail else b""

    def read(self) -> bytes:
        while self._fill():
            pass
        out = bytes(self._buf)
        self._buf.clear()
        return out

    def readline(self, limit: int = -1) -> bytes:
        start = 0
        while True:
            i = self._buf.find(b"\n", start)
            if i >= 0 and (limit < 0 or i < limit):
                n = i + 1
                break
            if limit >= 0 and len(self._buf) >= limit:
                n = limit
                break
            start = len(self._buf)
            if not self._fill():
                n = len(self._buf)
                break
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self) -> None:
        self._h.close()


# --- backend -------------------------------------------------------------------------------

def _ms(seconds: float) -> int:
    return max(1, min(int(seconds * 1000), 0x7FFFFFFF))


class WinHttpBackend:
    """WinHTTP backend; one WinHttpOpen session per proxy mode, shared between threads."""

    def __init__(self, connect_timeout: float = CONNECT_TIMEOUT, user_agent: str | None = None) -> None:
        self.connect_timeout = connect_timeout
        self.user_agent = user_agent or f"kiko-lite/{__version__}"
        self._lock = threading.Lock()
        self._sessions: dict[tuple[int, str | None], int] = {}
        self._warm: dict[tuple[str, int], float] = {}  # (host, port) -> monotonic time of the last reply

    def __repr__(self) -> str:
        with self._lock:
            return f"<WinHttpBackend sessions={len(self._sessions)}>"

    @staticmethod
    def _proxy_mode(scheme: str, host: str) -> tuple[tuple[int, str | None], Any]:
        """((access type, named proxy "host:port"), Proxy or None) for this request."""
        if urllib.request.getproxies_environment().get(scheme):
            p = proxy_for(scheme, host)
            if p is None:  # NO_PROXY match: direct
                return (_ACCESS_NO_PROXY, None), None
            return (_ACCESS_NAMED_PROXY, f"{p.host}:{p.port}"), p
        return (_ACCESS_AUTOMATIC_PROXY, None), None

    def _session(self, api: Any, mode: tuple[int, str | None]) -> int:
        with self._lock:
            h = self._sessions.get(mode)
            if h is None:
                h = api.open(self.user_agent, mode[0], mode[1])
                for protos in (_PROTO_TLS12 | _PROTO_TLS13, _PROTO_TLS12):  # no TLS 1.0 / 1.1 / SSL
                    try:
                        api.set_option_dword(h, _OPT_SECURE_PROTOCOLS, protos)
                        break
                    except WinHttpError:
                        continue
                self._sessions[mode] = h
            return h

    def request(self, method: str, url: str, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, timeout: float = DEFAULT_TIMEOUT,
                stream: bool = False) -> Response:
        """One request; ``timeout`` has the same meaning as in StdlibBackend.request."""
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
        if (not _TOKEN.match(method) or re.search(r"[\x00-\x20\x7f]", target)
                or any(not _TOKEN.match(str(k)) or re.search(r"[\r\n\x00]", str(v)) for k, v in hdrs.items())):
            raise HttpError("invalid request header or URL")
        head = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        mode, proxy = self._proxy_mode(scheme, host)
        try:
            api = _api()
        except (OSError, AttributeError):
            raise HttpError("WinHTTP is not available") from None
        for attempt in (0, 1):
            handles = _Handles(api)
            err: HttpError | None = None
            resend = False
            try:
                resp = self._exchange(api, handles, mode, proxy, method, scheme, host, port, target, head,
                                      body, timeout, stream)
            except WinHttpError as exc:
                handles.close()
                if exc.code == _ERR_RESEND_REQUEST and attempt == 0:
                    resend = True
                else:
                    err = _map_code(exc.code, host, port)
            except BaseException:
                handles.close()
                raise
            if resend:
                continue
            if err is not None:
                raise err  # raised outside the except block: no __context__
            return resp
        raise HttpError("unreachable")  # pragma: no cover

    def _exchange(self, api: Any, handles: _Handles, mode: tuple[int, str | None], proxy: Any, method: str,
                  scheme: str, host: str, port: int, target: str, head: str, body: bytes | None,
                  timeout: float, stream: bool) -> Response:
        session = self._session(api, mode)
        handles.conn = api.connect(session, host, port)
        req = handles.req = api.open_request(handles.conn, method, target, scheme == "https")
        api.set_option_dword(req, _OPT_DISABLE_FEATURE, _DISABLE_REDIRECTS | _DISABLE_COOKIES)
        api.set_timeouts(req, _ms(self.connect_timeout), _ms(self.connect_timeout), _ms(timeout), _ms(timeout))
        if proxy is not None and proxy.auth:
            user, _, pw = base64.b64decode(proxy.auth.split(" ", 1)[1]).decode("utf-8").partition(":")
            api.set_option_str(req, _OPT_PROXY_USERNAME, user)
            api.set_option_str(req, _OPT_PROXY_PASSWORD, pw)
        # send() connects (TLS, proxy auto-detection) and sends the head. With a body it returns then (the body goes
        # through write(), so the wait for the reply is only in receive()). WITHOUT a body WinHTTP keeps send()
        # blocked until the reply head arrives, so a short limit must apply to send() itself; it can only include
        # the connect budget when the connection is probably new (nothing answered from this host:port lately).
        with self._lock:
            warm = time.monotonic() - self._warm.get((host, port), -1e9) < _WARM_SECONDS
        budget = timeout if (warm and not body) else self.connect_timeout + timeout
        handles.guard(budget, api.send, req, head, len(body) if body else 0)
        for i in range(0, len(body or b""), _CHUNK):
            handles.guard(timeout, api.write, req, body[i:i + _CHUNK])
        handles.guard(timeout, api.receive, req)
        with self._lock:
            self._warm[(host, port)] = time.monotonic()
        status, reason, pairs = _parse_head(api.raw_headers(req))
        src = _Body(handles, host, port, timeout)
        if stream:
            return Response(status, reason, Headers(pairs), src, lambda reuse: handles.close(),
                            sock=_Abortable(handles))
        try:
            data = src.read()
        finally:
            handles.close()
        return Response(status, reason, Headers(pairs), io.BytesIO(data))

    def close(self) -> None:
        """Close the sessions (in-flight responses close their own handles)."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        if not sessions:
            return
        try:
            api = _api()
        except (OSError, AttributeError):
            return
        for h in sessions:
            try:
                api.close(h)
            except Exception:
                pass


def _parse_head(raw: str) -> tuple[int, str, list[tuple[str, str]]]:
    """(status, reason, header pairs) from WinHTTP's RAW_HEADERS_CRLF text."""
    lines = raw.replace("\0", "").split("\r\n")
    parts = lines[0].split(" ", 2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        raise HttpError("malformed response status line") from None
    reason = parts[2] if len(parts) > 2 else ""
    pairs = []
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep and k.strip():
            pairs.append((k.strip(), v.strip()))
    return status, reason, pairs


# --- probe and registration ----------------------------------------------------------------

def _probe() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "not Windows"
    if os.environ.get("KIKO_CA_BUNDLE"):
        return False, "KIKO_CA_BUNDLE set: WinHTTP cannot use a PEM bundle, stdlib used"
    v = sys.getwindowsversion()
    if v.major < 10:
        return False, f"Windows {v.major}.{v.minor} (need 10 or 11)"
    try:
        _api()
    except (OSError, AttributeError):
        return False, "winhttp.dll cannot be loaded"
    tag = "w11" if v.build >= _W11_BUILD else "w10"
    return True, f"WinHTTP ({tag}, build {v.build}): Windows proxy settings + SChannel"


def register() -> None:
    """Register with the factory (done at import; call again after factory.clear())."""
    factory.register("http", "winhttp", probe=_probe, load=WinHttpBackend, priority=5)


register()
