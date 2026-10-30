# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""Tiny .env reader (replaces python-dotenv).

Lines: KEY=VALUE with optional ``export `` prefix, blank lines, ``#`` comments (full-line,
or inline after an unquoted value preceded by whitespace). Single quotes are literal;
double quotes honour \\n \\t \\" \\\\. No interpolation. Invalid lines are skipped, never
raise. Values are never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_ESC = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _value(rest: str) -> str | None:
    rest = rest.strip()
    if not rest:
        return ""
    q = rest[0]
    if q == "'":
        end = rest.find("'", 1)
        if end < 0:
            return None
        tail = rest[end + 1:].strip()
        return rest[1:end] if not tail or tail.startswith("#") else None
    if q == '"':
        out = []
        i = 1
        while i < len(rest):
            c = rest[i]
            if c == "\\" and i + 1 < len(rest):
                n = rest[i + 1]
                out.append(_ESC.get(n, "\\" + n))
                i += 2
            elif c == '"':
                tail = rest[i + 1:].strip()
                return "".join(out) if not tail or tail.startswith("#") else None
            else:
                out.append(c)
                i += 1
        return None
    m = re.search(r"\s#", rest)
    return (rest[: m.start()] if m else rest).rstrip()


def parse(text: str) -> dict[str, str]:
    """Parse .env text into a dict. Bad lines are skipped."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("export ", "export\t")):
            line = line[7:].lstrip()
        key, sep, rest = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            continue
        val = _value(rest)
        if val is not None:
            result[key] = val
    return result


def load(path: str | os.PathLike[str], override: bool = False) -> dict[str, str]:
    """Read ``path`` and set os.environ (existing vars win unless ``override``).

    Returns the parsed dict (all entries). A missing/unreadable file gives {}.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    data = parse(text)
    for k, v in data.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return data
