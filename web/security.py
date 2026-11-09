# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Passwords, sessions, CSRF and the browser-facing checks (Host, Origin) of the web UI."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from kikolib import server as kserver

COOKIE = "kiko_web_session"
LOGIN_COOKIE = "kiko_web_login"
SESSION_TTL = 24 * 3600
MAX_SESSIONS = 100
DEFAULT_PASSWORD = "admin"  # first start only; a session that used it must change it (see WebUI)
HASH_KEY = "webui_admin_hash"  # same setting as the old web-ui: one admin password for both
PBKDF2_ITERATIONS = 600_000  # new hashes; an older hash carries its own count and still verifies
MIN_PASSWORD = 8
MAX_FAILS, FAIL_WINDOW = 5, 60.0
MAX_FORM = 256 * 1024

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'")
SEC_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "same-origin", "Cache-Control": "no-store"}


def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def is_loopback_host(host: str) -> bool:
    h = host.strip().lower().strip("[]")
    ip = _is_ip(h)
    return h == "localhost" or (ip is not None and ip.is_loopback)


def cookie_value(req: kserver.Request, name: str) -> str:
    for part in req.headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def set_cookie(name: str, value: str, max_age: int) -> str:
    return f"{name}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"


def parse_form(req: kserver.Request) -> dict[str, str]:
    """Body of a POST as {name: first value}; 415 unless it is a url-encoded form."""
    ctype = req.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        raise kserver.HTTPError(415, message="form post expected", headers=SEC_HEADERS)
    try:
        pairs = parse_qs(req.body.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
    except (UnicodeDecodeError, ValueError):
        raise kserver.HTTPError(400, message="bad form", headers=SEC_HEADERS) from None
    return {k: v[0] for k, v in pairs.items()}


# --- Host / Origin ---
def _host_only(value: str) -> str:
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.split(":", 1)[0] if v.count(":") == 1 else v


def _is_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def host_allowed(header: str, bind: str, extra: frozenset[str] = frozenset()) -> bool:
    """DNS-rebinding guard. A loopback bind accepts only loopback names; another bind also its own address,
    names in ``extra`` (KIKO_ALLOWED_HOSTS) and, for a wildcard bind, any IP literal."""
    host = _host_only(header)
    ip = _is_ip(host)
    if host == "localhost" or (ip is not None and ip.is_loopback):
        return True
    bound = bind.lower().strip("[]")
    bound_ip = _is_ip(bound)
    if bound == "localhost" or (bound_ip is not None and bound_ip.is_loopback):
        return False
    return host == bound or host in extra or (ip is not None and bound in ("", "0.0.0.0", "::"))


def origin_ok(headers: kserver.Headers) -> bool:
    """A browser sends Origin on every POST: it must be this very site. No Origin (curl, tests): the CSRF
    token still has to match."""
    if "origin" not in headers:
        return True
    host = headers.get("host", "")
    return bool(host) and headers["origin"].strip().lower() == f"http://{host.strip().lower()}"


# --- sessions ---
@dataclass
class Session:
    token: str
    expires: float
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    must_change: bool = False  # logged in with the default password: only /password and /logout are open
    flashes: list[tuple[str, str]] = field(default_factory=list)  # (kind, message), shown once

    def flash(self, kind: str, message: str) -> None:
        self.flashes.append((kind, message))

    def take_flashes(self) -> list[tuple[str, str]]:
        out, self.flashes = self.flashes, []
        return out


class Auth:
    """Admin password check (hash kept in the DB settings), in-memory sessions, login throttle."""

    def __init__(self, db, ttl: float = SESSION_TTL) -> None:  # db: kiko_lite.db.Database
        self.db, self.ttl = db, ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._fails: list[float] = []
        self._secret = secrets.token_bytes(32)

    def ensure_password(self) -> bool:
        """First start: store the hash of the default password. True when it was stored; an existing hash
        (whatever its password) is never touched."""
        if self.db.settings().get(HASH_KEY):
            return False
        self.db.set_setting(HASH_KEY, hash_password(DEFAULT_PASSWORD))
        return True

    def is_default(self) -> bool:
        return self.verify(DEFAULT_PASSWORD)

    def stored_hash(self) -> str:
        return self.db.settings().get(HASH_KEY, "")

    def verify(self, password: str) -> bool:
        return verify_password(password, self.stored_hash())

    def begin_attempt(self) -> bool:
        """Count a login attempt before the (slow) password check; False when over the limit. A successful
        login clears the count. Check and count happen under one lock, so parallel requests cannot slip through."""
        now = time.monotonic()
        with self._lock:
            self._fails = [t for t in self._fails if now - t < FAIL_WINDOW]
            if len(self._fails) >= MAX_FAILS:
                return False
            self._fails.append(now)
            return True

    def login(self) -> Session:
        now = time.monotonic()
        with self._lock:
            self._fails.clear()
            for t in [t for t, s in self._sessions.items() if s.expires <= now]:
                del self._sessions[t]
            while len(self._sessions) >= MAX_SESSIONS:
                del self._sessions[min(self._sessions, key=lambda t: self._sessions[t].expires)]
            sess = Session(secrets.token_urlsafe(32), now + self.ttl)
            self._sessions[sess.token] = sess
        return sess

    def get(self, req: kserver.Request) -> Session | None:
        token = cookie_value(req, COOKIE)
        with self._lock:
            sess = self._sessions.get(token)
            if sess is not None and sess.expires <= time.monotonic():
                del self._sessions[token]
                return None
        return sess

    def valid(self, token: str) -> bool:
        """True while the session with this token exists and has not expired."""
        with self._lock:
            sess = self._sessions.get(token)
            return sess is not None and sess.expires > time.monotonic()

    def rotate(self, token: str) -> Session | None:
        """New session token and CSRF token for a live session (after a password change)."""
        with self._lock:
            sess = self._sessions.pop(token, None)
            if sess is None:
                return None
            sess.token, sess.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            self._sessions[sess.token] = sess
            return sess

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def end_others(self, keep: str) -> None:
        with self._lock:
            self._sessions = {t: s for t, s in self._sessions.items() if t == keep}

    # login form CSRF (no session yet): double-submit. Cookie holds a nonce, the form its HMAC.
    def login_token(self, nonce: str) -> str:
        return hmac.new(self._secret, nonce.encode(), hashlib.sha256).hexdigest()

    def login_token_ok(self, req: kserver.Request, form_token: str) -> bool:
        nonce = cookie_value(req, LOGIN_COOKIE)
        return bool(nonce) and hmac.compare_digest(self.login_token(nonce), form_token)
