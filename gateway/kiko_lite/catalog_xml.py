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
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

from .resolver import show_name

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
