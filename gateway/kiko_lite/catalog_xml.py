# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""The model catalog as XML (``kiko-lite export-models --xml``, ``GET /kiko/v1/models.xml``).

One ``<model id="ALIAS" enabled="true|false" status="ok|error|unknown">`` per row, ordered by alias:

* ``<alias>``       human name (``display_name``, else the alias); ``id`` is the database alias
* ``<name>``        the full provider model id as stored (``nvidia/nvidia/nemotron-3-ultra-550b-a55b``)
* ``<key>``         the API key NAME (``NVIDIA_API_KEY``), never a value; a stored value-looking text is hidden
* ``<api_base>``    only when the row has its own API base
* ``<roles>``       one ``<role>`` per role, the primary first and marked ``primary="true"``
* ``<context>``, ``<max_output>``  integer tokens; the element is always there, empty when unknown
* ``<price per="1M">``  ``<in>`` / ``<out>`` (and ``<cache_read>`` when known) in USD, or the text ``free`` when the
  model is served free (stored price 0 / 0, or a free-tier provider, see ``FREE_TIER_PROVIDERS``); empty = unknown
* ``<priority>`` always; ``<thinking>``, ``<timeout>``, ``<toolshim>`` only when set

The schema is ``docs/schema/kiko-models.xsd`` (documented in docs/MODELS.md). Standard library only.

The reverse (``kiko-lite import-models --xml FILE``, ``tools/inject.py FILE``): ``parse_catalog`` checks the whole file
against the schema and returns EVERY error (nothing is written when there is one), ``plan_changes`` says what would be
added / changed / left alone, ``run_import`` writes all of it in one transaction after a timestamped backup, and a new
model stays disabled unless the file says ``enabled="true"`` AND it answers a ping through the running gateway.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kikolib import config as kconfig

from . import app as lite
from .db import CLASSES, THINKING_MODES, Database, check_timeout
from .resolver import MappingError, check_api_base, custom_provider_names, key_name_problem, map_row, show_name

FORMAT_VERSION = "1"
# Providers whose models are served without a price (the free tier): their rows read "free" even while the price
# columns are NULL (kiko_lite.prices has no entry for NVIDIA's hosted NIM API).
FREE_TIER_PROVIDERS = frozenset({"nvidia"})


def num(x: float | int) -> str:
    """A number for the file: 5.0 -> "5", 0.075 -> "0.075" (shortest text that reads back exactly)."""
    text = repr(float(x))
    return text[:-2] if text.endswith(".0") else text


def is_free(row: Mapping[str, Any]) -> bool:
    p_in, p_out = row.get("price_in_per_mtok"), row.get("price_out_per_mtok")
    if p_in is not None and p_out is not None:
        return p_in == 0 and p_out == 0
    prefix = str(row.get("provider") or "").partition("/")[0].strip().lower()
    return p_in is None and p_out is None and prefix in FREE_TIER_PROVIDERS


