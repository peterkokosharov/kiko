# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""A small TOML writer for ``kiko.toml``: update, insert or remove a key in a ``[section]`` and keep everything
else (comments, blank lines, order, line endings). Reading stays with ``tomllib``.

Line based on purpose: it never re-serialises the file. It refuses (``TomlWriteError``) instead of guessing when
it meets something it cannot edit safely: a value it cannot represent (not str / int / float / bool / a list of
those, NaN, control characters), a bad key or section name, a file that does not parse, a key whose old value
spans several lines, or a result that does not read back (via ``tomllib``) with the value that was asked for.
The file is replaced atomically (temp file in the same folder, fsync, ``os.replace``), so a crash never leaves a
half-written kiko.toml.
"""

from __future__ import annotations

import math
import os
import re
import stat
import tempfile
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_BARE = re.compile(r"[A-Za-z0-9_-]+\Z")
_HEADER = re.compile(r"^\s*\[(?!\[)\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_ARRAY_HEADER = re.compile(r"^\s*\[\[")
LINE_SEPARATORS = chr(0x85) + chr(0x2028) + chr(0x2029)  # refused: splitlines() would break on them
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}

Value = str | int | float | bool | list


class TomlWriteError(ValueError):
    """The change cannot be written safely; the file was not touched."""


def format_value(value: Any) -> str:
    """TOML text of ``value``; ``TomlWriteError`` for anything that cannot be represented."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TomlWriteError("NaN and infinity cannot be written")
        return repr(value)
    if isinstance(value, str):
        out = []
        for c in value:
            if c in _ESCAPES:
                out.append(_ESCAPES[c])
            elif ord(c) < 32 or ord(c) == 127 or c in LINE_SEPARATORS:
                raise TomlWriteError("control characters cannot be written")
            elif 0xD800 <= ord(c) <= 0xDFFF:
                raise TomlWriteError("unpaired surrogate cannot be written")
            else:
                out.append(c)
        return '"' + "".join(out) + '"'
    if isinstance(value, list):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    raise TomlWriteError(f"cannot write a value of type {type(value).__name__}")


def _check_name(name: str, what: str) -> None:
    parts = name.split(".")
    if not name or not all(_BARE.match(p) for p in parts):
        raise TomlWriteError(f"bad {what} name {name!r}")


def _split_comment(rest: str) -> tuple[str, str]:
    """(value text, trailing comment) of the text after ``=``; refuses a value that spans several lines."""
    quote = ""
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif rest.startswith('"""', i) or rest.startswith("'''", i):
            raise TomlWriteError("the old value spans several lines; edit the file by hand")
        elif c in "\"'":
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "#":
            return rest[:i].rstrip(), rest[i:]
        i += 1
    if quote or depth > 0:
        raise TomlWriteError("the old value spans several lines; edit the file by hand")
    return rest.rstrip(), ""


def _key_line(line: str, key: str) -> re.Match[str] | None:
    return re.match(rf"^(\s*)({re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')(\s*=\s*)(.*)$", line)


def update_text(text: str, changes: Mapping[tuple[str, str], Value | None]) -> str:
    """``text`` with every ``(section, key) -> value`` applied; a value of None removes the key. Pure function."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TomlWriteError(f"the file is not valid TOML: {e}") from None
    for (section, key), value in changes.items():
        _check_name(section, "section")
        _check_name(key, "key")
        if value is not None:
            format_value(value)  # refuse early
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(chr(10))  # not splitlines(): that also splits on U+0085, U+2028, U+2029 inside values
    if lines and lines[-1] == "":
        lines.pop()
    lines = [ln[:-1] if ln.endswith(chr(13)) else ln for ln in lines]
    for (section, key), value in changes.items():
        lines = _apply(lines, section, key, value)
    out = nl.join(lines) + (nl if lines else "")
    try:
        back = tomllib.loads(out)
    except tomllib.TOMLDecodeError as e:
        raise TomlWriteError(f"the result would not be valid TOML ({e}); nothing written") from None
    for (section, key), value in changes.items():
        node: Any = back
        for part in section.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        got = node.get(key) if isinstance(node, dict) else None
        if got != value:
            raise TomlWriteError(f"{section}.{key} cannot be edited in this file (defined in a form this writer "
                                 "does not edit); edit the file by hand")
    return out


def _apply(lines: list[str], section: str, key: str, value: Value | None) -> list[str]:
    start = -1  # index of the header of ``section``
    end = len(lines)  # first line after the section
    cur = ""
    for i, line in enumerate(lines):
        if _ARRAY_HEADER.match(line):
            if start >= 0:
                end = i
                break
            cur = "\0"
            continue
        m = _HEADER.match(line)
        if m:
            if start >= 0:
                end = i
                break
            cur = ".".join(p.strip().strip("\"'") for p in m.group(1).split("."))
            if cur == section:
                start = i
    if start < 0:
        if value is None:
            return lines
        add = [f"[{section}]", f"{key} = {format_value(value)}"]
        if lines and lines[-1].strip():
            lines = [*lines, ""]
        return [*lines, *add]
    last_key = start  # the last line of the section that holds a ``key = value``
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            last_key = i
        m = _key_line(lines[i], key)
        if m:
            if value is None:
                return [*lines[:i], *lines[i + 1:]]
            _old, comment = _split_comment(m.group(4))
            new = f"{m.group(1)}{m.group(2)}{m.group(3)}{format_value(value)}"
            if comment:
                new += "  " + comment
            return [*lines[:i], new, *lines[i + 1:]]
    if value is None:
        return lines
    return [*lines[:last_key + 1], f"{key} = {format_value(value)}", *lines[last_key + 1:]]


def _replace(tmp: str, target: Path, attempts: int = 8, delay: float = 0.1) -> None:
    """``os.replace``; on Windows it fails with PermissionError while another program has the file open, so try a
    few times, then give a clear error."""
    for i in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if i == attempts - 1:
                raise TomlWriteError(f"cannot replace {target.name}: it is open in another program; close it "
                                     "and save again") from None
            time.sleep(delay)


def update_file(path: str | os.PathLike[str], changes: Mapping[tuple[str, str], Value | None]) -> None:
    """Apply ``changes`` to the file (created when missing) and replace it atomically."""
    p = Path(path)
    try:
        text = p.read_bytes().decode("utf-8")
    except FileNotFoundError:
        text = ""
    except UnicodeDecodeError:
        raise TomlWriteError("the file is not UTF-8") from None
    new = update_text(text.removeprefix("﻿"), changes)
    if new == text:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(p.stat().st_mode)  # keep the file's own mode (mkstemp makes 0600)
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(tmp, mode)
        _replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_value(path: str | os.PathLike[str], section: str, key: str, value: Value) -> None:
    update_file(path, {(section, key): value})


def remove_key(path: str | os.PathLike[str], section: str, key: str) -> None:
    update_file(path, {(section, key): None})
