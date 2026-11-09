# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Read-only YAML subset parser (replaces pyyaml). Used only to import an old kiko.yaml once.

Supported: block mappings and sequences (``- key: value`` items too), nesting by spaces
(a tab in indentation is an error), plain / single- / double-quoted scalars, int, float,
bool, null, empty value -> None, ``#`` comments, single-line flow sequences and mappings,
a leading ``---``. Scalar typing follows PyYAML (YAML 1.1: yes/no/on/off are booleans;
sexagesimal ints such as 1:30 are not recognised and stay strings).

Anything else (anchors, aliases, tags, block scalars, several documents, complex keys,
multi-line scalars or flow collections) raises ``YamlError`` carrying the line number.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple


class YamlError(ValueError):
    """Unsupported or malformed YAML; ``line`` is the 1-based line number (or None)."""

    def __init__(self, msg: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {msg}" if line else msg)
        self.line = line


class _Line(NamedTuple):
    indent: int
    content: str
    no: int


_KEY_COLON = re.compile(r":(?:\s|$)")
_DEC = re.compile(r"[-+]?(?:0|[1-9][0-9_]*)\Z")
_HEX = re.compile(r"[-+]?0x[0-9a-fA-F_]+\Z")
_BIN = re.compile(r"[-+]?0b[01_]+\Z")
_OCT = re.compile(r"[-+]?0[0-7_]+\Z")
_FLOAT = re.compile(r"(?:[-+]?[0-9][0-9_]*\.[0-9_]*|\.[0-9_]+)(?:[eE][-+][0-9]+)?\Z")
_NULLS = {"~", "null", "Null", "NULL", ""}
_TRUE = {"yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"}
_FALSE = {"no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"}
_ESC = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
    "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
    "L": " ", "P": " ",
}
_BAD_START = {
    "&": "anchors", "*": "aliases", "!": "tags", "|": "block scalars", ">": "block scalars",
    "%": "directives", "@": "reserved indicator", "`": "reserved indicator",
}


def _strip_comment(s: str) -> str:
    q = None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if q == "'":
            if c == "'":
                if s[i + 1:i + 2] == "'":
                    i += 1
                else:
                    q = None
        elif q == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                q = None
        elif c in "'\"" and (i == 0 or s[i - 1] in " \t[{,"):
            q = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
        i += 1
    return s


def _quoted(s: str, i: int, no: int) -> tuple[str, int]:
    """Parse a quoted scalar starting at s[i]; return (value, index after closing quote)."""
    q = s[i]
    out: list[str] = []
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if q == "'":
            if c == "'":
                if s[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), i + 1
            out.append(c)
            i += 1
        else:
            if c == '"':
                return "".join(out), i + 1
            if c == "\\":
                e = s[i + 1:i + 2]
                if e in ("x", "u", "U"):
                    w = {"x": 2, "u": 4, "U": 8}[e]
                    hexs = s[i + 2:i + 2 + w]
                    try:
                        if len(hexs) != w:
                            raise ValueError
                        out.append(chr(int(hexs, 16)))
                    except ValueError:
                        raise YamlError("bad escape in double-quoted string", no) from None
                    i += 2 + w
                elif e in _ESC:
                    out.append(_ESC[e])
                    i += 2
                else:
                    raise YamlError("bad escape in double-quoted string", no)
            else:
                out.append(c)
                i += 1
    raise YamlError("unterminated quoted string (multi-line scalars are not supported)", no)


def _plain(t: str) -> Any:
    if t in _NULLS:
        return None
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    try:
        if _DEC.match(t):
            return int(t.replace("_", ""))
        if _HEX.match(t):
            return int(t.replace("_", ""), 16)
        if _BIN.match(t):
            return int(t.replace("_", ""), 2)
        if _OCT.match(t):
            return int(t.replace("_", ""), 8)
        if _FLOAT.match(t):
            return float(t.replace("_", ""))
    except ValueError:
        return t
    low = t.lstrip("+-")
    if low in (".inf", ".Inf", ".INF"):
        return float("-inf") if t.startswith("-") else float("inf")
    if t in (".nan", ".NaN", ".NAN"):
        return float("nan")
    return t


def _check_start(t: str, no: int) -> None:
    if t and t[0] in _BAD_START:
        raise YamlError(f"unsupported YAML feature: {_BAD_START[t[0]]}", no)


def _flow(s: str, i: int, no: int, in_key: bool = False) -> tuple[Any, int]:
    n = len(s)
    while i < n and s[i] == " ":
        i += 1
    if i >= n:
        raise YamlError("unterminated flow collection (multi-line flow is not supported)", no)
    c = s[i]
    if c in "[{":
        close = "]" if c == "[" else "}"
        seq: list[Any] = []
        mp: dict[Any, Any] = {}
        i += 1
        while True:
            while i < n and s[i] == " ":
                i += 1
            if i >= n:
                raise YamlError("unterminated flow collection (multi-line flow is not supported)", no)
            if s[i] == close:
                return (seq if c == "[" else mp), i + 1
            if c == "[":
                item, i = _flow(s, i, no)
                seq.append(item)
            else:
                key, i = _flow(s, i, no, True)
                while i < n and s[i] == " ":
                    i += 1
                if i >= n or s[i] != ":":
                    raise YamlError("flow mapping entry needs 'key: value'", no)
                i += 1
                while i < n and s[i] == " ":
                    i += 1
                if i < n and s[i] in ",}":
                    mp[key] = None
                else:
                    mp[key], i = _flow(s, i, no)
            while i < n and s[i] == " ":
                i += 1
            if i < n and s[i] == ",":
                i += 1
            elif i < n and s[i] == close:
                continue
            else:
                raise YamlError("expected ',' or closing bracket in flow collection", no)
    if c in "\"'":
        return _quoted(s, i, no)
    _check_start(s[i:], no)
    j = i
    while j < n and s[j] not in ",]}" and not (in_key and s[j] == ":"):
        j += 1
    text = s[i:j].strip()
    if not text:
        raise YamlError("empty flow item", no)
    if not in_key and _KEY_COLON.search(text):
        raise YamlError("unsupported flow syntax", no)
    return _plain(text), j