def _text(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text:
        el.text = text
    return el


def model_element(row: Mapping[str, Any]) -> ET.Element:
    alias = str(row["alias"])
    el = ET.Element("model", {"id": alias, "enabled": "true" if row.get("enabled") else "false",
                              "status": str(row.get("status") or "unknown")})
    _text(el, "alias", (row.get("display_name") or "").strip() or alias)
    _text(el, "name", str(row.get("provider") or ""))
    _text(el, "key", show_name(row.get("api_key_env")))
    if row.get("api_base"):
        _text(el, "api_base", str(row["api_base"]))
    roles = row.get("roles") or [row.get("model_class") or "SONNET"]
    rs = ET.SubElement(el, "roles")
    for i, role in enumerate(roles):
        r = _text(rs, "role", str(role))
        if i == 0:
            r.set("primary", "true")
    for tag, col in (("context", "context_window"), ("max_output", "max_output")):
        v = row.get(col)
        _text(el, tag, str(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None)
    price = ET.SubElement(el, "price", {"per": "1M"})
    if is_free(row):
        price.text = "free"
    elif row.get("price_in_per_mtok") is not None and row.get("price_out_per_mtok") is not None:
        _text(price, "in", num(row["price_in_per_mtok"]))
        _text(price, "out", num(row["price_out_per_mtok"]))
        if row.get("price_cache_read_per_mtok") is not None:
            _text(price, "cache_read", num(row["price_cache_read_per_mtok"]))
    _text(el, "priority", str(int(row.get("priority") if row.get("priority") is not None else 100)))
    if row.get("thinking"):
        _text(el, "thinking", str(row["thinking"]))
    if row.get("timeout") is not None:
        _text(el, "timeout", num(row["timeout"]))
    if row.get("toolshim"):
        _text(el, "toolshim", str(row["toolshim"]))
    return el


def export_xml(rows: list[dict[str, Any]]) -> bytes:
    """The catalog for ``rows`` (``Database.models()``) as UTF-8 XML bytes; stable: ordered by alias."""
    root = ET.Element("kiko-models", {"version": FORMAT_VERSION})
    for row in sorted(rows, key=lambda r: (str(r["alias"]).lower(), str(r["alias"]))):
        root.append(model_element(row))
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


# ---- import -----------------------------------------------------------------------------------------------

MAX_FILE_BYTES = 2_000_000
KEY_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")  # an UPPER_SNAKE environment variable NAME
ALIAS = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
CLASS = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
# Text that is the start of a well-known API key format: a file holding one is refused whole.
VALUE_LIKE = re.compile(r"(?:sk-|sk_|rk-|gh[pousr]_|AIza|xox[baprs]-|nvapi-|gsk_|xai-|pplx-|hf_|AKIA)[A-Za-z0-9_\-]{8,}")
MODEL_CHILDREN = ("alias", "name", "key", "api_base", "roles", "context", "max_output", "price", "priority",
                  "thinking", "timeout", "toolshim")
REQUIRED = ("alias", "name", "key", "roles", "context", "max_output", "price", "priority")


@dataclass
class Entry:
    """One ``<model>`` of a catalog file, checked."""
    alias: str
    provider: str
    key: str
    roles: list[str]
    primary: str
    api_base: str | None = None
    context: int | None = None
    max_output: int | None = None
    free: bool = False
    price_in: float | None = None
    price_out: float | None = None
    price_cache: float | None = None
    priority: int = 100
    thinking: str | None = None
    timeout: float | None = None
    toolshim: str | None = None
    display_name: str | None = None
    enabled: bool = False
    errors: list[str] = field(default_factory=list, repr=False)


def _int(text: str, what: str, errors: list[str], minimum: int | None = None) -> int | None:
    t = text.strip()
    if not re.fullmatch(r"-?\d{1,15}", t):
        errors.append(f"{what} must be a whole number, got '{t[:40]}'")
        return None
    v = int(t)
    if minimum is not None and v < minimum:
        errors.append(f"{what} must be {minimum} or more")
        return None
    return v


def _num(text: str, what: str, errors: list[str]) -> float | None:
    t = text.strip()
    try:
        v = float(t)
    except ValueError:
        errors.append(f"{what} must be a number, got '{t[:40]}'")
        return None
    if not math.isfinite(v) or v < 0 or not re.fullmatch(r"[0-9.eE+-]+", t):
        errors.append(f"{what} must be a finite number, zero or more")
        return None
    return v


def _bool(text: str | None) -> bool | None:
    t = (text or "").strip().lower()
    return True if t in ("true", "1") else False if t in ("false", "0") else None


def _scan_values(root: ET.Element, errors: list[str]) -> None:
    """A key VALUE anywhere in the file (text or attribute) refuses the whole file."""
    for el in root.iter():
        for text in [el.text or "", el.tail or "", *el.attrib.values()]:
            if VALUE_LIKE.search(text):
                errors.append(f"<{el.tag}> contains text that looks like an API key value; the file is refused "
                              "(only key NAMES belong in it)")
                return


def _parse_model(el: ET.Element, classes: Collection[str], custom: Collection[str]) -> Entry:
    errors: list[str] = []
    alias = el.get("id") or ""
    if not ALIAS.match(alias):
        errors.append("attribute id (the alias) is missing or not [A-Za-z0-9._:-]{1,128}")
    for a in el.attrib:
        if a not in ("id", "enabled", "status"):
            errors.append(f"unknown attribute '{a}'")
    enabled = _bool(el.get("enabled"))
    if enabled is None:
        errors.append("attribute enabled must be true or false")
    kids: dict[str, ET.Element] = {}
    for c in el:
        if c.tag not in MODEL_CHILDREN:
            errors.append(f"unknown element <{c.tag}>")
        elif c.tag in kids:
            errors.append(f"element <{c.tag}> appears twice")
        else:
            kids[c.tag] = c
    for tag in REQUIRED:
        if tag not in kids:
            errors.append(f"element <{tag}> is missing")

    def text(tag: str) -> str:
        return (kids[tag].text or "").strip() if tag in kids else ""

    name, key = text("name"), text("key")
    if "key" in kids and not (KEY_NAME.match(key) and key_name_problem(key) is None):
        errors.append("<key> must be the NAME of an environment variable in UPPER_SNAKE_CASE, not a key value")
        key = ""
    base = text("api_base") or None
    if base:
        try:
            check_api_base(base)
        except ValueError as e:
            errors.append(f"<api_base>: {e}")
            base = None
    if "name" in kids and not name:
        errors.append("<name> is empty")
    elif name and key:
        try:
            mapped = map_row({"provider": name, "api_base": base, "api_key_env": key}, custom)
            if not lite.valid_model_id(mapped.model_id, str(mapped.entry.get("kind", "openai"))):
                errors.append("<name>: the model id has characters that are not allowed")
        except (MappingError, ValueError) as e:
            errors.append(f"<name>: {e}")
    roles: list[str] = []
    primary = ""
    if "roles" in kids:
        marked = []
        for r in kids["roles"]:
            if r.tag != "role":
                errors.append(f"<roles> holds <{r.tag}>; only <role> is allowed")
                continue
            role = (r.text or "").strip()
            if not CLASS.match(role):
                errors.append(f"role '{role[:40]}' is not a class name (UPPER_SNAKE)")
            elif role not in classes:
                errors.append(f"role {role} is not a class of this database (list-classes)")
            elif role in roles:
                errors.append(f"role {role} appears twice")
            else:
                roles.append(role)
                flag = _bool(r.get("primary")) if r.get("primary") is not None else False
                if flag is None:
                    errors.append(f"role {role}: primary must be true or false")
                elif flag:
                    marked.append(role)
        if not roles:
            errors.append("<roles> needs at least one valid <role> (a model keeps at least one role)")
        if len(marked) > 1:
            errors.append(f"more than one primary role ({', '.join(marked)})")
        primary = marked[0] if marked else (roles[0] if roles else "")
    e = Entry(alias, name, key, roles, primary, base, enabled=bool(enabled))
    for tag in ("context", "max_output"):
        if tag in kids and text(tag):
            setattr(e, tag, _int(text(tag), f"<{tag}>", errors, 1))
    if "price" in kids:
        price = kids["price"]
        if price.get("per") != "1M":
            errors.append('<price> needs per="1M"')
        sub = {c.tag: c for c in price}
        bad = [t for t in sub if t not in ("in", "out", "cache_read")]
        if bad:
            errors.append(f"<price> holds unknown <{bad[0]}>")
        txt = (price.text or "").strip()
        if sub:
            if txt:
                errors.append("<price> mixes text and <in>/<out>")
            if "in" not in sub or "out" not in sub:
                errors.append("<price> needs both <in> and <out> (or the text free, or nothing)")
            else:
                e.price_in = _num(sub["in"].text or "", "<price><in>", errors)
                e.price_out = _num(sub["out"].text or "", "<price><out>", errors)
            if "cache_read" in sub:
                e.price_cache = _num(sub["cache_read"].text or "", "<price><cache_read>", errors)
        elif txt == "free":
            e.free = True
        elif txt:
            errors.append(f"<price> must be 'free', <in>/<out> or empty, got '{txt[:40]}'")
    if "priority" in kids:
        v = _int(text("priority"), "<priority>", errors)
        e.priority = 100 if v is None else v
    if "thinking" in kids:
        if text("thinking") not in THINKING_MODES:
            errors.append(f"<thinking> must be one of {', '.join(THINKING_MODES)}")
        else:
            e.thinking = None if text("thinking") == "default" else text("thinking")
    if "timeout" in kids:
        try:
            e.timeout = check_timeout(text("timeout"))
        except ValueError as ex:
            errors.append(f"<timeout>: {ex}")
    if "toolshim" in kids:
        if text("toolshim") != "on":
            errors.append("<toolshim> must be on (leave the element out for off)")
        else:
            e.toolshim = "on"
    human = text("alias")
    if "alias" in kids:
        if not human or len(human) > 200 or any(ord(ch) < 32 for ch in human):
            errors.append("<alias> (the human name) must be 1 to 200 printable characters")
        elif human != alias:
            e.display_name = human
    e.errors = [f"model '{alias or '?'}': {m}" for m in errors]
    return e


def parse_catalog(data: bytes, classes: Collection[str],
                  custom: Collection[str] = ()) -> tuple[list[Entry], list[str]]:
    """Check a catalog file against the schema. Returns (entries, errors); with any error nothing may be written.
    ALL errors are collected so the file can be fixed in one go."""
    if len(data) > MAX_FILE_BYTES:
        return [], [f"file is larger than {MAX_FILE_BYTES // 1_000_000} MB"]
    if re.search(rb"<!(?:DOCTYPE|ENTITY)", data, re.I):
        return [], ["DOCTYPE and ENTITY declarations are not allowed"]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return [], [f"not well-formed XML: {e}"]
    errors: list[str] = []
    if root.tag != "kiko-models":
        return [], [f"root element must be <kiko-models>, got <{root.tag}>"]
    if root.get("version") != FORMAT_VERSION:
        errors.append(f'<kiko-models> needs version="{FORMAT_VERSION}"')
    _scan_values(root, errors)
    entries: list[Entry] = []
    seen: set[str] = set()
    for el in root:
        if el.tag != "model":
            errors.append(f"unexpected element <{el.tag}> under <kiko-models>")
            continue
        e = _parse_model(el, classes, custom)
        errors.extend(e.errors)
        if e.alias in seen:
            errors.append(f"model '{e.alias}': id appears twice")
        seen.add(e.alias)
        entries.append(e)
    return entries, [VALUE_LIKE.sub("<hidden>", m) for m in errors]  # an error never echoes a key value


@dataclass
class Change:
    entry: Entry
    action: str  # "added" | "changed" | "unchanged"
    diffs: list[str] = field(default_factory=list)


def _wanted_prices(e: Entry) -> tuple[float | None, float | None, float | None]:
    if e.free:
        return 0.0, 0.0, None
    return e.price_in, e.price_out, e.price_cache


def plan_changes(existing: Mapping[str, Mapping[str, Any]], entries: list[Entry]) -> list[Change]:
    """added / changed / unchanged per entry, by alias. ``existing``: alias -> row (``Database.models()``).
    Unknown values in the file (empty context, empty price) keep what the database has; the ``enabled`` flag and
    health status of an existing model are never touched."""
    out = []
    for e in entries:
        row = existing.get(e.alias)
        if row is None:
            out.append(Change(e, "added"))
            continue
        pi, po, pc = _wanted_prices(e)
        checks = (
            ("name", row.get("provider") != e.provider),
            ("key", row.get("api_key_env") != e.key),
            ("api_base", (row.get("api_base") or None) != e.api_base),
            ("primary role", row.get("model_class") != e.primary),
            ("roles", set(row.get("roles") or [row.get("model_class")]) != set(e.roles)),
            ("context", e.context is not None and row.get("context_window") != e.context),
            ("max_output", e.max_output is not None and row.get("max_output") != e.max_output),
            # "free" matches what the exporter calls free (0 / 0, or a free-tier provider with no stored price)
            ("price", e.free and not is_free(row)),
            ("price in", not e.free and pi is not None and row.get("price_in_per_mtok") != pi),
            ("price out", not e.free and po is not None and row.get("price_out_per_mtok") != po),
            ("price cache_read", not e.free and pc is not None and row.get("price_cache_read_per_mtok") != pc),
            ("priority", row.get("priority") != e.priority),
            ("thinking", (row.get("thinking") or None) != e.thinking),
            ("timeout", (row.get("timeout") or None) != e.timeout),
            ("toolshim", (row.get("toolshim") or None) != e.toolshim),
            ("display name", (row.get("display_name") or None) != e.display_name),
        )
        d = [name for name, differs in checks if differs]
        out.append(Change(e, "changed" if d else "unchanged", d))
    return out


def upsert_args(c: Change) -> dict[str, Any]:
    """The ``Database.upsert_models`` item for one change. New models are added disabled; existing ones keep
    ``enabled`` and their health status."""
    e = c.entry
    pi, po, pc = _wanted_prices(e)
    return dict(alias=e.alias, provider=e.provider, api_key_env=e.key, api_base=e.api_base,
                model_class=e.primary, priority=e.priority, price_in=pi, price_out=po, price_cache=pc,
                enabled=False if c.action == "added" else None, thinking=e.thinking or "default",
                timeout=e.timeout if e.timeout is not None else "default", toolshim=e.toolshim or "off",
                context_window=e.context, max_output=e.max_output, roles=e.roles,
                display_name=e.display_name or "")


def backup_database(path: Path, tag: str = "import") -> Path:
    """Copy the database next to itself (``kiko.db.bak-YYYYMMDD-HHMMSS-import``) with the SQLite backup API."""
    target = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{tag}")
    src = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def ping_model(alias: str, timeout: float = 40.0) -> tuple[bool, str]:
    """Ask the running kiko-lite for one answer from ``alias`` (a tiny /v1/messages call). (worked, note)."""
    url = f"http://127.0.0.1:{kconfig.proxy_port()}/v1/messages"
    body = json.dumps({"model": alias, "max_tokens": 1024,
                       "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "x-api-key": os.environ.get("MASTER_KEY") or "sk-dummy-key", "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            reply = "".join(p.get("text", "") for p in data.get("content", []) if isinstance(p, dict)).strip()
            return (resp.status == 200 and bool(reply)), (f"answered '{reply[:20]}'" if reply else "empty answer")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # not running, timeout
        return False, type(e).__name__


def run_import(db_path: Path, data: bytes, *, dry_run: bool = False,
               pinger: Callable[[str], tuple[bool, str]] | None = None,
               say: Callable[[str], None] = print, settle: float = 2.5) -> int:
    """Import a catalog file. 0 = done (or a clean dry run), 2 = the file or the database refused it (nothing written).

    All errors of the file are reported before anything is written; the write is ONE transaction; a real write into
    an existing database is preceded by a timestamped backup. A new model is disabled unless its ``enabled="true"``
    is confirmed by a ping through kiko (``pinger``)."""
    exists = db_path.exists()
    db: Database | None = Database(db_path, readonly=dry_run) if exists else None  # a new file only after the check
    classes = db.class_names() if db is not None else list(CLASSES)
    existing = {r["alias"]: r for r in db.models()} if db is not None else {}
    custom = custom_provider_names(db) if db is not None else frozenset()
    entries, errors = parse_catalog(data, classes, custom)
    if errors:
        say(f"kiko: the catalog file is not valid ({len(errors)} problem(s)); nothing was written:")
        for m in errors:
            say(f"  - {m}")
        return 2
    changes = plan_changes(existing, entries)
    sym = {"added": "+", "changed": "~", "unchanged": "="}
    for c in changes:
        extra = f" ({', '.join(c.diffs)})" if c.diffs else ""
        if c.action == "added":
            extra = (f" (roles {', '.join(c.entry.roles)}; "
                     + ("enabled only if it answers a ping" if c.entry.enabled else "disabled") + ")")
        say(f"  {sym[c.action]} {c.action:<9} {c.entry.alias}{extra}")
    counts = {a: sum(1 for c in changes if c.action == a) for a in sym}
    say(f"kiko: {counts['added']} added, {counts['changed']} changed, {counts['unchanged']} unchanged"
        + (" (dry run, nothing written)" if dry_run else ""))
    todo = [c for c in changes if c.action != "unchanged"]
    if dry_run or not todo:
        return 0
    if db is None:
        db = Database(db_path)
    if exists and existing:
        say(f"kiko: backup {backup_database(db_path).name}")
    try:
        db.upsert_models([upsert_args(c) for c in todo])  # one transaction: all or nothing
    except ValueError as e:
        say(f"kiko: the database refused the import ({e}); nothing was written")
        return 2
    wants = [c.entry.alias for c in todo if c.action == "added" and c.entry.enabled]
    if wants:
        ping = pinger or ping_model
        for alias in wants:  # a ping goes through the gateway, which only serves enabled models
            db.set_enabled(alias, True)
        time.sleep(settle)  # the gateway's catalog is cached for a moment
        for alias in wants:
            ok, note = ping(alias)
            if ok:
                db.set_status(alias, "ok")
                say(f"kiko: {alias}: ping ok ({note}); enabled")
            else:
                db.set_enabled(alias, False)
                db.set_status(alias, "error")
                say(f"kiko: {alias}: ping failed ({note}); left DISABLED (enable-model {alias} once it works)")
    return 0
