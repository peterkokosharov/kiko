# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
KikoSQLiteLogger — production version.
Writes one billing row per successful LLM call to ~/.kircho/kircho.db.
Thread-safe via threading.Lock (WAL mode for concurrent readers).

Also provides: models table schema + seed_models / load_models / update_model_status helpers.
"""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

_DEFAULT_DB = Path.home() / ".kiko" / "kiko.db"
_lock = threading.Lock()
_models_cache: dict = {}  # key: (db_path_str, class_name) -> (expiry_time, list)

_CACHE_TTL = 5.0  # seconds; model list refreshes at most every 5 s


def _open_db(path: str, timeout: float = 5.0) -> "sqlite3.Connection":
    con = sqlite3.connect(path, timeout=timeout)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


_SCHEMA = """
CREATE TABLE IF NOT EXISTS billing (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL    NOT NULL DEFAULT 0.0,
    ts         TEXT    NOT NULL,
    route      TEXT
);
CREATE TABLE IF NOT EXISTS models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alias       TEXT UNIQUE NOT NULL,
    provider    TEXT NOT NULL,
    api_base    TEXT,
    api_key_env TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'unknown',
    last_check  TEXT,
    model_class TEXT NOT NULL DEFAULT 'SONNET',
    priority    INTEGER NOT NULL DEFAULT 100
);
CREATE TABLE IF NOT EXISTS responses (
    id          TEXT PRIMARY KEY,
    input_json  TEXT NOT NULL,
    output_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _open_db(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    # Migration: add route column to existing DBs that predate this schema.
    try:
        con.execute("ALTER TABLE billing ADD COLUMN route TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    _ensure_schema(con)
    con.close()


def _ensure_schema(db: sqlite3.Connection) -> None:
    """Run safe column migrations — idempotent, call before any SELECT on models."""
    for stmt in [
        "ALTER TABLE models ADD COLUMN model_class TEXT NOT NULL DEFAULT 'SONNET'",
        "ALTER TABLE models ADD COLUMN priority INTEGER NOT NULL DEFAULT 100",
    ]:
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    # Ensure responses table exists (safe for existing DBs created before this table).
    db.execute("""CREATE TABLE IF NOT EXISTS responses (
        id          TEXT PRIMARY KEY,
        input_json  TEXT NOT NULL,
        output_json TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    db.commit()


def seed_models(db_path: Path, model_list: list) -> int:
    """Insert or replace models from YAML model_list format or flat dict. Returns inserted count."""
    con = _open_db(str(db_path))
    count = 0
    for entry in model_list:
        alias = entry.get("model_name", "")
        if not alias:
            continue
        params = entry.get("litellm_params", {})
        provider = params.get("model", "") or entry.get("provider", "")
        api_base = params.get("api_base") or entry.get("api_base")
        api_key_ref = str(params.get("api_key", "")) or str(entry.get("api_key_env", ""))
        if api_key_ref.startswith("os.environ/"):
            api_key_env = api_key_ref[len("os.environ/"):]
        else:
            api_key_env = api_key_ref
        model_class = entry.get("model_class", "SONNET")
        priority = int(entry.get("priority", 100))
        status = entry.get("status", "unknown")
        enabled = int(entry.get("enabled", 1))
        con.execute(
            "INSERT OR REPLACE INTO models"
            "(alias, provider, api_base, api_key_env, model_class, priority, status, enabled) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (alias, provider, api_base, api_key_env, model_class, priority, status, enabled),
        )
        count += 1
    con.commit()
    con.close()
    return count


def load_models(db_path: Path) -> list:
    """Return enabled non-error models in YAML model_list format (for _model_list compat)."""
    if not db_path.exists():
        return []
    con = _open_db(str(db_path))
    _ensure_schema(con)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT alias, provider, api_base, api_key_env, model_class, priority FROM models "
        "WHERE enabled=1 AND status != 'error' ORDER BY priority, id"
    ).fetchall()
    con.close()
    result: list = []
    for row in rows:
        params: dict = {
            "model": row["provider"],
            "api_key": f"os.environ/{row['api_key_env']}",
        }
        if row["api_base"]:
            params["api_base"] = row["api_base"]
        result.append({
            "model_name": row["alias"],
            "litellm_params": params,
            "model_class": row["model_class"],
            "priority": row["priority"],
        })
    return result


def get_all_enabled_aliases(db_path: Path) -> list:
    """All enabled models regardless of status — for health checking.

    Returns entries in YAML model_list format so they can be passed directly
    to discover_models, but includes models with status='error' that load_models
    would otherwise skip.
    """
    if not db_path.exists():
        return []
    with _open_db(str(db_path)) as con:
        _ensure_schema(con)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT alias, provider, api_base, api_key_env FROM models WHERE enabled=1"
        ).fetchall()
    result: list = []
    for row in rows:
        params: dict = {
            "model": row["provider"] or "",
            "api_key": f"os.environ/{row['api_key_env']}",
        }
        if row["api_base"]:
            params["api_base"] = row["api_base"]
        result.append({"model_name": row["alias"], "litellm_params": params})
    return result


def count_all_models(db_path: Path) -> int:
    """Return the total number of models (enabled + disabled) in the DB, or 0 if DB is missing."""
    if not db_path.exists():
        return 0
    con = _open_db(str(db_path))
    total = con.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    con.close()
    return total


def get_models_by_class(db_path: Path, class_name: str) -> list[str]:
    """Return aliases for class_name, ok-first then unknown fallback, from cache."""
    import time as _time
    cls = class_name.upper()
    cache_key = (str(db_path), cls)
    entry = _models_cache.get(cache_key)
    if entry is not None:
        expiry, cached = entry
        if _time.monotonic() < expiry:
            return list(cached)

    if not db_path.exists():
        return []
    con = _open_db(str(db_path))
    try:
        rows = con.execute(
            "SELECT alias FROM models "
            "WHERE model_class=? AND enabled=1 AND status='ok' "
            "ORDER BY priority, id",
            (cls,),
        ).fetchall()
        result = [r[0] for r in rows]
        if not result:
            rows = con.execute(
                "SELECT alias FROM models "
                "WHERE model_class=? AND enabled=1 AND status='unknown' "
                "ORDER BY priority, id",
                (cls,),
            ).fetchall()
            result = [r[0] for r in rows]
    finally:
        con.close()

    _models_cache[cache_key] = (_time.monotonic() + _CACHE_TTL, result)
    return list(result)


def update_model_status(db_path: Path, alias: str, status: str) -> None:
    """Set status and last_check UTC timestamp for a model alias."""
    now = datetime.now(timezone.utc).isoformat()
    con = _open_db(str(db_path))
    con.execute(
        "UPDATE models SET status=?, last_check=? WHERE alias=?",
        (status, now, alias),
    )
    con.commit()
    _models_cache.clear()
    con.close()


def add_model(
    db_path: Path,
    alias: str,
    provider: str,
    api_key_env: str,
    api_base: str | None = None,
    model_class: str = "SONNET",
    priority: int = 100,
) -> bool:
    """Add or replace a model entry. Returns True if new, False if alias already existed (updated)."""
    _init_db(db_path)
    con = _open_db(str(db_path))
    existed = con.execute("SELECT 1 FROM models WHERE alias=?", (alias,)).fetchone() is not None
    con.execute(
        "INSERT OR REPLACE INTO models(alias, provider, api_base, api_key_env, model_class, priority) "
        "VALUES(?,?,?,?,?,?)",
        (alias, provider, api_base, api_key_env, model_class.upper(), priority),
    )
    con.commit()
    _models_cache.clear()
    con.close()
    return not existed


def delete_model(db_path: Path, alias: str) -> bool:
    """Delete a model by alias. Returns True if found and deleted, False if not found."""
    if not db_path.exists():
        return False
    con = _open_db(str(db_path))
    cur = con.execute("DELETE FROM models WHERE alias=?", (alias,))
    con.commit()
    _models_cache.clear()
    con.close()
    return cur.rowcount > 0


def set_model_enabled(db_path: Path, alias: str, enabled: bool) -> bool:
    """Enable or disable a model. Returns True if alias was found."""
    if not db_path.exists():
        return False
    con = _open_db(str(db_path))
    cur = con.execute(
        "UPDATE models SET enabled=? WHERE alias=?",
        (1 if enabled else 0, alias),
    )
    con.commit()
    _models_cache.clear()
    con.close()
    return cur.rowcount > 0


_VALID_CLASSES = {"OPUS", "SONNET", "HAIKU", "FABLE"}


def get_all_models(db_path: Path) -> list:
    """Return all models sorted by class then priority."""
    if not db_path.exists():
        return []
    con = _open_db(str(db_path))
    try:
        rows = con.execute(
            "SELECT alias, model_class, status, enabled, priority FROM models "
            "ORDER BY model_class, priority, id"
        ).fetchall()
        return [
            {"name": r[0], "class": r[1], "status": r[2], "enabled": bool(r[3]), "priority": r[4]}
            for r in rows
        ]
    finally:
        con.close()


def set_model_class(db_path: Path, model_name: str, new_class: str) -> bool:
    """Change the class of a model. Returns True if the model was found, False otherwise."""
    new_class = new_class.upper()
    if new_class not in _VALID_CLASSES:
        raise ValueError(f"Invalid class '{new_class}'; must be one of {sorted(_VALID_CLASSES)}")
    con = _open_db(str(db_path))
    try:
        cur = con.execute(
            "UPDATE models SET model_class=? WHERE alias=?",
            (new_class, model_name),
        )
        con.commit()
        _models_cache.clear()
        return cur.rowcount > 0
    finally:
        con.close()


def get_billing_summary(db_path: Path, days: int = 30) -> list:
    """Return per-model cost summary for the last N days, ordered by cost descending."""
    if not db_path.exists():
        return []
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = _open_db(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT model,
               COUNT(*)         AS calls,
               SUM(tokens_in)   AS tokens_in,
               SUM(tokens_out)  AS tokens_out,
               SUM(cost_usd)    AS cost_usd
        FROM billing
        WHERE ts >= ?
        GROUP BY model
        ORDER BY cost_usd DESC
        """,
        (cutoff,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def store_response(db_path: Path, response_id: str, input_items, output_items) -> None:
    """Persist a /v1/responses turn so it can be replayed via previous_response_id."""
    import json
    # Normalize to list so downstream chaining never sees a bare string.
    if isinstance(input_items, str):
        input_items = [{"role": "user", "content": input_items}]
    if isinstance(output_items, str):
        output_items = [{"role": "assistant", "content": output_items}]
    con = _open_db(str(db_path))
    _ensure_schema(con)
    con.execute(
        "INSERT OR REPLACE INTO responses (id, input_json, output_json) VALUES (?,?,?)",
        (response_id, json.dumps(input_items), json.dumps(output_items)),
    )
    con.commit()
    con.close()


def load_response(db_path: Path, response_id: str):
    """Return (input_items, output_items) for a stored response, or None if not found."""
    import json
    try:
        if not db_path.exists():
            return None
        con = _open_db(str(db_path))
        row = con.execute(
            "SELECT input_json, output_json FROM responses WHERE id=?",
            (response_id,),
        ).fetchone()
        con.close()
        if row is None:
            return None
        return json.loads(row[0]), json.loads(row[1])
    except Exception:
        return None


class KikoSQLiteLogger(CustomLogger):
    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            import os
            env_path = os.environ.get("KIKO_DB_PATH")
            db_path = Path(env_path) if env_path else _DEFAULT_DB
        self.db_path = db_path
        _init_db(db_path)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        if metadata.get("kiko_health_check"):
            return
        model      = kwargs.get("model", "unknown")
        cost       = float(kwargs.get("response_cost") or 0.0)
        usage      = getattr(response_obj, "usage", None)
        tokens_in  = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        # metadata is stored by litellm under litellm_params["metadata"],
        # not at the top-level kwargs["metadata"].
        route      = (
            (kwargs.get("litellm_params") or {}).get("metadata") or {}
        ).get("route")
        with _lock:
            con = _open_db(str(self.db_path), timeout=5)
            con.execute(
                "INSERT INTO billing(model,tokens_in,tokens_out,cost_usd,ts,route) VALUES(?,?,?,?,?,?)",
                (model, tokens_in, tokens_out, cost,
                 start_time.isoformat() if hasattr(start_time, "isoformat")
                 else datetime.now(timezone.utc).isoformat(),
                 route),
            )
            con.commit()
            con.close()

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        pass  # failures are not billed


instance = KikoSQLiteLogger()
