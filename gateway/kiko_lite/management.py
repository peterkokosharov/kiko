# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Loopback management API of kiko-lite: ``/kiko/v1/*`` for the native tray (docs/API-CONTRACT.md).

The tray's icon and colours come from ``GET /health``; every dialog comes from here. The data is the
same the read-only web page shows (DB rows, key NAMES, recent billing), shaped to the tray contract.
No endpoint returns a key value, and log lines are redacted before they leave the process.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

import kikolib
from kikolib import config as kconfig
from kikolib import server
from kikolib.server import JsonResponse, Request, StreamResponse

from . import catalog_xml
from . import keys as key_names

TOKEN_FILE = "api-token"  # ~/.kiko/api-token, mode 0600, first line
TOKEN_ENV = "KIKO_API_TOKEN"
API_VERSION = 1
_NAME = re.compile(r"[A-Za-z0-9._:-]+\Z")
_CLASS = re.compile(r"[A-Z0-9_]+\Z")
_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# Common secret shapes, cut down before anything is sent to the tray (which redacts again).
_SECRET = re.compile(r"\b(?:sk-|rk-|gh[pousr]_|AIza|xox[baprs]-|nvidia_api_)[A-Za-z0-9_\-]{8,}")


def _redact(line: str) -> str:
    text = _SECRET.sub("***REDACTED***", line)
    # "key=...", "Bearer ...", "x-api-key: ..." -- mask the value, never echo it.
    text = re.sub(r"(?i)\b(key|token|api[_-]?key|authorization)(\s*[:=]\s*)(bearer\s+)?\S+",
                  lambda m: m.group(1) + m.group(2) + (m.group(3) or "") + "***REDACTED***", text)
    return text


def token_for(db_path: Path) -> str:
    """Management token: env wins; else read ~/.kiko/api-token, creating it 0600 on first start.

    The value is returned to the caller (to build and check requests) and never logged or exposed by a route.
    """
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env
    path = Path(db_path).parent / TOKEN_FILE
    try:
        value = path.read_text(encoding="utf-8").splitlines()[0].strip()
        if value:
            return value
    except (OSError, IndexError):
        pass
    value = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
    except FileExistsError:
        value = path.read_text(encoding="utf-8").splitlines()[0].strip() or value
    except OSError:
        pass  # read-only home: still run, the token only lives in this process
    return value


def _err(status: int, code: str, message: str) -> JsonResponse:
    """The contract's error body (the rest of the gateway uses ``type``; the tray only reads ``message``)."""
    return JsonResponse({"error": {"code": code, "message": message}}, status=status,
                        headers={"Cache-Control": "no-store"})


def _loopback(req: Request) -> JsonResponse | None:
    host = req.headers.get("host")
    peer = req.client[0] if req.client else ""
    if host is None or not server._is_loopback(server._host_only(host)) or not server._is_loopback(peer):
        return _err(403, "forbidden", "the management API is for loopback only")
    return None


def _int_param(req: Request, name: str, default: int, lo: int, hi: int) -> tuple[int, JsonResponse | None]:
    raw = req.query.get(name)
    if raw is None or raw == "":
        return default, None
    if not re.fullmatch(r"\d+", raw):
        return 0, _err(400, "bad_request", f"{name} must be a whole number")
    value = int(raw)
    if not lo <= value <= hi:
        return 0, _err(400, "bad_request", f"{name} must be between {lo} and {hi}")
    return value, None


def _guarded(fn: Callable[["ManagementAPI", Request], JsonResponse]
             ) -> Callable[["ManagementAPI", Request], JsonResponse]:
    """Loopback + management token before any route body runs."""
    def wrapped(self: "ManagementAPI", req: Request) -> JsonResponse:
        denied = _loopback(req)
        if denied is not None:
            return denied
        supplied = req.headers.get("authorization", "")
        if supplied[:7].lower() == "bearer ":
            supplied = supplied[7:].strip()
        else:
            supplied = req.headers.get("x-api-key", "").strip()
        expected = self.token.encode("utf-8")
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected):
            return _err(401, "unauthorized", "invalid management token")
        return fn(self, req)
    return wrapped


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            return [ln.decode("utf-8", "replace") for ln in fh.read().splitlines()[-lines:]]
    except OSError:
        return []


