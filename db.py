# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""SQLite store shared with kiko-full: the SAME file (default ``~/.kiko/kiko.db``) and the same three tables.

``billing``, ``models`` and ``responses`` keep kiko-full's columns, so an existing kiko.db keeps working in both
directions. kiko-lite adds only nullable columns and new tables, guarded by ``PRAGMA user_version`` (2):

* ``models``  + ``price_in_per_mtok``, ``price_out_per_mtok``, ``price_cache_read_per_mtok``
* ``billing`` + ``attempts``, ``latency_ms``, ``upstream_model``, ``error``
* ``aliases(glob PK, target)`` and ``settings(key PK, value)`` (what kiko-full kept in its YAML)
* ``model_classes(name PK, keyword, role, sort)`` (v2): the model classes, seeded with the four kiko-full ones

A model row stores only the NAME of its API key (``api_key_env``), never a key value.

Threads: every operation opens a short-lived connection (WAL, ``synchronous=NORMAL``, 5 s busy timeout) and
writes are serialised by a lock, so one ``Database`` can be shared by all server threads.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 2
# (name, keyword found in the lower-cased requested model name, role, sort); keyword order as in kiko-full.
DEFAULT_CLASSES = (
    ("OPUS", "opus", "manager and evaluator of the work", 10),
    ("SONNET", "sonnet", "craftsman (main implementation work)", 20),
    ("HAIKU", "haiku", "laborer (bulk simple work)", 30),
    ("FABLE", "fable", "external auditor", 40),
)
CLASSES = tuple(c[0] for c in DEFAULT_CLASSES)  # the seeded names; the model_classes table is the truth
CLASS_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")

# Copied from kiko-full litellm/proxy/integrations/kiko_sqlite_logger.py (_SCHEMA); keep identical.
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

# (table, column, definition) added when missing; the first three are kiko-full's own old ad-hoc migrations.
_COLUMNS = (
    ("billing", "route", "TEXT"),
    ("models", "model_class", "TEXT NOT NULL DEFAULT 'SONNET'"),
    ("models", "priority", "INTEGER NOT NULL DEFAULT 100"),
    ("models", "price_in_per_mtok", "REAL"),
    ("models", "price_out_per_mtok", "REAL"),
    ("models", "price_cache_read_per_mtok", "REAL"),
    ("billing", "attempts", "INTEGER"),
    ("billing", "latency_ms", "INTEGER"),
    ("billing", "upstream_model", "TEXT"),
    ("billing", "error", "TEXT"),
)

_V1 = """
CREATE TABLE IF NOT EXISTS aliases (glob TEXT PRIMARY KEY, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS billing_ts ON billing(ts);
"""

_V2 = """
CREATE TABLE IF NOT EXISTS model_classes (
    name    TEXT PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    role    TEXT NOT NULL DEFAULT '',
    sort    INTEGER NOT NULL DEFAULT 100
);
"""


def default_path(cli_value: str | os.PathLike[str] | None = None,
                 env: Mapping[str, str] | None = None) -> Path:
    """``--db`` flag, else env ``KIKO_DB_PATH``, else ``~/.kiko/kiko.db`` (same rule as kiko-full)."""
    if cli_value:
        return Path(cli_value)
    env = os.environ if env is None else env
    if env.get("KIKO_DB_PATH"):
        return Path(env["KIKO_DB_PATH"])
    return Path.home() / ".kiko" / "kiko.db"


BUSY_TIMEOUT = 5.0  # seconds a connection waits for a lock (tests shorten it)