def _value(rest: str, no: int) -> Any:
    _check_start(rest, no)
    if rest[0] in "\"'":
        v, end = _quoted(rest, 0, no)
        if rest[end:].strip():
            raise YamlError("unexpected text after quoted string", no)
        return v
    if rest[0] in "[{":
        v, end = _flow(rest, 0, no)
        if rest[end:].strip():
            raise YamlError("unexpected text after flow collection", no)
        return v
    if _KEY_COLON.search(rest):
        raise YamlError("mapping values are not allowed here", no)
    return _plain(rest)


def _split_key(content: str, no: int) -> tuple[Any, str]:
    if content[0] in "\"'":
        k, end = _quoted(content, 0, no)
        rest = content[end:].lstrip()
        if not rest.startswith(":") or (len(rest) > 1 and rest[1] != " "):
            raise YamlError("expected 'key: value'", no)
        return k, rest[1:].strip()
    m = _KEY_COLON.search(content)
    if not m:
        raise YamlError("expected 'key: value'", no)
    ktxt = content[: m.start()].rstrip()
    if ktxt.startswith("? ") or ktxt == "?":
        raise YamlError("unsupported YAML feature: complex keys", no)
    _check_start(ktxt, no)
    if ktxt[:1] in "[{":
        raise YamlError("unsupported YAML feature: flow keys", no)
    return _plain(ktxt), content[m.end():].strip()


def _is_key_line(content: str) -> bool:
    if content[0] in "[{":
        return False
    if content[0] in "\"'":
        try:
            _, end = _quoted(content, 0, 0)
        except YamlError:
            return False
        rest = content[end:].lstrip()
        return rest.startswith(":") and (len(rest) == 1 or rest[1] == " ")
    return bool(_KEY_COLON.search(content))


def _is_dash(content: str) -> bool:
    return content == "-" or content.startswith("- ")


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.pos = 0

    def _more(self) -> bool:
        return self.pos < len(self.lines)

    def block(self, indent: int) -> Any:
        ln = self.lines[self.pos]
        if _is_dash(ln.content):
            return self.seq(indent)
        if ln.content.startswith("? "):
            raise YamlError("unsupported YAML feature: complex keys", ln.no)
        if _is_key_line(ln.content):
            return self.mapping(indent)
        self.pos += 1
        return _value(ln.content, ln.no)

    def seq(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self._more() and self.lines[self.pos].indent == indent and _is_dash(self.lines[self.pos].content):
            ln = self.lines[self.pos]
            rest = ln.content[1:]
            body = rest.lstrip(" ")
            if not body:
                self.pos += 1
                if self._more() and self.lines[self.pos].indent > indent:
                    items.append(self.block(self.lines[self.pos].indent))
                else:
                    items.append(None)
            elif _is_dash(body) or _is_key_line(body) or body.startswith("? "):
                off = indent + 1 + len(rest) - len(body)
                self.lines[self.pos] = _Line(off, body, ln.no)
                items.append(self.block(off))
            else:
                items.append(_value(body, ln.no))
                self.pos += 1
        self._no_deeper(indent)
        return items

    def mapping(self, indent: int) -> dict[Any, Any]:
        out: dict[Any, Any] = {}
        while self._more() and self.lines[self.pos].indent == indent:
            ln = self.lines[self.pos]
            if _is_dash(ln.content):
                raise YamlError("unexpected sequence item in mapping", ln.no)
            key, rest = _split_key(ln.content, ln.no)
            self.pos += 1
            if rest:
                out[key] = _value(rest, ln.no)
            elif self._more() and self.lines[self.pos].indent > indent:
                out[key] = self.block(self.lines[self.pos].indent)
            elif self._more() and self.lines[self.pos].indent == indent and _is_dash(self.lines[self.pos].content):
                out[key] = self.seq(indent)
            else:
                out[key] = None
        self._no_deeper(indent)
        return out

    def _no_deeper(self, indent: int) -> None:
        if self._more() and self.lines[self.pos].indent > indent:
            raise YamlError("unexpected indentation", self.lines[self.pos].no)


def _tokenize(text: str) -> list[_Line]:
    lines: list[_Line] = []
    started = False
    for no, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        stripped = _strip_comment(raw).rstrip()
        content = stripped.strip()
        if not content:
            continue
        lead = stripped[: len(stripped) - len(stripped.lstrip(" \t"))]
        if "\t" in lead:
            raise YamlError("tab character in indentation", no)
        if content == "---" and not started:
            started = True
            continue
        started = True
        if content in ("---", "...") or content.startswith(("--- ", "... ")):
            raise YamlError("multiple documents / document markers are not supported", no)
        lines.append(_Line(len(lead), content, no))
    return lines


def load(text: str) -> Any:
    """Parse a YAML-subset document and return Python objects (None for an empty document)."""
    lines = _tokenize(text)
    if not lines:
        return None
    p = _Parser(lines)
    result = p.block(lines[0].indent)
    if p._more():
        raise YamlError("unexpected content", p.lines[p.pos].no)
    return result


def load_file(path: str | Path) -> Any:
    """Read ``path`` (UTF-8) and parse it with ``load``."""
    return load(Path(path).read_text(encoding="utf-8"))