class ManagementAPI:
    def __init__(self, db: Any, db_path: Path, port: int, token: str,
                 env_file: Any | None = None, page: Any | None = None) -> None:
        self.db, self.db_path, self.port = db, Path(db_path), port
        self.token, self.env_file, self.page = token, env_file, page
        self.started = time.time()

    @_guarded
    def status(self, req: Request) -> JsonResponse:
        rows = self.db.models()
        return JsonResponse({
            "api_version": API_VERSION,
            "version": getattr(kikolib, "__version__", ""),
            "state": "ok",
            "uptime_s": int(time.time() - self.started),
            "pid": os.getpid(),
            "db_path": str(self.db_path),
            "models_enabled": sum(1 for r in rows if r.get("enabled")),
        })

    @_guarded
    def models(self, req: Request) -> JsonResponse:
        out = []
        for r in self.db.models():
            roles = r.get("roles") or [r.get("model_class") or "SONNET"]
            out.append({"name": r["alias"], "class": r.get("model_class") or "SONNET",
                        "roles": roles, "roles_text": ", ".join(roles),
                        "enabled": bool(r.get("enabled")), "status": r.get("status") or "unknown",
                        "context_window": r.get("context_window"), "max_output": r.get("max_output")})
        return JsonResponse({"models": out})

    @_guarded
    def models_xml(self, req: Request) -> StreamResponse:
        """The whole catalog as XML (docs/schema/kiko-models.xsd); key NAMES only. Same loopback + token rules."""
        return StreamResponse([catalog_xml.export_xml(self.db.models())],
                              headers={"Cache-Control": "no-store"}, content_type="application/xml; charset=utf-8")

    @_guarded
    def classes(self, req: Request) -> JsonResponse:
        return JsonResponse({"classes": [{"name": c["name"], "keyword": c["keyword"],
                                          "role": c.get("role") or ""} for c in self.db.classes()]})

    @_guarded
    def set_class(self, req: Request) -> JsonResponse:
        name = req.params.get("name", "")
        if not _NAME.fullmatch(name):
            return _err(400, "bad_request", "invalid model name")
        body = req.json()
        cls = body.get("class") if isinstance(body, dict) else None
        if not isinstance(cls, str) or not _CLASS.fullmatch(cls):
            return _err(400, "bad_request", "invalid class name")
        if cls not in {c["name"] for c in self.db.classes()}:
            return _err(400, "bad_request", f"unknown class {cls}")
        if not self.db.set_model_class(name, cls):
            return _err(404, "not_found", f"unknown model {name}")
        return JsonResponse({"name": name, "class": cls})

    @_guarded
    def set_roles(self, req: Request) -> JsonResponse:
        """``POST /kiko/v1/models/{name}/roles`` ``{"roles": ["OPUS", "CODER"], "primary": "OPUS"?}``: replace the
        roles of a model (at least one; the primary is kept when it is still listed, else the first)."""
        name = req.params.get("name", "")
        if not _NAME.fullmatch(name):
            return _err(400, "bad_request", "invalid model name")
        body = req.json()
        roles = body.get("roles") if isinstance(body, dict) else None
        primary = body.get("primary") if isinstance(body, dict) else None
        if (not isinstance(roles, list) or not roles or len(roles) > 64
                or not all(isinstance(x, str) and _CLASS.fullmatch(x) for x in roles)):
            return _err(400, "bad_request", "roles must be a non-empty list of class names")
        if primary is not None and not (isinstance(primary, str) and _CLASS.fullmatch(primary)):
            return _err(400, "bad_request", "invalid primary class name")
        try:
            found = self.db.set_roles(name, roles, primary)
        except ValueError as e:
            return _err(400, "bad_request", str(e))
        if not found:
            return _err(404, "not_found", f"unknown model {name}")
        row = self.db.get_model(name) or {}
        return JsonResponse({"name": name, "class": row.get("model_class"), "roles": row.get("roles", [])})

    @_guarded
    def keys(self, req: Request) -> JsonResponse:
        names: list[str] = []
        for entry in self.db.models():
            n = entry.get("api_key_env")
            if isinstance(n, str) and _KEY_NAME.fullmatch(n):
                names.append(n)
        try:
            from . import providers as ptable
            for entry in ptable.PROVIDERS.values():
                names.extend(n for n in (entry.get("key_names") or ())
                             if isinstance(n, str) and _KEY_NAME.fullmatch(n))
        except Exception:
            pass
        out = []
        for name in dict.fromkeys(names):
            try:
                present = bool(key_names.key_source(name, self.env_file))
            except Exception:
                present = False
            out.append({"name": name, "present": present})
        return JsonResponse({"keys": out})

    def _cap(self) -> float | None:
        raw = self.db.settings().get("monthly_cap_usd")
        if raw in (None, ""):
            raw = kconfig.value("billing.monthly_cap_usd")
        try:
            cap = float(raw)
        except (TypeError, ValueError):
            return None
        return cap if cap > 0 else None

    @_guarded
    def costs(self, req: Request) -> JsonResponse:
        days, bad = _int_param(req, "days", 30, 1, 365)
        if bad is not None:
            return bad
        per_model = []
        for row in self.db.billing_summary(days):
            per_model.append({"model": row.get("model") or "?", "usd": float(row.get("cost_usd") or 0),
                              "tokens_in": int(row.get("tokens_in") or 0),
                              "tokens_out": int(row.get("tokens_out") or 0)})
        today = 0.0
        try:
            rows = self.db.query("SELECT COALESCE(SUM(cost_usd),0) AS usd FROM billing"
                                 " WHERE date(ts)=date('now') AND (route IS NULL OR route IS NOT 'probe')")
            today = float(rows[0]["usd"]) if rows else 0.0
        except Exception:
            pass
        return JsonResponse({"days": days, "today_usd": today,
                             "period_usd": sum(m["usd"] for m in per_model),
                             "cap_usd": self._cap(), "per_model": per_model})

    @_guarded
    def logs(self, req: Request) -> JsonResponse:
        lines, bad = _int_param(req, "lines", 200, 1, 1000)
        if bad is not None:
            return bad
        log_file = kconfig.log_file()
        out = _tail(log_file, lines) if log_file else []
        if not out:  # no [logs] file: show what the DB has, newest last
            try:
                rows = self.db.query("SELECT ts, model, route, error FROM billing ORDER BY id DESC LIMIT ?",
                                     (lines,))
                out = [f"{r['ts']} {r['model']} {r.get('route') or ''} "
                       f"{'ERROR ' + str(r['error']) if r.get('error') else 'ok'}" for r in rows]
                out.reverse()
            except Exception:
                out = []
        return JsonResponse({"lines": [_redact(line) for line in out]})


def install(app: Any, db: Any, db_path: Path, port: int,
            env_file: Any | None = None, page: Any | None = None, token: str | None = None) -> ManagementAPI:
    """Add the contract routes to ``app``. Returns the handler object (it holds the token)."""
    api = ManagementAPI(db, db_path, port, token or token_for(Path(db_path)), env_file, page)
    app.route("GET", "/kiko/v1/status")(api.status)
    app.route("GET", "/kiko/v1/models")(api.models)
    app.route("GET", "/kiko/v1/models.xml")(api.models_xml)
    app.route("GET", "/kiko/v1/classes")(api.classes)
    app.route("POST", "/kiko/v1/models/{name}/class")(api.set_class)
    app.route("POST", "/kiko/v1/models/{name}/roles")(api.set_roles)
    app.route("GET", "/kiko/v1/keys")(api.keys)
    app.route("GET", "/kiko/v1/costs")(api.costs)
    app.route("GET", "/kiko/v1/logs")(api.logs)
    return api
