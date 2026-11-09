# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""SQLite store shared with kiko-full: the SAME file (default ``~/.kiko/kiko.db``) and the same three tables.

``billing``, ``models`` and ``responses`` keep kiko-full's columns, so an existing kiko.db keeps working in both
directions. kiko-lite adds only nullable columns and new tables, guarded by ``PRAGMA user_version`` (8):

* ``models``  + ``price_in_per_mtok``, ``price_out_per_mtok``, ``price_cache_read_per_mtok``
* ``models``  + ``thinking`` (v6; NULL = send nothing, else on / off / off-prompt, see ``THINKING_MODES``)
* ``models``  + ``timeout`` (v7; seconds, NULL = the global ``[proxy] upstream_timeout``, see ``check_timeout``)
* ``models``  + ``toolshim`` (v8; NULL = off, "on" = turn tool calls written as text into real ones, see ``TOOLSHIM_MODES``)
* ``billing`` + ``attempts``, ``latency_ms``, ``upstream_model``, ``error``, ``request_id`` (v5: the id of the
  ``responses`` row that holds the text of the request, see ``add_response``)
* ``aliases(glob PK, target)`` and ``settings(key PK, value)`` (what kiko-full kept in its YAML)
* ``model_classes(name PK, keyword, role, sort)`` (v2): the model classes, seeded with the four kiko-full ones
* (v3) the planned and proposed classes (VOICE, EYES, ...) are added once to an existing DB
* ``providers(name PK, api_base, api_key_env, sort)`` (v4): the providers the web form and the CLI offer, seeded
  with the ones kiko-lite supports; a model row still stores ``<provider>/<model id>``, ``api_base``, ``api_key_env``

A model row stores only the NAME of its API key (``api_key_env``), never a key value.

Threads: every operation opens a short-lived connection (WAL, ``synchronous=NORMAL``, 5 s busy timeout) and
writes are serialised by a lock, so one ``Database`` can be shared by all server threads.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from kikolib import config as kconfig
from kikolib import posix

SCHEMA_VERSION = 8
# Per-model "thinking" switch for OpenAI-compatible upstreams (NULL in the column = default = send nothing):
# on / off use the chat_template_kwargs enable_thinking body field, off-prompt adds a "/no_think" system line.
THINKING_MODES = ("default", "on", "off", "off-prompt")
# Per-model "toolshim" (NULL in the column = off): on = a tool call a weak model wrote as text becomes a real one.
TOOLSHIM_MODES = ("off", "on")


