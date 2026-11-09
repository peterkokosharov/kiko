# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Read-only sources for the spy: ``LogTail`` follows the kiko log file, ``DbPoll`` reads new request rows.

Neither writes anything: the log file is only opened for reading, the database only through a SQLite
``mode=ro`` URI (plus ``PRAGMA query_only``). Both are polled; neither raises on a missing or broken file."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from kiko_lite.db import ro_uri

logger = logging.getLogger("kiko.spy")

MAX_LINE = 4000  # characters kept per line
MAX_READ = 1024 * 1024  # bytes read per poll
START_BYTES = 64 * 1024  # how far back the first poll looks for the backlog


def _clip(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= MAX_LINE else text[:MAX_LINE] + " ..."


def _clean(raw: bytes) -> str:
    return _clip(raw.decode("utf-8", "replace"))


class LogTail:
    """Follows a text file like ``tail -f``. The first poll returns the last ``backlog`` lines; later polls the
    new complete lines. A file that shrinks, is replaced (rotation) or reappears is read again from its start."""

    def __init__(self, path: Callable[[], Path | None] | Path | None, backlog: int = 100) -> None:
        self._path = path if callable(path) else (lambda: path)
        self.backlog = backlog
        self._pos = 0
        self._ident: tuple[int, int] | None = None
        self._started = False

    def poll(self) -> list[str]:
        path = self._path()
        try:
            if path is None:
                return []
            st = os.stat(path)
        except OSError:
            self._ident = None  # gone: when it comes back it is a new file, read from the start
            self._pos = 0
            return []
        ident = (st.st_dev, st.st_ino)
        try:
            with open(path, "rb") as f:
                if not self._started:
                    self._started = True
                    start = max(0, st.st_size - START_BYTES)
                    f.seek(start)
                    data = f.read(START_BYTES)
                    end = data.rfind(b"\n") + 1
                    lines = data[:end].split(b"\n")[:-1]
                    if start > 0 and lines:
                        lines = lines[1:]  # the first line is cut in the middle
                    self._pos, self._ident = start + end, ident
                    return [_clean(x) for x in lines[-self.backlog:]] if self.backlog > 0 else []
                if ident != self._ident or st.st_size < self._pos:
                    self._pos = 0  # rotated or truncated
                self._ident = ident
                if st.st_size == self._pos:
                    return []
                f.seek(self._pos)
                data = f.read(MAX_READ)
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        if end == 0:
            if len(data) < MAX_READ:
                return []  # a line still being written: wait for its newline
            end = len(data)  # one giant line: do not stall
        self._pos += end
        return [_clean(x) for x in data[:end].split(b"\n") if x.strip()]


_WANTED = ("id", "ts", "model", "upstream_model", "route", "tokens_in", "tokens_out", "cost_usd", "latency_ms",
           "attempts", "error", "request_id")


def format_row(row: dict[str, object]) -> str:
    """One request row as one line of text."""
    model = str(row.get("model") or "-")
    up = row.get("upstream_model")
    if up and up != row.get("model"):
        model += f" -> {up}"
    parts = [str(row.get("ts") or "-"), model, f"in={row.get('tokens_in') or 0} out={row.get('tokens_out') or 0}"]
    try:
        parts.append(f"${float(row.get('cost_usd') or 0):.4f}")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    if row.get("latency_ms") is not None:
        parts.append(f"{row['latency_ms']}ms")
    if row.get("attempts") and row["attempts"] != 1:
        parts.append(f"attempts={row['attempts']}")
    if row.get("route"):
        parts.append(f"route={row['route']}")
    if row.get("request_id"):
        parts.append(f"id={row['request_id']}")
    if row.get("error"):
        parts.append("ERROR " + str(row["error"]))
    return _clip(" ".join(parts).replace("\r", " ").replace("\n", " "))


class DbPoll:
    """New rows of the ``billing`` table (one row per request), read through a read-only connection that is
    opened and closed per poll. The first poll returns the last ``backlog`` rows."""

    def __init__(self, path: Callable[[], Path] | Path, backlog: int = 50) -> None:
        self._path = path if callable(path) else (lambda: path)
        self.backlog = backlog
        self._last: int | None = None

    def poll(self) -> list[str]:
        path = self._path()
        if not path.is_file():
            return []
        con = None
        try:
            con = sqlite3.connect(ro_uri(path.resolve()), uri=True, timeout=2.0)
            con.execute("PRAGMA query_only = ON")
            have = {r[1] for r in con.execute("PRAGMA table_info(billing)")}
            cols = [c for c in _WANTED if c in have]
            if "id" not in have:
                return []
            top = con.execute("SELECT MAX(id) FROM billing").fetchone()[0] or 0
            sel = "SELECT " + ", ".join(cols) + " FROM billing"
            if self._last is None:
                rows = con.execute(sel + " ORDER BY id DESC LIMIT ?", (self.backlog,)).fetchall()[::-1]
                self._last = top
            elif top < self._last:  # rows were pruned or the file replaced: start over from here
                self._last = top
                return []
            else:
                rows = con.execute(sel + " WHERE id > ? ORDER BY id LIMIT 500", (self._last,)).fetchall()
                if rows:
                    self._last = rows[-1][cols.index("id")]
            return [format_row(dict(zip(cols, r))) for r in rows]
        except sqlite3.Error as e:
            logger.debug("spy: db poll failed: %s", type(e).__name__)
            return []
        finally:
            if con is not None:
                con.close()
