# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The web UI core: routing helpers (login, CSRF), responses with security headers, login/logout, static files."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kikolib import config as kconfig
from kikolib import server as kserver

from . import render as r
from .security import (COOKIE, DEFAULT_PASSWORD, LOGIN_COOKIE, MAX_FORM, SEC_HEADERS, Auth, Session, host_allowed, origin_ok,
                       parse_form, set_cookie)

logger = logging.getLogger("kiko.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"
_MIME = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

Response = kserver.StreamResponse


def html_response(body: str, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response([body.encode("utf-8")], status, {**SEC_HEADERS, **(headers or {})}, "text/html; charset=utf-8")


def redirect(to: str, headers: dict[str, str] | None = None) -> Response:
    return Response([b""], 303, {**SEC_HEADERS, "Location": to, **(headers or {})}, "text/plain")


def int_arg(source: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(source.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


class WebUI:
    """Holds the DB and the auth state; page modules register their routes with ``get`` and ``post``."""

    def __init__(self, db: Any, bind: str | None = None, allowed_hosts: frozenset[str] = frozenset(),
                 auth: Auth | None = None, password_file: Path | None = None,
                 started: dict[str, Any] | None = None) -> None:
        self.db = db
        self.bind = kconfig.web_host() if bind is None else bind
        # the server settings this process started with (the settings page compares them with the file)
        self.started = kconfig.server_values() if started is None else started
        self.allowed_hosts = allowed_hosts
        self.auth = auth or Auth(db)
        self.password_file = password_file
        self.app = kserver.App(max_body=MAX_FORM)

    # --- checks that run before every handler (inside the handler, not as a kikolib before hook: a hook can only
    # raise a JSON error, and the browser should see an HTML page) ---
    def _refusal(self, req: kserver.Request) -> str:
        if not host_allowed(req.headers.get("host", ""), self.bind, self.allowed_hosts):
            return "Host header not allowed"
        if req.method != "GET" and not origin_ok(req.headers):
            return "cross-origin request refused"
        return ""

    def route(self, method: str, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Like ``App.route``, but every handler first runs the Host/Origin guard (HTML 403 page on refusal)."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            def guarded(req: kserver.Request) -> Any:
                refusal = self._refusal(req)
                return self.error(403, refusal) if refusal else fn(req)
            self.app.route(method, path)(guarded)
            return fn
        return deco

    # --- responses ---
    def render(self, sess: Session | None, title: str, body: Any, active: str = "", script: str = "",
               status: int = 200) -> Response:
        page = r.layout(title, body, active=active, csrf=sess.csrf if sess else "",
                        flashes=sess.take_flashes() if sess else (), script=script)
        return html_response(page, status)

    def error(self, status: int, message: str) -> Response:
        return html_response(r.layout(f"Error {status}", r.tag("p", message), active=""), status)

    # --- route registration ---
    def get(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Logged-in GET page: ``fn(req, sess) -> Response``."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            def handler(req: kserver.Request) -> Any:
                sess = self.auth.get(req)
                if not sess:
                    return redirect("/login")
                return self._forced_password(req, sess) or self._run(fn, req, sess)
            self.route("GET", path)(handler)
            return fn
        return deco

    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Logged-in POST action with CSRF check: ``fn(req, sess, form) -> Response`` (usually a redirect)."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            def handler(req: kserver.Request) -> Any:
                sess = self.auth.get(req)
                if not sess:
                    return redirect("/login")
                forced = self._forced_password(req, sess)
                if forced:
                    return forced
                form = parse_form(req)
                if not secrets.compare_digest(form.get("csrf", "").encode(), sess.csrf.encode()):
                    return self.error(403, "Invalid or missing CSRF token. Go back, reload the page and try again.")
                return self._run(fn, req, sess, form)
            self.route("POST", path)(handler)
            return fn
        return deco

    @staticmethod
    def _forced_password(req: kserver.Request, sess: Session) -> Response | None:
        """A session that logged in with the default password may only change it (or log out)."""
        if sess.must_change and req.path not in ("/password", "/logout"):
            return redirect("/password")
        return None

    def _run(self, fn: Callable[..., Any], req: kserver.Request, *args: Any) -> Any:
        try:
            return fn(req, *args)
        except Exception:
            logger.error("error in %s %s", req.method, req.path, exc_info=True)
            return self.error(500, "Internal error; see the server log.")

    # --- login / logout / static ---
    def _login_page(self, error: str = "", status: int = 200) -> Response:
        nonce = secrets.token_urlsafe(16)
        body = r.join([
            r.tag("p", error, class_="flash error") if error else "",
            r.form("/login", self.auth.login_token(nonce),
                   r.field("Admin password", "password", kind="password", required=True, autofocus=True,
                           autocomplete="current-password"),
                   r.button("Log in")),
        ])
        page = r.layout("Log in", body)
        return html_response(page, status, {"Set-Cookie": set_cookie(LOGIN_COOKIE, nonce, 3600)})

    def _login_get(self, req: kserver.Request) -> Response:
        return redirect("/") if self.auth.get(req) else self._login_page()

    def _login_post(self, req: kserver.Request) -> Response:
        form = parse_form(req)
        if not self.auth.login_token_ok(req, form.get("csrf", "")):
            return self._login_page("The form expired, please try again.", 403)
        if not self.auth.begin_attempt():
            return self._login_page("Too many failed logins, wait a minute.", 429)
        if not self.auth.verify(form.get("password", "")):
            return self._login_page("Wrong password.", 401)
        sess = self.auth.login()
        sess.must_change = form.get("password", "") == DEFAULT_PASSWORD
        return redirect("/", {"Set-Cookie": set_cookie(COOKIE, sess.token, int(self.auth.ttl))})

    def _logout(self, req: kserver.Request, sess: Session, form: dict[str, str]) -> Response:
        self.auth.logout(sess.token)
        return redirect("/login", {"Set-Cookie": set_cookie(COOKIE, "", 0)})

    def _static(self, req: kserver.Request) -> Response:
        root = STATIC_DIR.resolve()
        try:
            target = (root / req.params["path"]).resolve()
        except (OSError, ValueError):
            target = None
        if target is None or not target.is_relative_to(root) or target.suffix not in _MIME or not target.is_file():
            return self.error(404, "Not found")
        return Response([target.read_bytes()], 200, SEC_HEADERS, _MIME[target.suffix])

    def build_app(self) -> kserver.App:
        from . import billing, classes, dashboard, models, providers, settings  # page modules register their routes
        route = self.route
        route("GET", "/login")(self._login_get)
        route("POST", "/login")(self._login_post)
        self.post("/logout")(self._logout)
        route("GET", "/static/{path:path}")(self._static)
        for module in (dashboard, models, providers, classes, billing, settings):
            module.register(self)
        for method in _METHODS:  # anything else: an HTML 404 that carries the security headers too
            for path in ("/", "/{path:path}"):
                route(method, path)(lambda req: self.error(404, "Not found"))
        return self.app
