#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""kiko-lite web-ui: DB management interface on kikolib.server + vanilla JS."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
from pathlib import Path

# Add kiko_lite and kikolib to path
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "kikolib"))

from kikolib import server as kserver
from kikolib import config as kconfig
from kiko_lite.db import Database, default_path
from kiko_lite.resolver import build_db_app

# Load config from ~/.kiko/kiko.toml
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("kiko.webui")

WEBUI_DIR = Path(__file__).parent
TEMPLATES_DIR = WEBUI_DIR / "templates"
STATIC_DIR = WEBUI_DIR / "static"

DB_PATH = kconfig.get_database_path()
DB = Database(DB_PATH)

# Web UI port from config
WEBUI_HOST = kconfig.get_webui_host()
WEBUI_PORT = kconfig.get_webui_port()

# ---- Auth System (Tomcat-style) ----------------------------------------------------

SESSIONS: dict[str, dict] = {}  # token -> {"user": "admin", "created": time}
SESSION_TTL = 24 * 3600  # 24 hours

# Password hash storage (in DB settings)
PASSWORD_KEY = "webui_admin_password_hash"
SALT_KEY = "webui_admin_salt"

def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()

def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    return hash_password(password, salt) == stored_hash

def get_or_create_credentials():
    """Get or create default admin/admin credentials."""
    settings = DB.settings()
    salt = settings.get(SALT_KEY)
    stored_hash = settings.get(PASSWORD_KEY)

    if not salt or not stored_hash:
        # Create default admin/admin
        salt = secrets.token_hex(16)
        default_hash = hash_password("admin", salt)
        DB.set_setting(SALT_KEY, salt)
        DB.set_setting(PASSWORD_KEY, default_hash)
        return salt, default_hash, True  # True = default password
    return salt, stored_hash, False

def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user": username, "created": __import__("time").time()}
    return token

def validate_session(token: str) -> str | None:
    if not token or token not in SESSIONS:
        return None
    session = SESSIONS[token]
    if __import__("time").time() - session["created"] > SESSION_TTL:
        del SESSIONS[token]
        return None
    return session["user"]

def require_auth(req: kserver.Request) -> str | None:
    """Check session cookie or Authorization header. Return username or None."""
    # Check cookie
    cookie = req.headers.get("cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session="):
            token = part[8:]
            user = validate_session(token)
            if user:
                return user
    # Check Authorization header (Bearer token)
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        user = validate_session(token)
        if user:
            return user
    return None

SALT, STORED_HASH, IS_DEFAULT = get_or_create_credentials()


# ---- Auth Routes -------------------------------------------------------------------

