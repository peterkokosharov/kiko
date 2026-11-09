#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko-lite web-ui: browser management of the kiko.db (models, classes, aliases, settings, billing, logs).

Standard library only, on ``kikolib.server`` plus a vanilla-JS front end (``static/``, ``templates/``).

Security model
  * binds 127.0.0.1 by default; a non-loopback bind needs ``--host`` and ``KIKO_ALLOWED_HOSTS`` for the names used
  * one admin user; the password is stored as a salted PBKDF2-HMAC-SHA256 hash in the DB settings table; the
    first start generates a random password and writes it ONCE to a private file (never stdout; the file is
    removed after the first password change; there is no default password)
  * sessions are random ``secrets`` tokens kept in memory, sent as an HttpOnly, SameSite=Strict cookie
  * ``App.browser_guard`` is on (Host allow-list against DNS rebinding, JSON-only POST) with
    ``allow_same_origin``: only an Origin equal to ``http://<Host>`` passes, any other origin is a 403
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kikolib import config as kconfig  # noqa: E402
from kikolib import logs as klogs  # noqa: E402
from kikolib import server as kserver  # noqa: E402
from kiko_lite.db import Database, default_path  # noqa: E402
from kiko_lite.keys import dotenv_values, key_source  # noqa: E402
from kikolib import posix as kposix  # noqa: E402

logger = logging.getLogger("kiko.webui")

WEBUI_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEBUI_DIR / "templates"
STATIC_DIR = WEBUI_DIR / "static"

COOKIE = "kiko_session"
SESSION_TTL = 24 * 3600
MAX_SESSIONS = 100
HASH_KEY = "webui_admin_hash"  # "pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>"
HIDDEN_SETTINGS = frozenset({HASH_KEY})
# The first-start password of the new web (web/security.py DEFAULT_PASSWORD; a test keeps the two equal). The web-ui
# has no forced-change page, so it never serves this password: no login with it, no non-loopback start while stored.
DEFAULT_PASSWORD = "admin"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
PBKDF2_ITERATIONS = 600_000  # new hashes; older hashes carry their own count and still verify
MIN_PASSWORD = 8
MAX_FAILS = 5  # failed logins per FAIL_WINDOW seconds before a 429
FAIL_WINDOW = 60.0

_MIME = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".html": "text/html; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon",
         ".svg": "image/svg+xml", ".json": "application/json", ".map": "application/json"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
_SEC_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store", "Content-Security-Policy": CSP}


def initial_password_path() -> Path:
    """Where the first-start password is kept (``kikolib.config.password_file``, in the config folder)."""
    return kconfig.password_file("webui")


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


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> kserver.JsonResponse:
    return kserver.JsonResponse(data, status, {**_SEC_HEADERS, **(headers or {})})


def _err(message: str, status: int) -> kserver.JsonResponse:
    return _json({"error": message}, status)


