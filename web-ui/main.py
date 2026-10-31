#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""kiko-lite web-ui: DB management interface on kikolib.server + vanilla JS."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kikolib import server as kserver
from kiko_lite.db import Database, default_path
from kiko_lite.resolver import build_db_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("kiko.webui")

WEBUI_DIR = Path(__file__).parent
TEMPLATES_DIR = WEBUI_DIR / "templates"
STATIC_DIR = WEBUI_DIR / "static"

DB_PATH = default_path()
DB = Database(DB_PATH)


# ---- API Routes --------------------------------------------------------------------

def api_models(req: kserver.Request) -> kserver.JsonResponse:
    models = DB.models()
    return kserver.JsonResponse({"models": [dict(m) for m in models]})


def api_model_get(req: kserver.Request) -> kserver.JsonResponse:
    alias = req.path.split("/")[-1]
    row = DB.get_model(alias)
    if row is None:
        return kserver.JsonResponse({"error": "not found"}, status=404)
    return kserver.JsonResponse(dict(row))


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


def api_model_delete(req: kserver.Request) -> kserver.JsonResponse:
    alias = req.path.split("/")[-1]
    if not DB.delete_model(alias):
        return kserver.JsonResponse({"error": "not found"}, status=404)
    return kserver.JsonResponse({"deleted": alias})


def api_classes(req: kserver.Request) -> kserver.JsonResponse:
    classes = DB.classes()
    return kserver.JsonResponse({"classes": classes})


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


def api_class_delete(req: kserver.Request) -> kserver.JsonResponse:
    name = req.path.split("/")[-1].upper()
    if not DB.delete_class(name):
        return kserver.JsonResponse({"error": "not found"}, status=404)
    return kserver.JsonResponse({"deleted": name})


def api_billing(req: kserver.Request) -> kserver.JsonResponse:
    days = int(req.query.get("days", "30"))
    rows = DB.billing_summary(days)
    return kserver.JsonResponse({"days": days, "rows": rows})


def api_settings(req: kserver.Request) -> kserver.JsonResponse:
    settings = DB.settings()
    return kserver.JsonResponse({"settings": settings})


def api_setting_post(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    key = data.get("key")
    value = data.get("value")
    if not key:
        return kserver.JsonResponse({"error": "key required"}, status=400)
    DB.set_setting(key, str(value))
    return kserver.JsonResponse({"key": key, "value": str(value)})


def api_aliases(req: kserver.Request) -> kserver.JsonResponse:
    aliases = DB.aliases()
    return kserver.JsonResponse({"aliases": aliases})


def api_alias_post(req: kserver.Request) -> kserver.JsonResponse:
    data = req.json()
    glob = data.get("glob")
    target = data.get("target")
    if not glob or not target:
        return kserver.JsonResponse({"error": "glob and target required"}, status=400)
    DB.set_alias(glob, target)
    return kserver.JsonResponse({"glob": glob, "target": target})


def api_alias_delete(req: kserver.Request) -> kserver.JsonResponse:
    glob = req.path.split("/")[-1]
    DB.delete_alias(glob)
    return kserver.JsonResponse({"deleted": glob})


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

def serve_index(req: kserver.Request) -> kserver.StreamResponse:
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return kserver.StreamResponse([html.encode("utf-8")], status=200, headers={"Content-Type": "text/html"}, content_type="text/html")


def serve_static(req: kserver.Request) -> kserver.StreamResponse:
    path = req.path[len("/static/"):]
    file_path = STATIC_DIR / path
    if not file_path.exists() or file_path.is_dir():
        return kserver.StreamResponse([b"Not found"], status=404, headers={"Content-Type": "text/plain"})

    mime = {
        ".js": "application/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }.get(file_path.suffix, "application/octet-stream")

    return kserver.StreamResponse([file_path.read_bytes()], status=200, headers={"Content-Type": mime})


# ---- Build App ---------------------------------------------------------------------

def build_web_app() -> kserver.App:
    app = kserver.App()

    # API routes
    app.route("GET", "/api/models")(api_models)
    app.route("GET", "/api/models/{alias}")(api_model_get)
    app.route("POST", "/api/models")(api_model_post)
    app.route("DELETE", "/api/models/{alias}")(api_model_delete)

    app.route("GET", "/api/classes")(api_classes)
    app.route("POST", "/api/classes")(api_class_post)
    app.route("DELETE", "/api/classes/{name}")(api_class_delete)

    app.route("GET", "/api/billing")(api_billing)

    app.route("GET", "/api/settings")(api_settings)
    app.route("POST", "/api/settings")(api_setting_post)

    app.route("GET", "/api/aliases")(api_aliases)
    app.route("POST", "/api/aliases")(api_alias_post)
    app.route("DELETE", "/api/aliases/{glob}")(api_alias_delete)

    app.route("GET", "/api/key-status/{alias}")(api_key_status)

    # Frontend
    app.route("GET", "/")(serve_index)
    app.route("GET", "/{path:path}")(serve_static)

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4008)
    args = parser.parse_args()

    app = build_web_app()
    print(f"kiko web-ui on http://{args.host}:{args.port}  db={DB_PATH}")
    kserver.serve(app, args.host, args.port)