def api_login(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    username = data.get("username", "")
    password = data.get("password", "")

    if username != "admin":
        return kserver.JsonResponse({"error": "invalid credentials"}, status=401)
    if not verify_password(password, SALT, STORED_HASH):
        return kserver.JsonResponse({"error": "invalid credentials"}, status=401)

    token = create_session(username)
    resp = kserver.JsonResponse({"ok": True, "token": token, "default_password": IS_DEFAULT})
    resp.headers["Set-Cookie"] = f"session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL}"
    return resp


def api_logout(req: kserver.Request) -> kserver.JsonResponse:
    cookie = req.headers.get("cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session="):
            token = part[8:]
            SESSIONS.pop(token, None)
            break
    resp = kserver.JsonResponse({"ok": True})
    resp.headers["Set-Cookie"] = "session=; HttpOnly; Path=/; Max-Age=0"
    return resp


def api_change_password(req: kserver.Request) -> kserver.JsonResponse:
    global STORED_HASH
    user = require_auth(req)
    if not user:
        return kserver.JsonResponse({"error": "unauthorized"}, status=401)

    data = req.json()
    current = data.get("current_password", "")
    new_pass = data.get("new_password", "")

    if not verify_password(current, SALT, STORED_HASH):
        return kserver.JsonResponse({"error": "current password incorrect"}, status=400)
    if len(new_pass) < 6:
        return kserver.JsonResponse({"error": "password must be at least 6 characters"}, status=400)

    STORED_HASH = hash_password(new_pass, SALT)
    DB.set_setting(PASSWORD_KEY, STORED_HASH)

    return kserver.JsonResponse({"ok": True, "message": "password changed"})


def api_auth_status(req: kserver.Request) -> kserver.JsonResponse:
    user = require_auth(req)
    if user:
        return kserver.JsonResponse({"authenticated": True, "user": user, "default_password": IS_DEFAULT})
    return kserver.JsonResponse({"authenticated": False})


# ---- Auth Decorator -----------------------------------------------------------------

def protected(fn):
    """Decorator to require auth on API routes."""
    def wrapper(req: kserver.Request):
        user = require_auth(req)
        if not user:
            return kserver.JsonResponse({"error": "unauthorized"}, status=401)
        return fn(req)
    return wrapper


# ---- API Routes --------------------------------------------------------------------

@protected
def api_models(req: kserver.Request) -> kserver.JsonResponse:
    models = DB.models()
    return kserver.JsonResponse({"models": [dict(m) for m in models]})


@protected
def api_model_get(req: kserver.Request) -> kserver.JsonResponse:
    alias = req.path.split("/")[-1]
    row = DB.get_model(alias)
    if row is None:
        return kserver.JsonResponse({"error": "not found"}, status=404)
    return kserver.JsonResponse(dict(row))


@protected
def api_model_post(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    alias = data.pop("alias", None)
    if not alias:
        return kserver.JsonResponse({"error": "alias required"}, status=400)
    try:
        new = DB.upsert_model(
            alias,
            data.get("provider", ""),
            data.get("api_key_env", ""),
            data.get("api_base") or None,
            data.get("model_class", "SONNET"),
            data.get("priority", 100),
            data.get("price_in_per_mtok"),
            data.get("price_out_per_mtok"),
            data.get("price_cache_read_per_mtok"),
            data.get("enabled", True),
            data.get("status", "unknown"),
        )
    except ValueError as e:
        return kserver.JsonResponse({"error": str(e)}, status=400)
    return kserver.JsonResponse({"alias": alias, "created": new})


@protected
def api_model_delete(req: kserver.Request) -> kserver.JsonResponse:
    alias = req.path.split("/")[-1]
    if not DB.delete_model(alias):
        return kserver.JsonResponse({"error": "not found"}, status=404)
    return kserver.JsonResponse({"deleted": alias})


@protected
def api_classes(req: kserver.Request) -> kserver.JsonResponse:
    classes = DB.classes()
    return kserver.JsonResponse({"classes": classes})


@protected
def api_class_post(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    name = data.get("name", "").upper()
    keyword = data.get("keyword", "").lower()
    role = data.get("role", "")
    if not name or not keyword:
        return kserver.JsonResponse({"error": "name and keyword required"}, status=400)
    try:
        DB.add_class(name, keyword, role)
    except ValueError as e:
        return kserver.JsonResponse({"error": str(e)}, status=400)
    return kserver.JsonResponse({"name": name, "keyword": keyword})


@protected
def api_class_delete(req: kserver.Request) -> kserver.JsonResponse:
    name = req.path.split("/")[-1].upper()
    if not DB.delete_class(name):
        return kserver.JsonResponse({"error": "not found"}, status=404)
    return kserver.JsonResponse({"deleted": name})


@protected
def api_billing(req: kserver.Request) -> kserver.JsonResponse:
    days = int(req.query.get("days", "30"))
    rows = DB.billing_summary(days)
    return kserver.JsonResponse({"days": days, "rows": rows})


@protected
def api_logs(req: kserver.Request) -> kserver.JsonResponse:
    """Get paginated logs from billing table, newest first."""
    limit = int(req.query.get("limit", "30"))
    offset = int(req.query.get("offset", "0"))
    order = req.query.get("order", "desc")  # desc = newest first

    # Get raw billing data with all fields
    with DB._read() as con:
        # Build query with all billing fields
        order_sql = "DESC" if order == "desc" else "ASC"
        rows = con.execute(
            f"SELECT id, model, tokens_in, tokens_out, cost_usd, ts, route, "
            f"attempts, latency_ms, upstream_model, error "
            f"FROM billing ORDER BY ts {order_sql} LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

    logs = []
    for r in rows:
        logs.append({
            "id": r["id"],
            "model": r["model"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "cost_usd": r["cost_usd"],
            "ts": r["ts"],
            "route": r["route"],
            "attempts": r["attempts"],
            "latency_ms": r["latency_ms"],
            "upstream_model": r["upstream_model"],
            "error": r["error"]
        })

    return kserver.JsonResponse({"logs": logs})


@protected
def api_logs_count(req: kserver.Request) -> kserver.JsonResponse:
    """Get total count of log entries."""
    with DB._read() as con:
        count = con.execute("SELECT COUNT(*) FROM billing").fetchone()[0]
    return kserver.JsonResponse({"count": count})


@protected
def api_responses(req: kserver.Request) -> kserver.JsonResponse:
    """Get paginated LLM request/response logs from responses table, newest first."""
    limit = int(req.query.get("limit", "30"))
    offset = int(req.query.get("offset", "0"))
    order = req.query.get("order", "desc")  # desc = newest first

    order_sql = "DESC" if order == "desc" else "ASC"
    with DB._read() as con:
        rows = con.execute(
            f"SELECT id, input_json, output_json, created_at "
            f"FROM responses ORDER BY created_at {order_sql} LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

    responses = []
    for r in rows:
        responses.append({
            "id": r["id"],
            "input_json": r["input_json"],
            "output_json": r["output_json"],
            "created_at": r["created_at"]
        })

    return kserver.JsonResponse({"responses": responses})


@protected
def api_responses_count(req: kserver.Request) -> kserver.JsonResponse:
    """Get total count of response entries."""
    with DB._read() as con:
        count = con.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    return kserver.JsonResponse({"count": count})


@protected
def api_settings(req: kserver.Request) -> kserver.JsonResponse:
    settings = DB.settings()
    return kserver.JsonResponse({"settings": settings})


@protected
def api_setting_post(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    key = data.get("key")
    value = data.get("value")
    if not key:
        return kserver.JsonResponse({"error": "key required"}, status=400)
    DB.set_setting(key, str(value))
    return kserver.JsonResponse({"key": key, "value": str(value)})


@protected
def api_aliases(req: kserver.Request) -> kserver.JsonResponse:
    aliases = DB.aliases()
    return kserver.JsonResponse({"aliases": aliases})


@protected
def api_alias_post(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    glob = data.get("glob")
    target = data.get("target")
    if not glob or not target:
        return kserver.JsonResponse({"error": "glob and target required"}, status=400)
    DB.set_alias(glob, target)
    return kserver.JsonResponse({"glob": glob, "target": target})


@protected
def api_alias_delete(req: kserver.Request) -> kserver.JsonResponse:
    glob = req.path.split("/")[-1]
    DB.delete_alias(glob)
    return kserver.JsonResponse({"deleted": glob})


@protected
def api_key_status(req: kserver.Request) -> kserver.JsonResponse:
    """Check where a key name is found (env, dotenv, store) - never returns the value."""
    from kiko_lite.cli import _dotenv_values, _key_source
    from kikolib import keystore

    alias = req.path.split("/")[-1]
    row = DB.get_model(alias)
    if not row:
        return kserver.JsonResponse({"error": "not found"}, status=404)

    key_name = row["api_key_env"]
    env_file = _dotenv_values(DB_PATH)

    source = None
    if os.environ.get(key_name) or os.environ.get(key_name.upper()):
        source = "env"
    elif keystore.get_secret(key_name, env={}):
        source = "store"
    elif env_file.get(key_name):
        source = "dotenv"

    return kserver.JsonResponse({
        "alias": alias,
        "key_name": key_name,
        "source": source or "NOT_FOUND"
    })


# ---- Static/Template Routes --------------------------------------------------------

def serve_login(req: kserver.Request) -> kserver.StreamResponse:
    """Serve login page."""
    html = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
    return kserver.StreamResponse([html.encode("utf-8")], status=200, headers={"Content-Type": "text/html"}, content_type="text/html")


def serve_frontend(req: kserver.Request) -> kserver.StreamResponse:
    """Serve static files from /static/ or index.html for SPA routes."""
    path = req.path

    # Serve static files
    if path.startswith("/static/"):
        file_path = STATIC_DIR / path[len("/static/"):]
        if file_path.exists() and not file_path.is_dir():
            mime = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".html": "text/html",
                ".png": "image/png",
                ".ico": "image/x-icon",
            }.get(file_path.suffix, "application/octet-stream")
            return kserver.StreamResponse([file_path.read_bytes()], status=200, headers={"Content-Type": mime})
        return kserver.StreamResponse([b"Not found"], status=404, headers={"Content-Type": "text/plain"})

    # Model form page (public - auth checked inside)
    if path == "/models/new" or path.startswith("/models/edit/"):
        user = require_auth(req)
        if not user:
            return kserver.StreamResponse([b""], status=302, headers={"Location": "/login"})
        html = (TEMPLATES_DIR / "model_form.html").read_text(encoding="utf-8")
        return kserver.StreamResponse([html.encode("utf-8")], status=200, headers={"Content-Type": "text/html"}, content_type="text/html")

    # Class form page (public - auth checked inside)
    if path == "/classes/new" or path.startswith("/classes/edit/"):
        user = require_auth(req)
        if not user:
            return kserver.StreamResponse([b""], status=302, headers={"Location": "/login"})
        html = (TEMPLATES_DIR / "class_form.html").read_text(encoding="utf-8")
        return kserver.StreamResponse([html.encode("utf-8")], status=200, headers={"Content-Type": "text/html"}, content_type="text/html")

    # Alias form page (public - auth checked inside)
    if path == "/aliases/new" or path.startswith("/aliases/edit/"):
        user = require_auth(req)
        if not user:
            return kserver.StreamResponse([b""], status=302, headers={"Location": "/login"})
        html = (TEMPLATES_DIR / "alias_form.html").read_text(encoding="utf-8")
        return kserver.StreamResponse([html.encode("utf-8")], status=200, headers={"Content-Type": "text/html"}, content_type="text/html")

    # Check auth for SPA routes
    user = require_auth(req)
    if not user:
        # Redirect to login page
        return kserver.StreamResponse([b""], status=302, headers={"Location": "/login"})

    # SPA fallback - serve index.html for all other routes
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return kserver.StreamResponse([html.encode("utf-8")], status=200, headers={"Content-Type": "text/html"}, content_type="text/html")


# ---- Build App ---------------------------------------------------------------------

def build_web_app() -> kserver.App:
    app = kserver.App()

    # API routes
    app.route("GET", "/api/models")(api_models)
    app.route("GET", "/api/models/{alias}")(api_model_get)
    app.route("POST", "/api/models")(api_model_post)
    app.route("PUT", "/api/models/{alias}")(api_model_post)
    app.route("DELETE", "/api/models/{alias}")(api_model_delete)

    app.route("GET", "/api/classes")(api_classes)
    app.route("POST", "/api/classes")(api_class_post)
    app.route("PUT", "/api/classes/{name}")(api_class_post)
    app.route("DELETE", "/api/classes/{name}")(api_class_delete)

    app.route("GET", "/api/billing")(api_billing)

    # Logs API routes (with pagination)
    app.route("GET", "/api/logs")(api_logs)
    app.route("GET", "/api/logs/count")(api_logs_count)

    # Responses (Text Logs) API routes
    app.route("GET", "/api/responses")(api_responses)
    app.route("GET", "/api/responses/count")(api_responses_count)

    app.route("GET", "/api/settings")(api_settings)
    app.route("POST", "/api/settings")(api_setting_post)

    app.route("GET", "/api/aliases")(api_aliases)
    app.route("POST", "/api/aliases")(api_alias_post)
    app.route("PUT", "/api/aliases/{glob}")(api_alias_post)
    app.route("DELETE", "/api/aliases/{glob}")(api_alias_delete)

    app.route("GET", "/api/key-status/{alias}")(api_key_status)

    # Auth routes (public)
    app.route("POST", "/api/auth/login")(api_login)
    app.route("POST", "/api/auth/login/")(api_login)
    app.route("POST", "/api/auth/logout")(api_logout)
    app.route("POST", "/api/auth/logout/")(api_logout)
    app.route("POST", "/api/auth/change-password")(api_change_password)
    app.route("POST", "/api/auth/change-password/")(api_change_password)
    app.route("GET", "/api/auth/status")(api_auth_status)
    app.route("GET", "/api/auth/status/")(api_auth_status)

    # Login page (public)
    app.route("GET", "/login")(serve_login)

    # Frontend - explicit routes (kikolib.server doesn't support /* catch-all)
    app.route("GET", "/")(serve_frontend)
    app.route("GET", "/static/{path:path}")(serve_frontend)
    app.route("GET", "/{path:path}")(serve_frontend)  # SPA fallback

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=WEBUI_HOST)
    parser.add_argument("--port", type=int, default=WEBUI_PORT)
    args = parser.parse_args()

    app = build_web_app()
    print(f"kiko web-ui on http://{args.host}:{args.port}  db={DB_PATH}")
    kserver.serve(app, args.host, args.port)