def _int(req: kserver.Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(req.query.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _num(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


def _html(name: str) -> kserver.StreamResponse:
    body = (TEMPLATES_DIR / name).read_bytes()
    return kserver.StreamResponse([body], 200, _SEC_HEADERS, "text/html; charset=utf-8")


def _redirect(to: str) -> kserver.StreamResponse:
    return kserver.StreamResponse([b""], 302, {**_SEC_HEADERS, "Location": to}, "text/plain")


class WebUI:
    """The web-ui state (DB, password hash, sessions) and its request handlers."""

    def __init__(self, db: Database, session_ttl: float = SESSION_TTL, password_file: Path | None = None) -> None:
        self.db = db
        self.password_file = password_file
        self.session_ttl = session_ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}  # token -> expiry (monotonic)
        self._fails: list[float] = []
        self.initial_password: str | None = None  # set only when this start created the credentials
        if not db.settings().get(HASH_KEY):
            self.initial_password = secrets.token_urlsafe(12)
            db.set_setting(HASH_KEY, hash_password(self.initial_password))
            if password_file is not None:  # private file, never stdout
                password_file.parent.mkdir(parents=True, exist_ok=True)
                kposix.write_private(password_file, (self.initial_password + "\n").encode("utf-8"))

    # --- sessions ---
    def _create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            for t in [t for t, exp in self._sessions.items() if exp <= now]:
                del self._sessions[t]
            while len(self._sessions) >= MAX_SESSIONS:  # drop the oldest
                del self._sessions[min(self._sessions, key=self._sessions.__getitem__)]
            self._sessions[token] = now + self.session_ttl
        return token

    @staticmethod
    def _cookie_token(req: kserver.Request) -> str:
        for part in req.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def authenticated(self, req: kserver.Request) -> bool:
        token = self._cookie_token(req)
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if exp <= time.monotonic():
                del self._sessions[token]
                return False
        return True

    def _cookie(self, value: str, max_age: int) -> str:
        return f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"

    def protected(self, fn: Callable[[kserver.Request], Any]) -> Callable[[kserver.Request], Any]:
        def wrapper(req: kserver.Request) -> Any:
            if not self.authenticated(req):
                return _err("unauthorized", 401)
            return fn(req)
        return wrapper

    # --- auth routes ---
    def login(self, req: kserver.Request) -> kserver.JsonResponse:
        data = req.json()
        if not isinstance(data, dict):
            return _err("JSON object expected", 400)
        now = time.monotonic()
        with self._lock:
            self._fails = [t for t in self._fails if now - t < FAIL_WINDOW]
            if len(self._fails) >= MAX_FAILS:
                return _json({"error": "too many failed logins, wait a minute"}, 429, {"Retry-After": "60"})
        stored = self.db.settings().get(HASH_KEY, "")
        user_ok = hmac.compare_digest(str(data.get("username", "")).encode(), b"admin")
        pass_ok = verify_password(str(data.get("password", "")), stored)  # always run: constant work
        if not (user_ok and pass_ok):
            with self._lock:
                self._fails.append(now)
            return _err("invalid credentials", 401)
        if str(data.get("password", "")) == DEFAULT_PASSWORD:  # pass_ok: the stored password is the default
            return _err("the default password cannot be used here: change it in the new web UI "
                        "(python -m web) first", 403)
        with self._lock:
            self._fails.clear()
        token = self._create_session()
        return _json({"ok": True, "default_password": False},
                     headers={"Set-Cookie": self._cookie(token, int(self.session_ttl))})

    def is_default_password(self) -> bool:
        return verify_password(DEFAULT_PASSWORD, self.db.settings().get(HASH_KEY, ""))

    def logout(self, req: kserver.Request) -> kserver.JsonResponse:
        with self._lock:
            self._sessions.pop(self._cookie_token(req), None)
        return _json({"ok": True}, headers={"Set-Cookie": self._cookie("", 0)})

    def change_password(self, req: kserver.Request) -> kserver.JsonResponse:
        data = req.json()
        if not isinstance(data, dict):
            return _err("JSON object expected", 400)
        if not verify_password(str(data.get("current_password", "")), self.db.settings().get(HASH_KEY, "")):
            return _err("current password incorrect", 400)
        new = data.get("new_password", "")
        if not isinstance(new, str) or len(new) < MIN_PASSWORD:
            return _err(f"password must be at least {MIN_PASSWORD} characters", 400)
        self.db.set_setting(HASH_KEY, hash_password(new))
        if self.password_file is not None:  # the initial password is obsolete now
            try:
                self.password_file.unlink()
            except OSError:
                pass
        keep = self._cookie_token(req)
        with self._lock:  # every other session ends
            self._sessions = {t: e for t, e in self._sessions.items() if t == keep}
        return _json({"ok": True, "message": "password changed"})

    def auth_status(self, req: kserver.Request) -> kserver.JsonResponse:
        if self.authenticated(req):
            return _json({"authenticated": True, "user": "admin", "default_password": False})
        return _json({"authenticated": False})

    # --- models ---
    def models(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"models": self.db.models()})

    def model_get(self, req: kserver.Request) -> kserver.JsonResponse:
        row = self.db.get_model(req.params["alias"])
        return _json(row) if row else _err("not found", 404)

    def model_post(self, req: kserver.Request) -> kserver.JsonResponse:
        data = req.json()
        if not isinstance(data, dict):
            return _err("JSON object expected", 400)
        alias = req.params.get("alias") or data.get("alias")
        if not alias or not isinstance(alias, str):
            return _err("alias required", 400)
        try:
            enabled = data.get("enabled")
            new = self.db.upsert_model(
                alias, str(data.get("provider", "")), str(data.get("api_key_env", "")),
                data.get("api_base") or None, str(data.get("model_class") or "SONNET"),
                int(data.get("priority", 100)), _num(data.get("price_in_per_mtok")),
                _num(data.get("price_out_per_mtok")), _num(data.get("price_cache_read_per_mtok")),
                None if enabled is None else bool(enabled), data.get("status") or None)
        except (ValueError, TypeError) as e:
            return _err(str(e), 400)
        return _json({"alias": alias, "created": new})

    def model_delete(self, req: kserver.Request) -> kserver.JsonResponse:
        alias = req.params["alias"]
        return _json({"deleted": alias}) if self.db.delete_model(alias) else _err("not found", 404)

    def key_status(self, req: kserver.Request) -> kserver.JsonResponse:
        """Where a model's key name is found (env / store / dotenv). Never the value."""
        alias = req.params["alias"]
        row = self.db.get_model(alias)
        if not row:
            return _err("not found", 404)
        name = row["api_key_env"]
        source = key_source(name, dotenv_values(self.db.path))
        return _json({"alias": alias, "key_name": name, "source": source or "NOT_FOUND"})

    # --- classes ---
    def classes(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"classes": self.db.classes()})

    def class_post(self, req: kserver.Request) -> kserver.JsonResponse:
        data = req.json()
        if not isinstance(data, dict):
            return _err("JSON object expected", 400)
        name = str(req.params.get("name") or data.get("name", "")).upper()
        keyword = str(data.get("keyword", "")).lower()
        if not name or not keyword:
            return _err("name and keyword required", 400)
        try:
            self.db.add_class(name, keyword, str(data.get("role", "")))
        except ValueError as e:
            return _err(str(e), 400)
        return _json({"name": name, "keyword": keyword})

    def class_delete(self, req: kserver.Request) -> kserver.JsonResponse:
        name = req.params["name"].upper()
        try:
            ok = self.db.delete_class(name)
        except ValueError as e:
            return _err(str(e), 400)
        return _json({"deleted": name}) if ok else _err("not found", 404)

    # --- billing / logs ---
    def billing(self, req: kserver.Request) -> kserver.JsonResponse:
        days = _int(req, "days", 30, 1, 3660)
        return _json({"days": days, "rows": self.db.billing_summary(days)})

    def _page(self, table: str, cols: str, order_col: str, req: kserver.Request) -> list[dict[str, Any]]:
        limit, offset = _int(req, "limit", 30, 1, 500), _int(req, "offset", 0, 0, 10**9)
        direction = "ASC" if req.query.get("order") == "asc" else "DESC"
        # table, cols, order_col are constants of this module; only the numbers come from the request
        return self.db.query(f"SELECT {cols} FROM {table} ORDER BY {order_col} {direction} LIMIT ? OFFSET ?",
                             (limit, offset))

    def logs(self, req: kserver.Request) -> kserver.JsonResponse:
        cols = "id, model, tokens_in, tokens_out, cost_usd, ts, route, attempts, latency_ms, upstream_model, error"
        return _json({"logs": self._page("billing", cols, "ts", req)})

    def _count(self, table: str) -> int:
        return int(self.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])

    def logs_count(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"count": self._count("billing")})

    def responses(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"responses": self._page("responses", "id, input_json, output_json, created_at",
                                              "created_at", req)})

    def responses_count(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"count": self._count("responses")})

    # --- settings / aliases ---
    def settings(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"settings": {k: v for k, v in self.db.settings().items() if k not in HIDDEN_SETTINGS}})

    def setting_post(self, req: kserver.Request) -> kserver.JsonResponse:
        data = req.json()
        if not isinstance(data, dict):
            return _err("JSON object expected", 400)
        key = data.get("key")
        if not key or not isinstance(key, str):
            return _err("key required", 400)
        if key in HIDDEN_SETTINGS:
            return _err("this setting is managed by the web-ui itself", 403)
        self.db.set_setting(key, str(data.get("value", "")))
        return _json({"key": key, "value": str(data.get("value", ""))})

    def aliases(self, req: kserver.Request) -> kserver.JsonResponse:
        return _json({"aliases": [{"glob": g, "target": t} for g, t in self.db.aliases()]})

    def alias_post(self, req: kserver.Request) -> kserver.JsonResponse:
        data = req.json()
        if not isinstance(data, dict):
            return _err("JSON object expected", 400)
        glob = req.params.get("glob") or data.get("glob")
        target = data.get("target")
        if not glob or not target or not isinstance(glob, str) or not isinstance(target, str):
            return _err("glob and target required", 400)
        self.db.set_alias(glob, target)
        return _json({"glob": glob, "target": target})

    def alias_delete(self, req: kserver.Request) -> kserver.JsonResponse:
        glob = req.params["glob"]
        return _json({"deleted": glob}) if self.db.delete_alias(glob) else _err("not found", 404)

    # --- pages / static files ---
    def login_page(self, req: kserver.Request) -> kserver.StreamResponse:
        return _html("login.html")

    def static_file(self, req: kserver.Request) -> kserver.StreamResponse:
        root = STATIC_DIR.resolve()
        try:
            target = (root / req.params["path"]).resolve()
        except (OSError, ValueError):
            target = None
        if target is None or not target.is_relative_to(root) or not target.is_file():
            return kserver.StreamResponse([b"Not found"], 404, _SEC_HEADERS, "text/plain")
        return kserver.StreamResponse([target.read_bytes()], 200, _SEC_HEADERS,
                                      _MIME.get(target.suffix.lower(), "application/octet-stream"))

    def page(self, req: kserver.Request) -> kserver.StreamResponse:
        """HTML pages (logged-in users only; others go to /login); SPA fallback for every other GET path."""
        if not self.authenticated(req):
            return _redirect("/login")
        path = req.path
        for prefix, template in (("/models/", "model_form.html"), ("/classes/", "class_form.html"),
                                 ("/aliases/", "alias_form.html")):
            if path == prefix + "new" or path.startswith(prefix + "edit/"):
                return _html(template)
        return _html("index.html")

    def build_app(self) -> kserver.App:
        app = kserver.App(max_body=1024 * 1024)
        app.browser_guard = True
        app.allow_same_origin = True
        p = self.protected
        r = app.route
        r("GET", "/api/models")(p(self.models))
        r("GET", "/api/models/{alias}")(p(self.model_get))
        r("POST", "/api/models")(p(self.model_post))
        r("PUT", "/api/models/{alias}")(p(self.model_post))
        r("DELETE", "/api/models/{alias}")(p(self.model_delete))
        r("GET", "/api/classes")(p(self.classes))
        r("POST", "/api/classes")(p(self.class_post))
        r("PUT", "/api/classes/{name}")(p(self.class_post))
        r("DELETE", "/api/classes/{name}")(p(self.class_delete))
        r("GET", "/api/billing")(p(self.billing))
        r("GET", "/api/logs")(p(self.logs))
        r("GET", "/api/logs/count")(p(self.logs_count))
        r("GET", "/api/responses")(p(self.responses))
        r("GET", "/api/responses/count")(p(self.responses_count))
        r("GET", "/api/settings")(p(self.settings))
        r("POST", "/api/settings")(p(self.setting_post))
        r("GET", "/api/aliases")(p(self.aliases))
        r("POST", "/api/aliases")(p(self.alias_post))
        r("PUT", "/api/aliases/{glob}")(p(self.alias_post))
        r("DELETE", "/api/aliases/{glob}")(p(self.alias_delete))
        r("GET", "/api/key-status/{alias}")(p(self.key_status))
        r("POST", "/api/auth/login")(self.login)
        r("POST", "/api/auth/logout")(self.logout)
        r("POST", "/api/auth/change-password")(p(self.change_password))
        r("GET", "/api/auth/status")(self.auth_status)
        r("GET", "/login")(self.login_page)
        r("GET", "/static/{path:path}")(self.static_file)
        r("GET", "/")(self.page)
        r("GET", "/{path:path}")(self.page)  # SPA fallback
        return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="kiko-lite web-ui (loopback by default)")
    ap.add_argument("--host", default=None, help=f"bind address (default {kconfig.web_host()}: [web] host of {kconfig.TOML_NAME})")
    ap.add_argument("--port", type=int, default=None, help=f"port (default {kconfig.web_port()}: [web] port or base_port of {kconfig.TOML_NAME})")
    ap.add_argument("--db", default=None, help=f"database file (default {kconfig.database_path()})")
    args = ap.parse_args(argv)
    klogs.setup_logging()

    host = args.host or kconfig.web_host()
    port = args.port if args.port is not None else kconfig.web_port()
    db_path = default_path(args.db)  # the same resolver as kiko-lite (--db, KIKO_DB_PATH, KIKO_CONFIG_DIR)
    pw_file = initial_password_path()
    ui = WebUI(Database(db_path), password_file=pw_file)
    if ui.initial_password:
        print(f"kiko web-ui: first start. Login: admin; the initial password is in {pw_file} "
              f"(read it once; it is deleted when you change the password in the UI)", flush=True)
    if host not in LOOPBACK_HOSTS and ui.is_default_password():
        print(f"kiko web-ui: refusing to bind {host}: the admin password is still the default. "
              f"Change it in the new web UI (python -m web, on loopback) first.", file=sys.stderr, flush=True)
        return 2
    if host not in LOOPBACK_HOSTS:
        logger.warning("web-ui bound to %s: plain HTTP, reachable from the network", host)
    print(f"kiko web-ui on http://{host}:{port}  db={db_path}", flush=True)
    try:
        kserver.serve(ui.build_app(), host, port)
    except OSError as e:
        print(f"kiko web-ui: cannot listen on {host}:{port} ({e.strerror or e}). The old web-ui shares the [web] "
              "port with 'python -m web'; stop that one or start this with --port.", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
