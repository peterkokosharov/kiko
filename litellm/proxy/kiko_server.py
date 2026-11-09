# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
KIKO — minimal OpenAI-compatible AI proxy.
Replaces the full proxy_server.py entry point.
No PostgreSQL, Redis, JWT, apscheduler or enterprise code.
Billing: KikoSQLiteLogger → ~/.kircho/kircho.db
"""
import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

_kiko_log = logging.getLogger("kiko")

import litellm
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.integrations.kiko_sqlite_logger import (
    _DEFAULT_DB,
    KikoSQLiteLogger,
    instance as _kiko_logger,
    add_model,
    count_all_models,
    delete_model,
    get_all_enabled_aliases,
    get_all_models,
    get_billing_summary,
    get_models_by_class,
    load_models,
    load_response,
    seed_models,
    set_model_class,
    set_model_enabled,
    store_response,
    update_model_status,
)

# Prevent `"no-log": true` in request body from skipping billing callbacks.
litellm.global_disable_no_log_param = True

# ── globals set by lifespan ───────────────────────────────────────────────────
_config_path: str = os.environ.get("CONFIG_FILE_PATH", "")
_config: dict = {}  # raw YAML config (set by _load_config)
_model_list: list = []
# True when any models exist in the DB (enabled or disabled).
# Used by _prepare to distinguish "no models configured" (→ 404) from
# "all models disabled" (→ 503).
_any_models_exist: bool = False
# model_aliases: glob-pattern → kiko model name (from YAML model_aliases section)
_model_aliases: dict = {}

# Map claude model tier keywords to ANTHROPIC_DEFAULT_*_MODEL env vars.
# Checked in order; first match wins.
_ANTHROPIC_ENV_ALIAS_MAP = [
    ("opus",   "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("haiku",  "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("fable",  "ANTHROPIC_DEFAULT_FABLE_MODEL"),
]

# Map tier keywords in model names to DB model_class values.
_CLASS_KEYWORDS = {
    "opus": "OPUS",
    "sonnet": "SONNET",
    "haiku": "HAIKU",
    "fable": "FABLE",
}

# HTTP status → Anthropic error type string
_HTTP_STATUS_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "not_found_error",
    429: "rate_limit_error",
}

_ENV_REF = re.compile(r"^os\.environ/(\w+)$")
_LITELLM_PREFIX = re.compile(r"^litellm\.\w+:\s*")


def _exc_detail(exc: Exception) -> str:
    """Strip upstream 'litellm.ClassName: ' prefix from exception messages."""
    return _LITELLM_PREFIX.sub("", str(exc))


async def _read_json_object(request: Any) -> dict:
    """Parse the request body as a JSON object; 400 (Anthropic envelope) otherwise."""
    from fastapi import HTTPException  # lazy

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _require_model_field(body: dict) -> None:
    """400 when 'model' is missing, empty or not a string."""
    from fastapi import HTTPException  # lazy

    m = body.get("model")
    if not isinstance(m, str) or not m.strip():
        raise HTTPException(status_code=400, detail="'model' is required and must be a non-empty string")


def _is_upstream_5xx(exc: Exception) -> bool:
    """True only with evidence of a real upstream HTTP 5xx response.

    Marker: status >= 500 AND real upstream response headers are present, either on
    exc.response.headers or on exc.litellm_response_headers. litellm's InternalServerError /
    ServiceUnavailableError / BadGatewayError carry a dummy response (fake request URL);
    exception_type() records the headers of the REAL upstream response in
    litellm_response_headers (exception_mapping_utils.py:2372, 2713-2725). A wrapped internal
    error (generic Exception -> OpenAIError(500), or APIConnectionError) has no such headers.
    Excluded outright: fastapi HTTPException (our own) and litellm APIConnectionError (catch-all
    with fixed status 500 for unmapped/internal errors; answers 500, not 502).
    """
    import httpx
    from fastapi import HTTPException

    if isinstance(exc, (HTTPException, litellm.exceptions.APIConnectionError)):
        return False
    status = getattr(exc, "status_code", None)
    resp = getattr(exc, "response", None)
    if status is None and isinstance(resp, httpx.Response):
        status = resp.status_code
    if not (isinstance(status, int) and status >= 500):
        return False
    resp_headers = resp.headers if isinstance(resp, httpx.Response) else None
    return bool(resp_headers) or bool(getattr(exc, "litellm_response_headers", None))


def _rate_limit_headers(exc) -> dict | None:
    """Extract retry-after / retry-after-ms from wherever litellm stores them.

    Real upstream 429s do not pass ``headers=`` to RateLimitError.__init__;
    instead the response headers live on ``exc.response.headers`` or on
    ``exc.litellm_response_headers``.  We probe all three sources in order.
    """
    for src in (
        getattr(exc, "headers", None),
        getattr(exc, "litellm_response_headers", None),
        getattr(getattr(exc, "response", None), "headers", None),
    ):
        if src is None:
            continue
        result = {}
        for key in ("retry-after", "retry-after-ms"):
            val = src.get(key) if hasattr(src, "get") else None
            if val is not None:
                result[key] = str(val)
        if result:
            return result
    return None


def _resolve_model_alias(model: str) -> str:
    """
    Resolve model aliases before _prepare() runs.

    Priority:
    1. YAML model_aliases glob patterns (loaded into _model_aliases).
    2. Class-based routing: keyword in model name → best alive DB model of that class.
    3. ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL env vars — applied
       when the model name contains the matching tier keyword (case-insensitive).

    Returns the original model name when no alias matches.
    """
    # 1. Config glob patterns (e.g. "claude-sonnet-*" → "mock-claude")
    for pattern, target in _model_aliases.items():
        if fnmatch.fnmatch(model, pattern):
            return target
    # 2. Class-based routing: keyword in model name → best alive DB model of that class
    ml = model.lower()
    for kw, cls in _CLASS_KEYWORDS.items():
        if kw in ml:
            candidates = get_models_by_class(_db_path(), cls)
            if candidates:
                return candidates[0]
            break  # class matched but no live models — fall through to env var
    # 3. Env-var tier aliases for claude-* models
    for keyword, env_var in _ANTHROPIC_ENV_ALIAS_MAP:
        if keyword in ml:
            target = os.environ.get(env_var, "")
            if target:
                return target
    return model


def _cred_get(var_name: str) -> str:
    """
    Reads a secret for <VAR_NAME> from the platform credential store.
    Returns the value, or '' if not found or on any failure.
    stdlib only (ctypes, subprocess, shutil).

    Backends tried in order:

    Windows (sys.platform == 'win32'):
      - Windows Credential Manager (WCM) via ctypes/advapi32.
        Target name: KIKO/KIKO_IDENTITY/<VAR_NAME>.
        Returns the value if found; '' otherwise.  No further backends tried.

    Non-Windows:
      a. secret-tool (GNOME Keyring / KDE Plasma with libsecret):
         Requires DBUS_SESSION_BUS_ADDRESS env var and secret-tool on PATH.
         Lookup: kiko-identity=<VAR_NAME> (attribute stored by kiko-identity).
      b. dcop kwalletd (TDE / q4os Trinity Desktop):
         Requires dcop on PATH (uses DCOPserver, not D-Bus).
         Opens kdewallet, reads from the Passwords folder, then closes.
      c. Returns ''.
    """
    upper = var_name.upper()

    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes

            class _CRED(ctypes.Structure):
                _fields_ = [
                    ("Flags",              ctypes.wintypes.DWORD),
                    ("Type",               ctypes.wintypes.DWORD),
                    ("TargetName",         ctypes.wintypes.LPWSTR),
                    ("Comment",            ctypes.wintypes.LPWSTR),
                    ("LastWritten",        ctypes.wintypes.FILETIME),
                    ("CredentialBlobSize", ctypes.wintypes.DWORD),
                    ("CredentialBlob",     ctypes.POINTER(ctypes.wintypes.BYTE)),
                    ("Persist",            ctypes.wintypes.DWORD),
                    ("AttributeCount",     ctypes.wintypes.DWORD),
                    ("Attributes",         ctypes.c_void_p),
                    ("TargetAlias",        ctypes.wintypes.LPWSTR),
                    ("UserName",           ctypes.wintypes.LPWSTR),
                ]

            adv = ctypes.windll.advapi32
            for target in (f"KIKO/KIKO_IDENTITY/{upper}",):
                ptr = ctypes.c_void_p()
                if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)):
                    continue
                try:
                    cred = ctypes.cast(ptr, ctypes.POINTER(_CRED)).contents
                    blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
                    value = blob.decode("utf-16-le")
                finally:
                    adv.CredFree(ptr)
                if value:
                    return value
            return ""
        except Exception:
            return ""

    # Non-Windows: try secret-tool, then dcop kwalletd.

    # a. secret-tool (GNOME Keyring / KDE Plasma via libsecret)
    #    kiko-identity stores secrets with attribute "kiko-identity"=<name>.
    #    D-Bus is required for the Secret Service API.
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS") and shutil.which("secret-tool"):
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", "kiko-identity", upper],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass

    # b. dcop kwalletd (TDE / Trinity Desktop — uses DCOPserver, not D-Bus)
    #    NOTE: needs live verification on q4os/TDE before production use.
    if shutil.which("dcop"):
        handle_str = ""
        try:
            open_result = subprocess.run(
                ["dcop", "kwalletd", "open", "kdewallet", "0"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if open_result.returncode == 0 and open_result.stdout.strip():
                raw = open_result.stdout.strip()
                try:
                    handle_int = int(raw)
                except ValueError:
                    pass
                else:
                    if handle_int >= 0:
                        handle_str = raw

            if handle_str:
                read_result = None
                try:
                    read_result = subprocess.run(
                        ["dcop", "kwalletd", "readPassword", handle_str, "Passwords", upper],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                finally:
                    try:
                        subprocess.run(
                            ["dcop", "kwalletd", "close", handle_str, "0"],
                            capture_output=True,
                            timeout=3,
                        )
                    except (subprocess.SubprocessError, OSError):
                        pass
                if read_result is not None and read_result.returncode == 0 and read_result.stdout.strip():
                    return read_result.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass

    return ""


def _db_path() -> Path:
    """Return the kircho.db path: KIKO_DB_PATH env var or default ~/.kircho/kircho.db."""
    custom = os.environ.get("KIKO_DB_PATH", "")
    return Path(custom) if custom else _DEFAULT_DB


def _resolve_env(val: str) -> str:
    """Resolve os.environ/VAR references. Falls back to platform credential store (_cred_get)."""
    m = _ENV_REF.match(val)
    if not m:
        return val
    var_name = m.group(1)
    return os.environ.get(var_name) or _cred_get(var_name)


def _resolve_model_params(model_name: str) -> dict:
    """Return litellm_params for a model alias, with os.environ/ refs expanded."""
    for entry in _model_list:
        if entry.get("model_name") == model_name:
            raw = dict(entry.get("litellm_params", {}))
            return {k: _resolve_env(v) if isinstance(v, str) else v for k, v in raw.items()}
    return {}


# Fields that the client is never allowed to supply — they could redirect
# requests to attacker-controlled infrastructure or inject fake credentials.
_DANGEROUS_BODY_FIELDS = frozenset({
    "api_key",
    "api_base",
    "base_url",
    "extra_headers",
    "mock_response",
    "custom_llm_provider",
    "model_info",
    # Security additions: prevent routing/billing/mock injection via client body
    "fallbacks",           # would route through attacker-controlled model config
    "headers",             # alias for extra_headers in litellm
    "input_cost_per_token",      # client could manipulate billing cost
    "output_cost_per_token",     # client could manipulate billing cost
    "input_cost_per_second",     # client could manipulate billing cost
    "output_cost_per_second",    # client could manipulate billing cost
    "base_model",                # client must not override model billing reference
    "model_list",          # client must not override the server's model list
    "mock_tool_calls",     # mock injection
    "mock_timeout",        # mock injection
})


def _prepare(model: str, body: dict) -> dict:
    """
    Security chokepoint called before every upstream request.

    1. Strip fields the client must never supply (api_key, api_base, …).
    2. Enforce the model allowlist unconditionally — there is no bypass:
         - 404 "No models configured" when _model_list is empty and no models
           exist at all (DB never initialised or YAML-only config without --init-db).
         - 503 "No models available" when _model_list is empty but models do
           exist in the DB (all are disabled).
         - 404 "Model not found" when _model_list is non-empty but the requested
           alias is absent.
    3. Inject/override with the kiko-configured litellm_params so that
       api_base (and api_key) always come from the server config, never from
       the client body.
    """
    from fastapi import HTTPException  # lazy — avoids heavy import at module load

    # 1. Strip dangerous fields unconditionally.
    clean = {k: v for k, v in body.items() if k not in _DANGEROUS_BODY_FIELDS}

    # 2. Enforce allowlist unconditionally — no bypass when model_list is empty.
    if not _model_list:
        if _any_models_exist:
            raise HTTPException(status_code=503, detail="No models available")
        raise HTTPException(status_code=404, detail="No models configured")
    entry = next(
        (m for m in _model_list if m.get("model_name") == model), None
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Model not found")

    # 3. Inject configured litellm_params (api_base always wins over client).
    resolved = {
        k: _resolve_env(v) if isinstance(v, str) else v
        for k, v in entry.get("litellm_params", {}).items()
    }

    return {**clean, **resolved}

# Parsed from general_settings; defaults here, overridden by _load_config.
_billing_enabled: bool = True
_billing_logger = _kiko_logger  # may be replaced by a custom-path instance
_monthly_cap_usd: Optional[float] = None  # None = no cap enforced
_billing_db_path: Optional[Path] = None   # set when billing_db is in config


async def _check_monthly_cap(route: str) -> Any:
    """
    Check current-month spending against monthly_cap_usd.

    Returns None when the cap is not set or not yet reached.
    Returns a JSONResponse(429) with the correct error shape for `route` when
    the cap is met or exceeded.

    Route groups:
      - "messages", "messages_count_tokens" → Anthropic error shape
      - "gemini_generate", "gemini_stream", "gemini_count_tokens" → Gemini error shape
      - everything else → OpenAI error shape
    """
    if _monthly_cap_usd is None:
        return None
    import sqlite3 as _sqlite3
    from fastapi.responses import JSONResponse as _JSONResponse
    db = _billing_db_path or _db_path()
    if not db.exists():
        return None
    con = _sqlite3.connect(str(db), timeout=5)
    row = con.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM billing "
        "WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')"
    ).fetchone()
    con.close()
    spent = float(row[0]) if row else 0.0
    if spent < _monthly_cap_usd:
        return None
    msg = f"Monthly spending cap of {_monthly_cap_usd:.2f} USD reached"
    if route in ("messages", "messages_count_tokens"):
        content: dict = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": msg},
        }
    elif route in ("gemini_generate", "gemini_stream", "gemini_count_tokens"):
        content = {
            "error": {"code": 429, "message": msg, "status": "RESOURCE_EXHAUSTED"},
        }
    else:
        content = {
            "error": {
                "message": msg,
                "type": "insufficient_quota",
                "code": "monthly_cap_exceeded",
            },
        }
    return _JSONResponse(status_code=429, content=content)


def _load_config(path: str) -> None:
    """
    Parse the YAML config and apply settings to litellm globals.
    Model list: DB is source of truth (if populated); falls back to YAML.
    Handles:
      - model_list (DB first, YAML fallback)
      - model_aliases (glob → kiko model name mappings)
      - litellm_settings (all keys; success_callback resolves "kiko_sqlite" alias)
      - general_settings.disable_billing   → skips KikoSQLiteLogger
      - general_settings.billing_db        → custom SQLite path
      - general_settings.health_check_interval → background health check period (s)
    """
    global _model_list, _any_models_exist, _billing_enabled, _billing_logger, _model_aliases, _monthly_cap_usd, _billing_db_path, _config
    import yaml

    with open(path) as fh:
        conf = yaml.safe_load(fh) or {}

    _config = conf

    # DB is always the source of truth — no YAML fallback.
    # Use `kiko --init-db` to seed the DB from the YAML model_list on first run.
    db = _db_path()
    db_models = load_models(db)
    _model_list = db_models
    _any_models_exist = bool(db_models) or count_all_models(db) > 0
    litellm.model_list = _model_list

    # model_aliases: glob → kiko model name (optional section)
    _model_aliases = dict(conf.get("model_aliases", {}))

    # general_settings — billing knobs only; auth is handled by user_api_key_auth
    gs = conf.get("general_settings", {})
    if gs.get("disable_billing", False):
        _billing_enabled = False

    cap = gs.get("monthly_cap_usd")
    if cap is not None:
        _monthly_cap_usd = float(cap)

    custom_db = gs.get("billing_db", "")
    if custom_db and _billing_enabled:
        from litellm.proxy.integrations.kiko_sqlite_logger import KikoSQLiteLogger
        _billing_db_path = Path(custom_db)
        _billing_logger = KikoSQLiteLogger(db_path=_billing_db_path)

    # litellm_settings — pass through; resolve "kiko_sqlite" alias
    for key, val in conf.get("litellm_settings", {}).items():
        if key in ("success_callback", "callbacks") and isinstance(val, list):
            # Resolve "kiko_sqlite" string alias; leave other items as strings
            # (litellm resolves built-in string callbacks itself)
            resolved = []
            for item in val:
                if item == "kiko_sqlite":
                    _billing_enabled = True  # explicit opt-in
                else:
                    resolved.append(item)
            if resolved:
                try:
                    setattr(litellm, key, resolved)
                except AttributeError:
                    pass
            continue
        try:
            setattr(litellm, key, val)
        except AttributeError:
            pass


# ── discover_models ───────────────────────────────────────────────────────────

async def discover_models(db_path: Path, model_list: list) -> dict:
    """
    Ping each model with a 1-token request. Updates status in DB.
    Returns {alias: 'ok'|'error'} dict.
    """
    results: dict = {}
    for entry in model_list:
        alias = entry["model_name"]
        resolved = {
            k: _resolve_env(v) if isinstance(v, str) else v
            for k, v in entry.get("litellm_params", {}).items()
        }
        try:
            await litellm.acompletion(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=400,  # reasoning models (gemini-3.8-flash, nemotron) need budget
                metadata={"kiko_health_check": True},
                **resolved,
            )
            status = "ok"
        except Exception:
            status = "error"
        update_model_status(db_path, alias, status)
        results[alias] = status
    return results


def _get_health_interval() -> int:
    """Return the configured health check interval in seconds (default 300, min 1).

    Validates that the configured value is a non-negative integer; falls back to
    300 on bad input and clamps to at least 1 to prevent tight loops.
    """
    raw = _config.get("general_settings", {}).get("health_check_interval", 300)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = 300
    return max(1, value)


async def _run_health_check(db_path: Path) -> None:
    """Single health check iteration: probe ALL enabled models (including errored ones),
    update their status in the DB, then reload the routing list.

    Health pings carry metadata={"kiko_health_check": True} so KikoSQLiteLogger
    skips billing for them regardless of which callback list they land in.
    The entire body is wrapped in try/except Exception so any transient error
    (DB unavailable, network failure, bad config) is logged and the loop continues.
    asyncio.CancelledError is a BaseException and therefore not caught here,
    so task cancellation always stops the loop cleanly.
    """
    global _model_list
    try:
        all_models = get_all_enabled_aliases(db_path)
        await discover_models(db_path, all_models)
        # Reload routing list so updated statuses take effect immediately.
        _model_list = load_models(db_path)
    except Exception as exc:
        _kiko_log.warning("health check failed: %s", exc)


# ── SSE streaming helper ──────────────────────────────────────────────────────

async def _stream_sse(response: Any) -> AsyncGenerator[str, None]:
    try:
        async for chunk in response:
            try:
                data = chunk.model_dump(exclude_unset=True)
            except AttributeError:
                data = chunk
            yield f"data: {json.dumps(data)}\n\n"
    finally:
        yield "data: [DONE]\n\n"


# ── application factory ───────────────────────────────────────────────────────
# fastapi is imported lazily here so that `import litellm.proxy.kiko_server`
# does not pull in websockets or other server-only dependencies.

def create_app() -> Any:
    """Create and return the configured FastAPI application."""
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

    async def _local_only(request: Request) -> None:
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(status_code=403, detail="Local access only")

    @asynccontextmanager
    async def _lifespan(app: Any) -> AsyncGenerator[None, None]:
        cfg = _config_path
        if cfg and Path(cfg).exists():
            _load_config(cfg)

        litellm.set_verbose = False
        litellm.drop_params = True
        litellm.suppress_debug_info = True
        # Route OpenAI-provider /v1/messages requests through chat/completions
        # instead of the Responses API (avoids the adapter gap for non-Anthropic backends).
        litellm.use_chat_completions_url_for_anthropic_messages = True

        if _billing_enabled and _billing_logger not in litellm.callbacks:
            litellm.callbacks.append(_billing_logger)

        async def _health_loop() -> None:
            while True:
                await asyncio.sleep(_get_health_interval())
                await _run_health_check(_db_path())

        task = asyncio.create_task(_health_loop())
        yield  # server runs
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(
        title="KIKO",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    # ── Anthropic error shape exception handler ────────────────────────────────
    @app.exception_handler(HTTPException)
    async def _anthropic_http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Wrap HTTPException details in the Anthropic error body shape."""
        error_type = _HTTP_STATUS_ERROR_TYPE.get(exc.status_code, "api_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": "error",
                "error": {
                    "type": error_type,
                    "message": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
                },
            },
            headers=dict(exc.headers) if exc.headers else None,
        )

    router = APIRouter()

    # ── health ────────────────────────────────────────────────────────────────

    @router.get("/health", tags=["health"])
    @router.get("/health/liveliness", tags=["health"])
    async def health() -> dict:
        db = _db_path()
        classes: dict = {}
        if db.exists():
            con = sqlite3.connect(str(db), timeout=5)
            try:
                rows = con.execute(
                    "SELECT model_class, status, COUNT(*) FROM models "
                    "WHERE enabled=1 GROUP BY model_class, status"
                ).fetchall()
            finally:
                con.close()
            for cls, st, cnt in rows:
                if cls not in classes:
                    classes[cls] = {"alive": 0, "total": 0}
                classes[cls]["total"] += cnt
                if st == "ok":
                    classes[cls]["alive"] += cnt
        return {"status": "healthy", "classes": classes}

    # ── models ────────────────────────────────────────────────────────────────

    @router.get(
        "/v1/models",
        dependencies=[Depends(user_api_key_auth)],
        tags=["models"],
    )
    @router.get(
        "/models",
        dependencies=[Depends(user_api_key_auth)],
        tags=["models"],
    )
    async def list_models() -> dict:
        data = [
            {"id": m["model_name"], "object": "model", "owned_by": "kiko"}
            for m in _model_list
        ]
        return {"object": "list", "data": data}

    # ── chat completions ──────────────────────────────────────────────────────

    @router.post(
        "/v1/chat/completions",
        dependencies=[Depends(user_api_key_auth)],
        tags=["chat"],
    )
    @router.post(
        "/chat/completions",
        dependencies=[Depends(user_api_key_auth)],
        tags=["chat"],
    )
    async def chat_completions(request: Request) -> Any:
        if (cap_resp := await _check_monthly_cap("chat")) is not None:
            return cap_resp
        body: dict = await request.json()
        model: str = _resolve_model_alias(body.get("model", ""))
        stream: bool = bool(body.get("stream", False))
        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(model, body)
        params["metadata"] = {"route": "chat"}
        try:
            response = await litellm.acompletion(**params)
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_exc_detail(exc))

        if stream:
            return StreamingResponse(
                _stream_sse(response),
                media_type="text/event-stream",
            )
        return response.model_dump(exclude_unset=True)

    # ── completions ───────────────────────────────────────────────────────────

    @router.post(
        "/v1/completions",
        dependencies=[Depends(user_api_key_auth)],
        tags=["completions"],
    )
    @router.post(
        "/completions",
        dependencies=[Depends(user_api_key_auth)],
        tags=["completions"],
    )
    async def completions(request: Request) -> Any:
        if (cap_resp := await _check_monthly_cap("completions")) is not None:
            return cap_resp
        body: dict = await request.json()
        model: str = _resolve_model_alias(body.get("model", ""))
        stream: bool = bool(body.get("stream", False))
        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(model, body)
        params["metadata"] = {"route": "completions"}
        try:
            response = await litellm.atext_completion(**params)
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_exc_detail(exc))

        if stream:
            return StreamingResponse(
                _stream_sse(response),
                media_type="text/event-stream",
            )
        return response.model_dump(exclude_unset=True)

    # ── embeddings ────────────────────────────────────────────────────────────

    @router.post(
        "/v1/embeddings",
        dependencies=[Depends(user_api_key_auth)],
        tags=["embeddings"],
    )
    @router.post(
        "/embeddings",
        dependencies=[Depends(user_api_key_auth)],
        tags=["embeddings"],
    )
    async def embeddings(request: Request) -> Any:
        if (cap_resp := await _check_monthly_cap("embeddings")) is not None:
            return cap_resp
        body: dict = await request.json()
        model: str = _resolve_model_alias(body.get("model", ""))
        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(model, body)
        params["metadata"] = {"route": "embeddings"}
        try:
            response = await litellm.aembedding(**params)
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_exc_detail(exc))
        return response.model_dump(exclude_unset=True)

    # ── messages (Anthropic Messages API) ─────────────────────────────────────

    @router.post(
        "/v1/messages",
        dependencies=[Depends(user_api_key_auth)],
        tags=["messages"],
    )
    async def messages_route(request: Request) -> Any:
        if (cap_resp := await _check_monthly_cap("messages")) is not None:
            return cap_resp
        body: dict = await _read_json_object(request)
        _require_model_field(body)
        if not isinstance(body.get("messages"), list):
            raise HTTPException(status_code=400, detail="'messages' is required and must be an array")
        model: str = _resolve_model_alias(body.get("model", ""))
        stream: bool = bool(body.get("stream", False))
        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(model, body)
        params["metadata"] = {"route": "messages"}
        try:
            response = await litellm.anthropic_messages(**params)
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=502 if _is_upstream_5xx(exc) else 500, detail=_exc_detail(exc)
            )

        if stream:
            # litellm.anthropic_messages(stream=True) returns AsyncIterator[bytes]
            # where each chunk is already SSE-formatted:
            #   b"event: <type>\ndata: <json>\n\n"
            # Billing callbacks fire when the iterator is fully consumed.
            async def _anthropic_sse_gen() -> AsyncGenerator[bytes, None]:
                async for chunk in response:
                    if isinstance(chunk, bytes):
                        yield chunk
                    else:
                        yield str(chunk).encode()

            return StreamingResponse(
                _anthropic_sse_gen(),
                media_type="text/event-stream",
            )

        # AnthropicMessagesResponse is a TypedDict (plain dict) — return directly.
        if hasattr(response, "model_dump"):
            return response.model_dump(exclude_unset=True)
        return dict(response)

    # ── count_tokens (Anthropic Messages API) ─────────────────────────────────

    @router.post(
        "/v1/messages/count_tokens",
        dependencies=[Depends(user_api_key_auth)],
        tags=["messages"],
    )
    async def count_tokens_route(request: Request) -> Any:
        """
        Count input tokens for a Messages API request without calling the upstream.
        Body: same as /v1/messages but "stream" is ignored.
        Response: {"input_tokens": N}
        """
        if (cap_resp := await _check_monthly_cap("messages_count_tokens")) is not None:
            return cap_resp
        body: dict = await request.json()
        model: str = _resolve_model_alias(body.get("model", ""))
        messages: list = list(body.get("messages", []))
        # Anthropic format allows a top-level "system" string; prepend it
        # as a system role message for token counting purposes.
        system = body.get("system")
        if system and isinstance(system, str):
            messages = [{"role": "system", "content": system}] + messages
        try:
            n: int = litellm.token_counter(model=model, messages=messages)
        except Exception:
            n = 0
        return {"input_tokens": n}

    # ── Gemini generateContent ────────────────────────────────────────────────

    @router.post(
        "/v1beta/models/{model_id}:generateContent",
        dependencies=[Depends(user_api_key_auth)],
        tags=["gemini"],
    )
    @router.post(
        "/models/{model_id}:generateContent",
        dependencies=[Depends(user_api_key_auth)],
        tags=["gemini"],
    )
    async def generate_content_route(model_id: str, request: Request) -> Any:
        """
        Gemini-compatible generateContent endpoint.
        Translates Gemini request/response format via the google_genai adapter.
        Always routes through the adapter (never the native Gemini HTTP path).
        Response: generateContentResponse with candidates and usageMetadata.
        """
        if (cap_resp := await _check_monthly_cap("gemini_generate")) is not None:
            return cap_resp
        from typing import cast as _cast
        from litellm.google_genai.adapters.handler import GOOGLE_GENAI_ADAPTER
        from litellm.types.utils import ModelResponse

        body: dict = await request.json()
        kiko_model: str = _resolve_model_alias(model_id)
        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(kiko_model, {})

        # The configured litellm model (e.g. "openai/gpt-4o-mini" or "gemini/gemini-2.0-flash")
        litellm_model: str = params.get("model", kiko_model)

        # Extract Gemini-specific fields; strip anything client should never supply
        contents = body.get("contents", [])
        config = body.get("generationConfig")
        extra_kwargs = {
            k: v for k, v in body.items()
            if k not in ("contents", "generationConfig") and k not in _DANGEROUS_BODY_FIELDS
        }

        # Translate Gemini → OpenAI format via adapter (avoids native Gemini HTTP path)
        completion_request = GOOGLE_GENAI_ADAPTER.translate_generate_content_to_completion(
            model=litellm_model,
            contents=contents,
            config=config,
            litellm_params=None,
            **extra_kwargs,
        )

        # Merge with server-side credentials/routing (api_key, api_base, …)
        completion_kwargs = {**dict(completion_request), **params}
        completion_kwargs["metadata"] = {"route": "gemini_generate"}

        try:
            completion_response = await litellm.acompletion(**completion_kwargs)
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_exc_detail(exc))

        # Translate OpenAI → Gemini format
        return GOOGLE_GENAI_ADAPTER.translate_completion_to_generate_content(
            _cast(ModelResponse, completion_response)
        )

    # ── Gemini streamGenerateContent ─────────────────────────────────────────

    @router.post(
        "/v1beta/models/{model_id}:streamGenerateContent",
        dependencies=[Depends(user_api_key_auth)],
        tags=["gemini"],
    )
    @router.post(
        "/models/{model_id}:streamGenerateContent",
        dependencies=[Depends(user_api_key_auth)],
        tags=["gemini"],
    )
    async def stream_generate_content_route(model_id: str, request: Request) -> Any:
        """
        Gemini-compatible streamGenerateContent endpoint.
        Translates Gemini request/response via the google_genai adapter.
        Response: data: <json>\\n\\n chunks (NO event: line, NO [DONE] marker).
        Last chunk has finishReason and usageMetadata.
        Billing fires after stream is exhausted.
        """
        if (cap_resp := await _check_monthly_cap("gemini_stream")) is not None:
            return cap_resp
        from litellm.google_genai.adapters.transformation import (
            GoogleGenAIAdapter as _GeminiAdapter,
            GoogleGenAIStreamWrapper,
        )

        body: dict = await request.json()
        kiko_model: str = _resolve_model_alias(model_id)
        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(kiko_model, {})

        # The configured litellm model (e.g. "openai/gpt-4o-mini" or "gemini/gemini-2.0-flash")
        litellm_model: str = params.get("model", kiko_model)

        # Extract Gemini-specific fields; strip anything client should never supply
        contents = body.get("contents", [])
        config = body.get("generationConfig")
        extra_kwargs = {
            k: v for k, v in body.items()
            if k not in ("contents", "generationConfig") and k not in _DANGEROUS_BODY_FIELDS
        }

        from litellm.google_genai.adapters.handler import GOOGLE_GENAI_ADAPTER

        # Translate Gemini → OpenAI format via adapter
        completion_request = GOOGLE_GENAI_ADAPTER.translate_generate_content_to_completion(
            model=litellm_model,
            contents=contents,
            config=config,
            litellm_params=None,
            **extra_kwargs,
        )

        # Merge with server-side credentials/routing; force streaming + request usage
        # stream_options.include_usage asks the backend to include usage data in the
        # stream so we can surface totalTokenCount in the last Gemini chunk.
        completion_kwargs = {
            **dict(completion_request),
            **params,
            "stream": True,
            "stream_options": {"include_usage": True},
            "metadata": {"route": "gemini_stream"},
        }

        try:
            completion_response = await litellm.acompletion(**completion_kwargs)
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_exc_detail(exc))

        # Custom SSE generator: translates OpenAI streaming chunks → Gemini format.
        # Emits "data: <json>\n\n" — NO event: prefix, NO [DONE] marker.
        #
        # litellm (with stream_options.include_usage) strips usage from the
        # finish_reason chunk and emits it as a separate trailing chunk.
        # We therefore buffer the finish_reason Gemini chunk and accumulate
        # usage from any subsequent chunk, then emit the buffered chunk with
        # a correct usageMetadata before the generator exits.
        # Billing callbacks fire when completion_response is fully exhausted.
        async def _gemini_sse_gen() -> AsyncGenerator[bytes, None]:
            _adapter = _GeminiAdapter()
            # Wrapper is used only for accumulated_tool_calls state — we iterate
            # completion_response directly rather than via the wrapper.
            wrapper = GoogleGenAIStreamWrapper(completion_stream=completion_response)
            accumulated_usage: Any = None
            buffered_final: Optional[dict] = None

            async for chunk in completion_response:
                if chunk is None:
                    continue

                # Collect usage from any chunk that carries it (may arrive on a
                # synthetic trailing chunk separate from the finish_reason chunk).
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    accumulated_usage = chunk_usage

                transformed = _adapter.translate_streaming_completion_to_generate_content(
                    chunk, wrapper
                )
                if not isinstance(transformed, dict):
                    continue

                # Detect the finish_reason chunk and buffer it so we can attach
                # the accumulated usage before emitting.
                has_finish = any(
                    c.get("finishReason")
                    for c in transformed.get("candidates", [])
                )
                if has_finish:
                    buffered_final = transformed
                else:
                    yield f"data: {json.dumps(transformed)}\n\n".encode()

            # Flush any tool calls that were still accumulating when the stream ended.
            if wrapper.accumulated_tool_calls:
                parts = []
                for tc in wrapper.accumulated_tool_calls.values():
                    try:
                        parsed = json.loads(tc["arguments"] or "{}")
                        parts.append({
                            "functionCall": {
                                "name": tc["name"] or "undefined_tool_name",
                                "args": parsed,
                            }
                        })
                    except Exception:
                        pass
                wrapper.accumulated_tool_calls.clear()
                if parts:
                    chunk_out: dict = {
                        "candidates": [{
                            "content": {"parts": parts, "role": "model"},
                            "finishReason": "STOP",
                            "index": 0,
                            "safetyRatings": [],
                        }]
                    }
                    if buffered_final is None:
                        buffered_final = chunk_out
                    else:
                        yield f"data: {json.dumps(chunk_out)}\n\n".encode()

            # Emit the finish_reason chunk with accumulated usageMetadata.
            if buffered_final is not None:
                if accumulated_usage is not None:
                    buffered_final["usageMetadata"] = _adapter._map_usage(accumulated_usage)
                yield f"data: {json.dumps(buffered_final)}\n\n".encode()

        return StreamingResponse(
            _gemini_sse_gen(),
            media_type="text/event-stream",
        )

    # ── OpenAI Responses API (Steps 10 + 11) ─────────────────────────────────

    # Tool types that kiko does not support; stripped from incoming requests.
    _STRIPPED_TOOL_TYPES = frozenset({"web_search", "computer_use_preview"})

    @router.post(
        "/v1/responses",
        dependencies=[Depends(user_api_key_auth)],
        tags=["responses"],
    )
    async def responses_route(request: Request) -> Any:
        """
        OpenAI Responses API — non-streaming (Step 10) and streaming (Step 11).
        Codex CLI sends POST /v1/responses; kiko bridges to chat/completions
        via litellm.aresponses(use_chat_completions_api=True).
        web_search / computer_use_preview tools are stripped before forwarding.

        Streaming (stream=true):
          Returns text/event-stream with the sequence:
            event: response.created
            event: response.output_item.added
            event: response.content_part.added
            event: response.output_text.delta  (one or more)
            event: response.output_text.done
            event: response.output_item.done
            event: response.completed
          Each frame: "event: {type}\\ndata: {json}\\n\\n"
          Billing fires when the iterator is exhausted.
        """
        if (cap_resp := await _check_monthly_cap("responses")) is not None:
            return cap_resp
        body: dict = await _read_json_object(request)
        _require_model_field(body)
        model: str = _resolve_model_alias(body.get("model", ""))
        stream: bool = bool(body.get("stream", False))

        # _prepare: 404 on unknown model, strip client keys/api_base, inject config.
        params = _prepare(model, body)

        # Extract named params that aresponses receives explicitly.
        # litellm_params in params override the alias; pop them so **params
        # does not clash with keyword arguments below.
        input_data = params.pop("input", body.get("input", []))
        model_resolved = params.pop("model", model)
        params.pop("stream", None)               # extracted above; passed explicitly
        params.pop("previous_response_id", None) # passed explicitly below
        params.pop("tools", None)                # replaced by filtered list

        # Strip unsupported tool types; pass None when nothing remains.
        # Use isinstance guard: non-dict entries would raise AttributeError on .get().
        raw_tools: list = body.get("tools") or []
        filtered_tools = [
            t for t in raw_tools
            if isinstance(t, dict) and t.get("type") not in _STRIPPED_TOOL_TYPES
        ] or None

        previous_response_id = body.get("previous_response_id")
        params["metadata"] = {"route": "responses"}

        # Normalize string input to list format (list("hello") = ['h','e','l','l','o'] breaks chaining).
        if isinstance(input_data, str):
            input_data = [{"role": "user", "content": input_data}]

        # History chaining: load the prior response and prepend its items.
        if previous_response_id:
            _hist_db = _billing_db_path or _db_path()
            prior = load_response(_hist_db, previous_response_id)
            if prior is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"previous_response_id '{previous_response_id}' not found",
                )
            prior_input, prior_output = prior
            input_data = prior_input + prior_output + list(input_data)

        try:
            response = await litellm.aresponses(
                input=input_data,
                model=model_resolved,
                use_chat_completions_api=True,
                stream=stream,
                tools=filtered_tools,
                previous_response_id=None,  # handled by kiko; not forwarded to upstream
                **params,
            )
        except litellm.exceptions.AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=_exc_detail(exc))
        except litellm.exceptions.BadRequestError as exc:
            raise HTTPException(status_code=400, detail=_exc_detail(exc))
        except litellm.exceptions.RateLimitError as exc:
            raise HTTPException(status_code=429, detail=_exc_detail(exc), headers=_rate_limit_headers(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=502 if _is_upstream_5xx(exc) else 500, detail=_exc_detail(exc)
            )

        if stream:
            # litellm.aresponses(stream=True) returns a LiteLLMCompletionStreamingIterator
            # (BaseResponsesAPIStreamingIterator subclass) — an async iterator that yields
            # pydantic event objects with a `type` attribute.
            # Each SSE frame: "event: {type}\ndata: {json}\n\n"
            # Billing callbacks fire when the iterator is fully exhausted.
            # The real response id comes from the response.completed event; capture it
            # inside the generator and store AFTER the stream ends so that chained
            # calls (previous_response_id) find the correct id.
            _store_db_s = _billing_db_path or _db_path()
            _input_snap = input_data  # capture before generator; do not mutate after

            async def _responses_sse_gen() -> AsyncGenerator[str, None]:
                _resp_id = None
                _output_items: list = []
                async for item in response:
                    try:
                        data = item.model_dump_json()
                    except AttributeError:
                        data = json.dumps(str(item))
                    # The `type` attribute may be a str-subclass Enum member.
                    # model_dump_json() serialises it to the correct string value,
                    # so parse it back from JSON rather than relying on f-string
                    # formatting of the enum (which varies across Python versions).
                    try:
                        parsed = json.loads(data)
                        event_type = parsed.get("type", "")
                    except Exception:
                        parsed = {}
                        event_type = str(getattr(item, "type", ""))
                    if event_type == "response.completed":
                        try:
                            resp_obj = parsed.get("response", {})
                            _resp_id = resp_obj.get("id", "")
                            _output_items = resp_obj.get("output", [])
                        except Exception:
                            pass
                    yield f"event: {event_type}\ndata: {data}\n\n"
                if _resp_id:
                    try:
                        store_response(_store_db_s, _resp_id, _input_snap, _output_items)
                    except Exception:
                        pass

            return StreamingResponse(
                _responses_sse_gen(),
                media_type="text/event-stream",
            )

        if hasattr(response, "model_dump"):
            result = response.model_dump(exclude_unset=True)
        else:
            result = dict(response)

        # Store this response so a future call can chain via previous_response_id.
        # input_data already has history prepended — store the full snapshot for
        # correct multi-turn depth (each row contains the whole conversation so far).
        resp_id = result.get("id", "")
        output_items = result.get("output", [])
        if resp_id:
            _store_db = _billing_db_path or _db_path()
            try:
                store_response(_store_db, resp_id, input_data, output_items)
            except Exception:
                pass  # don't fail the response over a logging failure

        return result

    # ── Gemini countTokens ────────────────────────────────────────────────────

    @router.post(
        "/v1beta/models/{model_id}:countTokens",
        dependencies=[Depends(user_api_key_auth)],
        tags=["gemini"],
    )
    @router.post(
        "/models/{model_id}:countTokens",
        dependencies=[Depends(user_api_key_auth)],
        tags=["gemini"],
    )
    async def count_tokens_gemini_route(model_id: str, request: Request) -> Any:
        """
        Gemini-compatible countTokens endpoint.
        Translates Gemini contents to messages then counts locally — no upstream call.
        Response: {"totalTokens": N}
        """
        if (cap_resp := await _check_monthly_cap("gemini_count_tokens")) is not None:
            return cap_resp
        from litellm.google_genai.adapters.handler import GOOGLE_GENAI_ADAPTER

        body: dict = await request.json()
        kiko_model: str = _resolve_model_alias(model_id)
        # _prepare: 404 on unknown model; we only need the resolved model name here
        params = _prepare(kiko_model, {})
        litellm_model: str = params.get("model", kiko_model)

        contents = body.get("contents", [])
        config = body.get("generationConfig")

        # Translate Gemini contents → OpenAI messages for local token counting
        completion_request = GOOGLE_GENAI_ADAPTER.translate_generate_content_to_completion(
            model=litellm_model,
            contents=contents,
            config=config,
            litellm_params=None,
        )
        messages = list(completion_request.get("messages", []))

        try:
            n: int = litellm.token_counter(model=litellm_model, messages=messages)
        except Exception:
            n = 0
        return {"totalTokens": n}

    @router.get(
        "/kiko/models",
        dependencies=[Depends(_local_only), Depends(user_api_key_auth)],
        tags=["kiko"],
    )
    async def kiko_get_models(request: Request) -> Any:
        """List all models with their class, status and which is selected per class."""
        db = _billing_db_path or _db_path()
        all_models = get_all_models(db)
        # Determine selected: first model returned by get_models_by_class for each class
        selected: set = set()
        classes_seen: set = set()
        for m in all_models:
            cls = m["class"]
            if cls not in classes_seen:
                classes_seen.add(cls)
                candidates = get_models_by_class(db, cls)
                if candidates:
                    selected.add(candidates[0])
        result = [
            {**m, "selected": m["name"] in selected}
            for m in all_models
        ]
        return {"models": result}

    @router.post(
        "/kiko/models/{model_name}/class",
        dependencies=[Depends(_local_only), Depends(user_api_key_auth)],
        tags=["kiko"],
    )
    async def kiko_set_model_class(model_name: str, request: Request) -> Any:
        """Change the class of a model (e.g. HAIKU → SONNET)."""
        body = await request.json()
        new_class = body.get("class", "")
        if not new_class:
            raise HTTPException(status_code=400, detail="'class' field required")
        db = _billing_db_path or _db_path()
        try:
            found = set_model_class(db, model_name, new_class)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not found:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")
        return {"name": model_name, "class": new_class.upper()}

    @router.post(
        "/kiko/shutdown",
        dependencies=[Depends(_local_only), Depends(user_api_key_auth)],
        tags=["kiko"],
    )
    async def kiko_shutdown(request: Request) -> Any:
        """Gracefully shut down the kiko proxy."""
        import asyncio as _asyncio
        import signal as _signal

        async def _do_shutdown() -> None:
            await _asyncio.sleep(0.2)
            _signal.raise_signal(_signal.SIGINT)

        _asyncio.create_task(_do_shutdown())
        return {"status": "shutting down"}

    app.include_router(router)
    return app


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_server() -> None:
    """Entry point for `kiko` CLI command."""
    import argparse
    import asyncio

    import uvicorn

    global _config_path

    parser = argparse.ArgumentParser(
        prog="kiko",
        description="KIKO — AI proxy server",
    )
    parser.add_argument("--config", help="Path to YAML model config")
    parser.add_argument("--port", type=int, default=4000, help="Listen port (default 4000)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default 127.0.0.1 — localhost only)",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Seed models table from --config YAML, then exit",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List models from DB (KIKO_DB_PATH or default), then exit",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Ping each enabled model and update status in DB, then exit",
    )
    # Model CRUD
    parser.add_argument(
        "--add-model", metavar="ALIAS", dest="add_model",
        help="Add or update a model (use with --provider, --api-key-env, --api-base)",
    )
    parser.add_argument(
        "--provider", metavar="PROVIDER/ID",
        help="Provider model string, e.g. gemini/gemini-2.0-flash-lite  (for --add-model)",
    )
    parser.add_argument(
        "--api-key-env", metavar="VAR", dest="api_key_env",
        help="Env var name for the API key, e.g. GEMINI_API_KEY  (for --add-model)",
    )
    parser.add_argument(
        "--api-base", metavar="URL", dest="api_base",
        help="API base URL, e.g. https://integrate.api.nvidia.com/v1  (for --add-model)",
    )
    parser.add_argument(
        "--class", dest="model_class", metavar="CLASS",
        choices=["HAIKU", "SONNET", "OPUS", "FABLE"], default="SONNET",
        help="Model class for --add-model (HAIKU/SONNET/OPUS/FABLE, default: SONNET)",
    )
    parser.add_argument(
        "--priority", type=int, default=100, metavar="N",
        help="Routing priority for --add-model (lower = preferred, default: 100)",
    )
    parser.add_argument(
        "--delete-model", metavar="ALIAS", dest="delete_model",
        help="Remove a model from the DB by alias",
    )
    parser.add_argument(
        "--enable-model", metavar="ALIAS", dest="enable_model",
        help="Re-enable a disabled model",
    )
    parser.add_argument(
        "--disable-model", metavar="ALIAS", dest="disable_model",
        help="Disable a model (keeps it in DB, hidden from server and --list-models)",
    )
    # Billing
    parser.add_argument(
        "--costs", action="store_true",
        help="Show billing cost summary from the DB",
    )
    parser.add_argument(
        "--days", type=int, default=30, metavar="N",
        help="Number of days for --costs report (default: 30)",
    )
    args = parser.parse_args()
    litellm.suppress_debug_info = True

    # MASTER_KEY: env var takes priority; fallback to Credential Manager
    if not os.environ.get("MASTER_KEY"):
        ck = _cred_get("MASTER_KEY")
        if ck:
            os.environ["MASTER_KEY"] = ck

    # ── --init-db ─────────────────────────────────────────────────────────────
    if args.init_db:
        if not args.config:
            parser.error("--init-db requires --config")
        import yaml
        with open(args.config) as fh:
            conf = yaml.safe_load(fh) or {}
        from litellm.proxy.integrations.kiko_sqlite_logger import _init_db
        db = _db_path()
        _init_db(db)
        n = seed_models(db, conf.get("model_list", []))
        print(f"kiko: seeded {n} model(s) into {db}")
        return

    # ── --list-models ─────────────────────────────────────────────────────────
    if args.list_models:
        db = _db_path()
        models = load_models(db)
        if not models:
            print(f"No enabled models in {db}")
        else:
            print(f"Models in {db}:")
            for m in models:
                p = m["litellm_params"]
                base = f"  api_base={p['api_base']}" if "api_base" in p else ""
                print(f"  {m['model_name']:30s}  {p['model']}{base}")
        return

    # ── --discover ────────────────────────────────────────────────────────────
    if args.discover:
        db = _db_path()
        models = load_models(db)
        if not models:
            print(f"No enabled models in {db} — run --init-db first")
            return
        print(f"Discovering {len(models)} model(s) ...")
        results = asyncio.run(discover_models(db, models))
        for alias, status in results.items():
            print(f"  {alias}: {status}")
        return

    # ── --add-model ───────────────────────────────────────────────────────────
    if args.add_model:
        if not args.provider or not args.api_key_env:
            parser.error("--add-model requires --provider and --api-key-env")
        db = _db_path()
        is_new = add_model(
            db, args.add_model, args.provider, args.api_key_env, args.api_base,
            model_class=args.model_class, priority=args.priority,
        )
        action = "added" if is_new else "updated"
        print(f"kiko: {action} model '{args.add_model}' in {db}")
        return

    # ── --delete-model ────────────────────────────────────────────────────────
    if args.delete_model:
        db = _db_path()
        if delete_model(db, args.delete_model):
            print(f"kiko: deleted '{args.delete_model}' from {db}")
        else:
            print(f"kiko: model '{args.delete_model}' not found in {db}")
            sys.exit(1)
        return

    # ── --enable-model ────────────────────────────────────────────────────────
    if args.enable_model:
        db = _db_path()
        if set_model_enabled(db, args.enable_model, True):
            print(f"kiko: enabled '{args.enable_model}'")
        else:
            print(f"kiko: model '{args.enable_model}' not found in {db}")
            sys.exit(1)
        return

    # ── --disable-model ───────────────────────────────────────────────────────
    if args.disable_model:
        db = _db_path()
        if set_model_enabled(db, args.disable_model, False):
            print(f"kiko: disabled '{args.disable_model}'")
        else:
            print(f"kiko: model '{args.disable_model}' not found in {db}")
            sys.exit(1)
        return

    # ── --costs ───────────────────────────────────────────────────────────────
    if args.costs:
        db = _db_path()
        rows = get_billing_summary(db, args.days)
        if not rows:
            print(f"No billing data in {db} for the last {args.days} day(s)")
            return
        col = f"{'Model':<35} {'Calls':>6} {'Tokens In':>10} {'Tokens Out':>11} {'Cost USD':>12}"
        sep = "-" * len(col)
        print(f"Cost summary — last {args.days} day(s)  [{db}]")
        print(col)
        print(sep)
        total = 0.0
        for r in rows:
            print(
                f"  {r['model']:<33} {r['calls']:>6} "
                f"{r['tokens_in']:>10} {r['tokens_out']:>11} {r['cost_usd']:>12.6f}"
            )
            total += r["cost_usd"]
        print(sep)
        print(f"  {'TOTAL':<33} {'':>6} {'':>10} {'':>11} {total:>12.6f}")
        return

    # ── serve (default) ───────────────────────────────────────────────────────
    if not args.config:
        parser.error("--config is required to start the server")
    if not os.environ.get("MASTER_KEY"):
        print(
            "ERROR: MASTER_KEY is not set. "
            "Set it via the MASTER_KEY env var or Windows Credential Manager.",
            file=sys.stderr,
        )
        sys.exit(1)
    _config_path = args.config
    app = create_app()
    print(f"KIKO running on http://{args.host}:{args.port} (Press CTRL+C to quit)", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", server_header=False)


# ── module-level app access for tests and ASGI servers ────────────────────────
# fastapi is imported lazily in create_app() to avoid heavy deps at import time.
# Accessing `kiko_server.app` creates and caches the app on first access.
_cached_app: Any = None


def __getattr__(name: str) -> Any:
    global _cached_app
    if name == "app":
        if _cached_app is None:
            _cached_app = create_app()
        return _cached_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