def ro_uri(path: str | os.PathLike[str]) -> str:
    """SQLite ``mode=ro`` URI for an absolute path (Windows drive, UNC and POSIX; ``?``, ``#``, ``%`` encoded).

    ``C:\\a b\\k.db`` -> ``file:///C:/a%20b/k.db?mode=ro``; ``\\\\server\\share\\k.db`` ->
    ``file:////server/share/k.db?mode=ro``; ``/tmp/k.db`` -> ``file:///tmp/k.db?mode=ro``.
    """
    posix = str(path).replace("\\", "/")
    if posix.startswith("//?/"):  # Windows long-path prefix
        posix = posix[4:]
        if posix[:4].upper() == "UNC/":
            posix = "//" + posix[4:]
    quoted = quote(posix, safe="/:")
    if posix.startswith("//"):  # UNC: empty authority, then the //server/share path
        return "file:" + "//" + quoted + "?mode=ro"
    if len(posix) > 1 and posix[1] == ":":  # drive letter
        return "file:///" + quoted + "?mode=ro"
    return "file://" + quoted + "?mode=ro"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Open (and migrate) one kiko.db. Creating the object creates the file and parent folder.

    ``readonly=True`` opens an EXISTING file read-only (SQLite ``mode=ro``): no folder, no migration, no
    write; a kiko-full DB that was never migrated reads as ``schema_version() == 0``. Used by the commands
    that only look (doctor, list-models, costs).
    """

    def __init__(self, path: str | os.PathLike[str], readonly: bool = False,
                 busy_timeout: float | None = None) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._busy = BUSY_TIMEOUT if busy_timeout is None else busy_timeout
        self._wlock = threading.Lock()
        if readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._wlock:
            con = self._connect()
            try:
                self._migrate(con)
            finally:
                con.close()

    # -- plumbing --
    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            con = sqlite3.connect(ro_uri(self.path.resolve()), uri=True, timeout=self._busy)
            con.row_factory = sqlite3.Row
            return con
        con = sqlite3.connect(str(self.path), timeout=self._busy)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._wlock:
            con = self._connect()
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return  # already migrated: no write at all (a write-protected file must still open)
        con.executescript(_SCHEMA)
        for table, column, definition in _COLUMNS:
            if column not in Database._columns(con, table):
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():  # locked, read-only, disk full ...
                        raise
        con.executescript(_V1)
        con.executescript(_V2)
        con.executemany("INSERT OR IGNORE INTO model_classes(name, keyword, role, sort) VALUES(?,?,?,?)",
                        DEFAULT_CLASSES)
        for table, column, _ in _COLUMNS:  # never mark a half-migrated DB as migrated
            if column not in Database._columns(con, table):
                raise sqlite3.OperationalError(f"migration incomplete: {table}.{column} is missing")
        if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()

    def schema_version(self) -> int:
        with self._read() as con:
            return int(con.execute("PRAGMA user_version").fetchone()[0])

    # -- model classes --
    def classes(self) -> list[dict[str, Any]]:
        """Classes (name, keyword, role, sort, models = enabled models of the class) in detection order.

        A DB without the table (v1, read-only) reads as the four defaults.
        """
        with self._read() as con:
            return self._classes_in(con)

    def _classes_in(self, con: sqlite3.Connection) -> list[dict[str, Any]]:
        if not self._has_table(con, "model_classes"):
            counts = dict(con.execute("SELECT model_class, COUNT(*) FROM models WHERE enabled=1"
                                      " GROUP BY model_class").fetchall()) if self._has_table(con, "models") else {}
            return [{"name": n, "keyword": k, "role": r, "sort": o, "models": counts.get(n, 0)}
                    for n, k, r, o in DEFAULT_CLASSES]
        rows = con.execute(
            "SELECT c.name, c.keyword, c.role, c.sort,"
            " (SELECT COUNT(*) FROM models m WHERE m.model_class=c.name AND m.enabled=1) AS models"
            " FROM model_classes c ORDER BY c.sort, c.name").fetchall()
        return [dict(r) for r in rows]

    def class_names(self) -> list[str]:
        return [c["name"] for c in self.classes()]

    def add_class(self, name: str, keyword: str, role: str = "") -> None:
        """Add a class; ValueError for a bad name/keyword or one that already exists."""
        if not CLASS_NAME.match(name or ""):
            raise ValueError("class name must match ^[A-Z][A-Z0-9_]{0,31}$")
        if len(keyword or "") < 3 or keyword != keyword.lower() or keyword != keyword.strip() or len(keyword) > 64:
            raise ValueError("keyword must be lower-case text of 3 to 64 characters")
        with self._write() as con:
            for n, k in con.execute("SELECT name, keyword FROM model_classes"):
                if n == name:
                    raise ValueError(f"class {name} already exists")
                if k == keyword:
                    raise ValueError(f"keyword '{keyword}' is already used by class {n}")
                if k.lower() in keyword or keyword in k.lower():  # detection is a first-substring-match
                    raise ValueError(f"keyword '{keyword}' overlaps keyword '{k}' of class {n} "
                                     "(one contains the other, so one class would hide the other)")
            top = con.execute("SELECT COALESCE(MAX(sort), 0) FROM model_classes").fetchone()[0]
            con.execute("INSERT INTO model_classes(name, keyword, role, sort) VALUES(?,?,?,?)",
                        (name, keyword, role or "", int(top) + 10))

    # -- models --
    def models(self) -> list[dict[str, Any]]:
        with self._read() as con:
            rows = con.execute("SELECT * FROM models ORDER BY model_class, priority, id").fetchall()
        return [dict(r) for r in rows]

    def get_model(self, alias: str) -> dict[str, Any] | None:
        with self._read() as con:
            row = con.execute("SELECT * FROM models WHERE alias=?", (alias,)).fetchone()
        return dict(row) if row else None

    def upsert_model(self, alias: str, provider: str, api_key_env: str, api_base: str | None = None,
                     model_class: str = "SONNET", priority: int = 100, price_in: float | None = None,
                     price_out: float | None = None, price_cache: float | None = None,
                     enabled: bool | None = None, status: str | None = None) -> bool:
        """Insert or update by alias (id, status, enabled and given prices are kept on update). True if new."""
        cls = model_class.upper()
        with self._write() as con:
            known = [c["name"] for c in self._classes_in(con)]
            if cls not in known:
                raise ValueError(f"model class must be one of {', '.join(known)}")
            self._check_alias(con, alias)
            existed = con.execute("SELECT 1 FROM models WHERE alias=?", (alias,)).fetchone() is not None
            con.execute(
                "INSERT INTO models(alias, provider, api_base, api_key_env, model_class, priority, enabled, status,"
                " price_in_per_mtok, price_out_per_mtok, price_cache_read_per_mtok) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(alias) DO UPDATE SET provider=excluded.provider, api_base=excluded.api_base,"
                " api_key_env=excluded.api_key_env, model_class=excluded.model_class, priority=excluded.priority,"
                " enabled=COALESCE(?, models.enabled), status=COALESCE(?, models.status),"
                " price_in_per_mtok=COALESCE(excluded.price_in_per_mtok, models.price_in_per_mtok),"
                " price_out_per_mtok=COALESCE(excluded.price_out_per_mtok, models.price_out_per_mtok),"
                " price_cache_read_per_mtok=COALESCE(excluded.price_cache_read_per_mtok,"
                " models.price_cache_read_per_mtok)",
                (alias, provider, api_base, api_key_env, cls, int(priority),
                 1 if enabled is None else int(enabled), status or "unknown", price_in, price_out, price_cache,
                 None if enabled is None else int(enabled), status),
            )
        return not existed

    @staticmethod
    def _check_alias(con: sqlite3.Connection, alias: str) -> None:
        norm = alias.strip().lower()
        for (other,) in con.execute("SELECT alias FROM models WHERE alias<>?", (alias,)):
            if other.strip().lower() == norm:  # provider table names are case-insensitive
                raise ValueError(f"alias '{alias}' differs only in case from existing '{other}'")

    def check_alias(self, alias: str) -> None:
        """Raise ValueError if ``alias`` would collide (in case) with another row."""
        with self._read() as con:
            self._check_alias(con, alias)

    def next_model_id(self, alias: str) -> int:
        """The id the row for ``alias`` has, or will get when it is inserted next (AUTOINCREMENT)."""
        with self._read() as con:
            row = con.execute("SELECT id FROM models WHERE alias=?", (alias,)).fetchone()
            if row:
                return int(row[0])
            return int(con.execute(
                "SELECT MAX(COALESCE((SELECT seq FROM sqlite_sequence WHERE name='models'), 0),"
                " COALESCE((SELECT MAX(id) FROM models), 0)) + 1").fetchone()[0])

    def key_names_in_use(self, except_alias: str) -> set[str]:
        with self._read() as con:
            return {r[0] for r in con.execute("SELECT api_key_env FROM models WHERE alias<>?", (except_alias,))}

    def checkpoint(self) -> bool:
        """Fold the WAL into the DB file and truncate it. False if a reader kept it from finishing (busy)."""
        con = self._connect()
        try:
            row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        return not (row and row[0])

    def set_key_name(self, alias: str, name: str) -> tuple[bool, bool]:
        """Replace a row's ``api_key_env``. Returns (row found, old text wiped from the file).

        Uses ``secure_delete`` and a WAL checkpoint; wiped is False when the checkpoint was blocked by a reader
        (the old value may then stay in the file/WAL until the last reader is gone).
        """
        with self._write() as con:
            con.execute("PRAGMA secure_delete=ON")
            found = con.execute("UPDATE models SET api_key_env=? WHERE alias=?", (name, alias)).rowcount > 0
        return found, self.checkpoint()

    def set_enabled(self, alias: str, enabled: bool) -> bool:
        with self._write() as con:
            return con.execute("UPDATE models SET enabled=? WHERE alias=?", (int(enabled), alias)).rowcount > 0

    def set_status(self, alias: str, status: str, ts: str | None = None) -> bool:
        """Record a health result (``ok`` / ``error``) and when it was checked. True if the row exists."""
        with self._write() as con:
            return con.execute("UPDATE models SET status=?, last_check=? WHERE alias=?",
                               (status, ts or now_iso(), alias)).rowcount > 0

    def delete_model(self, alias: str) -> bool:
        with self._write() as con:
            return con.execute("DELETE FROM models WHERE alias=?", (alias,)).rowcount > 0

    def delete_class(self, name: str) -> bool:
        with self._write() as con:
            return con.execute("DELETE FROM model_classes WHERE name=?", (name,)).rowcount > 0

    # -- aliases / settings --
    def _has_table(self, con: sqlite3.Connection, name: str) -> bool:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def aliases(self) -> list[tuple[str, str]]:
        with self._read() as con:
            if self.readonly and not self._has_table(con, "aliases"):
                return []
            return [(r[0], r[1]) for r in con.execute("SELECT glob, target FROM aliases ORDER BY rowid")]

    def set_alias(self, glob: str, target: str) -> None:
        with self._write() as con:
            con.execute("INSERT INTO aliases(glob, target) VALUES(?,?)"
                        " ON CONFLICT(glob) DO UPDATE SET target=excluded.target", (glob, target))

    def delete_alias(self, glob: str) -> bool:
        with self._write() as con:
            return con.execute("DELETE FROM aliases WHERE glob=?", (glob,)).rowcount > 0

    def settings(self) -> dict[str, str]:
        with self._read() as con:
            if self.readonly and not self._has_table(con, "settings"):
                return {}
            return {r[0]: r[1] for r in con.execute("SELECT key, value FROM settings ORDER BY key")}

    def set_setting(self, key: str, value: str) -> None:
        with self._write() as con:
            con.execute("INSERT INTO settings(key, value) VALUES(?,?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def delete_setting(self, key: str) -> bool:
        with self._write() as con:
            return con.execute("DELETE FROM settings WHERE key=?", (key,)).rowcount > 0

    # -- billing --
    def add_billing(self, model: str, tokens_in: int, tokens_out: int, cost_usd: float, ts: str | None = None,
                    route: str | None = "messages", attempts: int | None = None, latency_ms: int | None = None,
                    upstream_model: str | None = None, error: str | None = None) -> None:
        with self._write() as con:
            con.execute(
                "INSERT INTO billing(model, tokens_in, tokens_out, cost_usd, ts, route, attempts, latency_ms,"
                " upstream_model, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (model, int(tokens_in), int(tokens_out), float(cost_usd), ts or now_iso(), route, attempts,
                 latency_ms, upstream_model, error))

    def month_spent(self) -> float:
        """SUM(cost_usd) of the current (UTC) calendar month, the same query as kiko-full's cap check."""
        with self._read() as con:
            row = con.execute("SELECT COALESCE(SUM(cost_usd), 0.0) FROM billing"
                              " WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')").fetchone()
        return float(row[0])

    def billing_summary(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._read() as con:
            err = "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)" if "error" in self._columns(con, "billing") \
                else "0"
            rows = con.execute(
                "SELECT model, COUNT(*) AS calls, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out,"
                f" SUM(cost_usd) AS cost_usd, {err} AS errors"
                " FROM billing WHERE ts >= ? GROUP BY model ORDER BY cost_usd DESC", (cutoff,)).fetchall()
        return [dict(r) for r in rows]