def check_timeout(value: float | str | None) -> float | None:
    """A per-model timeout as stored: None, 0 and "default" (any case) mean "use the global one" and give None;
    otherwise seconds, finite, more than 0 and at most ``kconfig.MAX_TIMEOUT_SECONDS``. ValueError otherwise."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "default")):
        return None
    try:
        x = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a number of seconds or 'default'") from None
    if isinstance(value, bool) or not math.isfinite(x) or x < 0 or x > kconfig.MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be 0 (default) or more than 0 and at most {int(kconfig.MAX_TIMEOUT_SECONDS)} seconds")
    return None if x == 0 else x


def fmt_timeout(value: object) -> str:
    """A stored per-model timeout for display, e.g. "600"; "" when unset or a bad (hand-edited) value."""
    try:
        x = check_timeout(value)  # type: ignore[arg-type]
    except ValueError:
        return ""
    return "" if x is None else f"{x:g}"


# (name, keyword found in the lower-cased requested model name, role, sort); keyword order as in kiko-full.
_CLASSES_V2 = (
    ("OPUS", "opus", "manager and evaluator of the work", 10),
    ("SONNET", "sonnet", "craftsman (main implementation work)", 20),
    ("HAIKU", "haiku", "laborer (bulk simple work)", 30),
    ("FABLE", "fable", "external auditor", 40),
)
# Planned and proposed classes (docs/site/en/MODEL-CLASSES.md), added by schema v3. Keywords are chosen so that
# no ordinary model name used today (claude-*, gemini-*, gpt-*, deepseek-*, llama-*, openrouter/*) contains one:
# the risky words (coder, guard, router, ocr, realtime, action) carry a "kiko-" prefix.
_CLASSES_V3 = (
    ("VOICE", "voice", "text-to-speech", 50),
    ("EYES", "eyes", "understands images and screenshots", 60),
    ("EARS", "ears", "speech-to-text", 70),
    ("MEMORY", "memory", "embeddings for search and memory (also called EMBED)", 80),
    ("RERANK", "rerank", "reorders search results by relevance", 90),
    ("GUARD", "kiko-guard", "moderation and safety filter", 100),
    ("CODER", "kiko-coder", "code-specialized model", 110),
    ("ROUTER", "kiko-router",
     "tiny fast model that decides which class or handler a request needs (also called CLASSIFIER)", 120),
    ("TUTOR", "tutor", "cheap long-context reader that digests big documents (also called SCOUT)", 130),
    ("OCR", "kiko-ocr", "text and tables from scans or PDFs (also called DOC)", 140),
    ("PAINTER", "painter", "image generation", 150),
    ("VIDEO", "video", "video generation or understanding", 160),
    ("MUSIC", "music", "music and audio generation", 170),
    ("WATCH", "watch",
     "object detection on camera streams, including plates and faces (also called DETECT)", 180),
    ("SENSOR", "sensor", "models over sensor or time-series data, e.g. anomaly detection", 190),
    ("REALTIME", "kiko-realtime", "low-latency voice or chat sessions, streaming both ways", 200),
    ("ACTION", "kiko-action", "computer-use or tool-acting models", 210),
)
DEFAULT_CLASSES = _CLASSES_V2 + _CLASSES_V3
CLASSES = tuple(c[0] for c in DEFAULT_CLASSES)  # the seeded names; the model_classes table is the truth
CLASS_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
# (name, default api_base, conventional key NAME, sort): the providers kiko_lite.providers supports, with the
# base URLs it already uses (schema v4). A provider the owner deleted later is not re-added.
_PROVIDERS_V4 = (
    ("gemini", "https://generativelanguage.googleapis.com", "GEMINI_API_KEY", 10),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", 20),
    ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", 30),
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", 40),
    ("dashscope", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", 50),
    ("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", 60),
)
DEFAULT_PROVIDERS = _PROVIDERS_V4
PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")

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
    ("models", "thinking", "TEXT"),
    ("models", "timeout", "REAL"),
    ("models", "toolshim", "TEXT"),
    ("billing", "attempts", "INTEGER"),
    ("billing", "latency_ms", "INTEGER"),
    ("billing", "upstream_model", "TEXT"),
    ("billing", "error", "TEXT"),
    ("billing", "request_id", "TEXT"),
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

_V5 = """
CREATE INDEX IF NOT EXISTS billing_request_id ON billing(request_id);
CREATE INDEX IF NOT EXISTS responses_created_at ON responses(created_at);
"""

_V4 = """
CREATE TABLE IF NOT EXISTS providers (
    name        TEXT PRIMARY KEY,
    api_base    TEXT NOT NULL,
    api_key_env TEXT NOT NULL,
    sort        INTEGER NOT NULL DEFAULT 100
);
"""


def default_path(cli_value: str | os.PathLike[str] | None = None,
                 env: Mapping[str, str] | None = None) -> Path:
    """``--db`` flag, else what ``kikolib.config`` says (env ``KIKO_DB_PATH``, ``[database] path`` of kiko.toml, or
    ``kiko.db`` in the config folder). kiko-lite and the web both resolve the file here. ``env`` replaces the
    process environment (tests)."""
    if cli_value:
        return Path(cli_value)
    return kconfig.database_path(env=env)


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

    On POSIX a folder created here gets mode 0o700 and the file 0o600 (a no-op on Windows).

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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # mode: only a folder created here
        if sys.platform != "win32" and not self.path.exists():  # the file (and its WAL / SHM) is born 0o600
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        with self._wlock:
            con = self._connect()
            try:
                self._migrate(con)
            finally:
                con.close()
        posix.make_private(self.path)  # an older, world-readable file too; no-op on Windows

    # -- plumbing --
    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            con = sqlite3.connect(ro_uri(self.path.resolve()), uri=True, timeout=self._busy)
            con.row_factory = sqlite3.Row
            return con
        con = sqlite3.connect(str(self.path), timeout=self._busy)
        con.row_factory = sqlite3.Row
        end = time.monotonic() + self._busy
        while True:  # switching to WAL may fail at once with "locked" (no busy wait): retry within the timeout
            try:
                con.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or time.monotonic() >= end:
                    con.close()
                    raise
                time.sleep(0.02)
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

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run one read-only SELECT (callers pass constant SQL, values only via ``params``); rows as dicts."""
        with self._read() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

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
            have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            if not {"billing_request_id", "responses_created_at"} <= have:
                try:
                    con.executescript(_V5)  # on every open: an index dropped or lost is rebuilt
                except sqlite3.OperationalError:
                    pass  # a write-protected file must still open
            return  # already migrated and complete: no write at all
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
        con.executescript(_V4)
        con.executescript(_V5)
        for table, column, _ in _COLUMNS:  # never mark a half-migrated DB as migrated
            if column not in Database._columns(con, table):
                raise sqlite3.OperationalError(f"migration incomplete: {table}.{column} is missing")
        con.commit()
        # The seeding runs under one write lock: two processes opening the same old DB at once must not both
        # insert (the second waits for the busy timeout, re-reads user_version and finds nothing to do).
        con.execute("BEGIN IMMEDIATE")
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                con.rollback()
                return
            if version < 2:  # the four kiko-full classes, seeded once
                con.executemany("INSERT OR IGNORE INTO model_classes(name, keyword, role, sort) VALUES(?,?,?,?)",
                                _CLASSES_V2)
            if version < 3:  # the planned classes, added once; a class deleted later is not re-added
                have = [(n, k.lower()) for n, k in con.execute("SELECT name, keyword FROM model_classes")]
                top = con.execute("SELECT COALESCE(MAX(sort), 0) FROM model_classes").fetchone()[0]
                base = max(c[3] for c in _CLASSES_V2)
                for name, keyword, role, sort in _CLASSES_V3:
                    if any(n == name or k in keyword or keyword in k for n, k in have):
                        continue  # the user already has this name or an overlapping keyword: leave theirs alone
                    # after every class that exists now (the owner's own too), in the order of the table
                    con.execute("INSERT OR IGNORE INTO model_classes(name, keyword, role, sort) VALUES(?,?,?,?)",
                                (name, keyword, role, int(top) + sort - base))
                    have.append((name, keyword))
            if version < 4:  # the supported providers, seeded once; one deleted later is not re-added
                con.executemany("INSERT OR IGNORE INTO providers(name, api_base, api_key_env, sort) VALUES(?,?,?,?)",
                                _PROVIDERS_V4)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.commit()
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise

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

    # -- providers --
    def list_providers(self) -> list[dict[str, Any]]:
        """Providers (name, api_base, api_key_env, sort, models = rows using it) in display order.

        A DB without the table (older version, read-only) reads as the seeded defaults.
        """
        with self._read() as con:
            if not self._has_table(con, "providers"):
                return [{"name": n, "api_base": b, "api_key_env": k, "sort": o, "models": 0}
                        for n, b, k, o in DEFAULT_PROVIDERS]
            rows = [dict(r) for r in con.execute("SELECT name, api_base, api_key_env, sort FROM providers"
                                                 " ORDER BY sort, name")]
            used = self._provider_use(con)
        for r in rows:
            r["models"] = used.get(r["name"], 0)
        return rows

    @staticmethod
    def _provider_use(con: sqlite3.Connection) -> dict[str, int]:
        """Model rows per provider prefix (the text before the first '/', lower case)."""
        used: dict[str, int] = {}
        for (prov,) in con.execute("SELECT provider FROM models"):
            text = str(prov)
            prefix = text.partition("/")[0].strip().lower() if "/" in text else ""
            used[prefix] = used.get(prefix, 0) + 1
        return used

    def get_provider(self, name: str) -> dict[str, Any] | None:
        return next((p for p in self.list_providers() if p["name"] == name), None)

    def set_provider(self, name: str, api_base: str, api_key_env: str, sort: int | None = None) -> bool:
        """Insert or update a provider. True if new. ValueError for a bad name or an empty base / key name."""
        if not PROVIDER_NAME.match(name or ""):
            raise ValueError("provider name must be lower case letters, digits, '_' or '-' (1 to 32), "
                             "starting with a letter")
        api_base, api_key_env = (api_base or "").strip(), (api_key_env or "").strip()
        if not api_base or not api_key_env:
            raise ValueError("a provider needs an API base and an API key name")
        from .resolver import check_provider  # late: resolver imports this module
        check_provider(api_base, api_key_env)  # a key NAME (never a value) and a plain https URL
        with self._write() as con:
            existed = con.execute("SELECT 1 FROM providers WHERE name=?", (name,)).fetchone() is not None
            top = con.execute("SELECT COALESCE(MAX(sort), 0) FROM providers").fetchone()[0]
            con.execute("INSERT INTO providers(name, api_base, api_key_env, sort) VALUES(?,?,?,?)"
                        " ON CONFLICT(name) DO UPDATE SET api_base=excluded.api_base,"
                        " api_key_env=excluded.api_key_env, sort=COALESCE(?, providers.sort)",
                        (name, api_base, api_key_env, int(top) + 10 if sort is None else int(sort),
                         None if sort is None else int(sort)))
        return not existed

    def delete_provider(self, name: str) -> bool:
        """Delete a provider; ValueError while a model still uses it. False when it does not exist."""
        with self._write() as con:
            used = self._provider_use(con).get(name.lower(), 0)
            if used:
                raise ValueError(f"provider {name} is still used by {used} model(s)")
            return con.execute("DELETE FROM providers WHERE name=?", (name,)).rowcount > 0

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
                     enabled: bool | None = None, status: str | None = None,
                     thinking: str | None = None, timeout: float | str | None = None, toolshim: str | None = None) -> bool:
        """Insert or update by alias (id, status, enabled and given prices are kept on update). True if new.

        ``thinking``: one of ``THINKING_MODES``; None keeps the stored value on update ("default" clears it).
        ``timeout``: upstream timeout in seconds for this model (see ``check_timeout``); None keeps the stored value
        on update, 0 or "default" clears it (the global timeout applies again).
        ``toolshim``: "on" / "off" (see ``TOOLSHIM_MODES``); None keeps the stored value on update."""
        cls = model_class.upper()
        tmo = check_timeout(timeout)
        if thinking is not None and thinking not in THINKING_MODES:
            raise ValueError(f"thinking must be one of {', '.join(THINKING_MODES)}")
        if toolshim is not None and toolshim not in TOOLSHIM_MODES:
            raise ValueError(f"toolshim must be one of {', '.join(TOOLSHIM_MODES)}")
        with self._write() as con:
            known = [c["name"] for c in self._classes_in(con)]
            if cls not in known:
                raise ValueError(f"model class must be one of {', '.join(known)}")
            self._check_alias(con, alias)
            existed = con.execute("SELECT 1 FROM models WHERE alias=?", (alias,)).fetchone() is not None
            con.execute(
                "INSERT INTO models(alias, provider, api_base, api_key_env, model_class, priority, enabled, status,"
                " price_in_per_mtok, price_out_per_mtok, price_cache_read_per_mtok, thinking, timeout, toolshim) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(alias) DO UPDATE SET provider=excluded.provider, api_base=excluded.api_base,"
                " api_key_env=excluded.api_key_env, model_class=excluded.model_class, priority=excluded.priority,"
                " enabled=COALESCE(?, models.enabled), status=COALESCE(?, models.status),"
                " price_in_per_mtok=COALESCE(excluded.price_in_per_mtok, models.price_in_per_mtok),"
                " price_out_per_mtok=COALESCE(excluded.price_out_per_mtok, models.price_out_per_mtok),"
                " price_cache_read_per_mtok=COALESCE(excluded.price_cache_read_per_mtok,"
                " models.price_cache_read_per_mtok),"
                " thinking=CASE WHEN ? THEN models.thinking ELSE excluded.thinking END,"
                " timeout=CASE WHEN ? THEN models.timeout ELSE excluded.timeout END,"
                " toolshim=CASE WHEN ? THEN models.toolshim ELSE excluded.toolshim END",
                (alias, provider, api_base, api_key_env, cls, int(priority),
                 1 if enabled is None else int(enabled), status or "unknown", price_in, price_out, price_cache,
                 None if thinking in (None, "default") else thinking, None if timeout is None else tmo,
                 "on" if toolshim == "on" else None,
                 None if enabled is None else int(enabled), status, int(thinking is None), int(timeout is None), int(toolshim is None)),
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

    def set_thinking(self, alias: str, mode: str) -> bool:
        """Set the per-model thinking switch (one of ``THINKING_MODES``; "default" clears it). True if the row exists."""
        if mode not in THINKING_MODES:
            raise ValueError(f"thinking must be one of {', '.join(THINKING_MODES)}")
        with self._write() as con:
            return con.execute("UPDATE models SET thinking=? WHERE alias=?",
                               (None if mode == "default" else mode, alias)).rowcount > 0

    def set_toolshim(self, alias: str, mode: str) -> bool:
        """Set the per-model toolshim switch (one of ``TOOLSHIM_MODES``; "off" clears it). True if the row exists."""
        if mode not in TOOLSHIM_MODES:
            raise ValueError(f"toolshim must be one of {', '.join(TOOLSHIM_MODES)}")
        with self._write() as con:
            return con.execute("UPDATE models SET toolshim=? WHERE alias=?",
                               ("on" if mode == "on" else None, alias)).rowcount > 0

    def set_timeout(self, alias: str, seconds: float | str) -> bool:
        """Set the per-model upstream timeout (``check_timeout``; 0 or "default" clears it). True if the row exists."""
        value = check_timeout(seconds)
        with self._write() as con:
            return con.execute("UPDATE models SET timeout=? WHERE alias=?", (value, alias)).rowcount > 0

    def set_status(self, alias: str, status: str, ts: str | None = None) -> bool:
        """Record a health result (``ok`` / ``error``) and when it was checked. True if the row exists."""
        with self._write() as con:
            return con.execute("UPDATE models SET status=?, last_check=? WHERE alias=?",
                               (status, ts or now_iso(), alias)).rowcount > 0

    def delete_model(self, alias: str) -> bool:
        with self._write() as con:
            return con.execute("DELETE FROM models WHERE alias=?", (alias,)).rowcount > 0

    # -- aliases / settings --
    def delete_class(self, name: str) -> bool:
        """Delete a class; ValueError while a model still uses it. False when it does not exist."""
        with self._write() as con:
            if con.execute("SELECT 1 FROM models WHERE model_class=? LIMIT 1", (name,)).fetchone():
                raise ValueError(f"class {name} is still used by a model")
            return con.execute("DELETE FROM model_classes WHERE name=?", (name,)).rowcount > 0

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
                    upstream_model: str | None = None, error: str | None = None,
                    request_id: str | None = None) -> None:
        with self._write() as con:
            con.execute(
                "INSERT INTO billing(model, tokens_in, tokens_out, cost_usd, ts, route, attempts, latency_ms,"
                " upstream_model, error, request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (model, int(tokens_in), int(tokens_out), float(cost_usd), ts or now_iso(), route, attempts,
                 latency_ms, upstream_model, error, request_id))

    def add_response(self, request_id: str, input_json: str, output_json: str) -> None:
        """Store the text of one exchange (the Text log). ``request_id`` is also ``billing.request_id``."""
        with self._write() as con:
            con.execute("INSERT OR REPLACE INTO responses(id, input_json, output_json) VALUES(?,?,?)",
                        (request_id, input_json, output_json))

    def prune_logs(self, days: int) -> int:
        """Delete ``billing`` rows and stored ``responses`` older than ``days`` days; returns the rows deleted
        (``[logs] retention_days``). Newer rows are never touched."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).isoformat()  # billing.ts is ISO 8601
        cutoff_sql = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")  # responses.created_at: datetime('now')
        deleted = 0
        with self._write() as con:
            for table, column, bound in (("billing", "ts", cutoff), ("responses", "created_at", cutoff_sql)):
                if self._has_table(con, table):
                    deleted += con.execute(f"DELETE FROM {table} WHERE {column} < ?", (bound,)).rowcount
        return deleted

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
            live = " AND route IS NOT 'probe'" if "route" in self._columns(con, "billing") else ""
            rows = con.execute(
                "SELECT model, COUNT(*) AS calls, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out,"
                f" SUM(cost_usd) AS cost_usd, {err} AS errors"
                f" FROM billing WHERE ts >= ?{live} GROUP BY model ORDER BY cost_usd DESC", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def probe_summary(self, days: int = 30) -> tuple[int, float]:
        """(calls, cost USD) of the background health probes (billing route "probe") in the last ``days`` days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._read() as con:
            if "route" not in self._columns(con, "billing"):
                return 0, 0.0
            row = con.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0.0) FROM billing"
                              " WHERE ts >= ? AND route = 'probe'", (cutoff,)).fetchone()
        return int(row[0]), float(row[1])
