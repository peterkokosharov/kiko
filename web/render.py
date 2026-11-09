# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""HTML building blocks. Every value is escaped by default; only `Markup` (built by these helpers) passes raw."""

from __future__ import annotations

import html
from collections.abc import Iterable
from string import Template
from typing import Any
from urllib.parse import urlencode


class Markup(str):
    """A string that is already safe HTML (produced by `tag`, `join` or `raw`)."""


def esc(value: Any) -> Markup:
    if isinstance(value, Markup):
        return value
    return Markup(html.escape("" if value is None else str(value), quote=True))


def raw(text: str) -> Markup:
    """Only for constant, developer-written HTML; never for data."""
    return Markup(text)


def join(items: Iterable[Any], sep: str = "") -> Markup:
    return Markup(sep.join(esc(i) for i in items))


def _attrs(attrs: dict[str, Any]) -> str:
    out = []
    for k, v in attrs.items():
        name = k.rstrip("_").replace("_", "-")
        if v is True:
            out.append(f" {name}")
        elif v is not None and v is not False:
            out.append(f' {name}="{esc(v)}"')
    return "".join(out)


def tag(name: str, /, *content: Any, **attrs: Any) -> Markup:
    return Markup(f"<{name}{_attrs(attrs)}>{join(content)}</{name}>")


def void(name: str, /, **attrs: Any) -> Markup:
    return Markup(f"<{name}{_attrs(attrs)}>")


def link(text: Any, href: str, **attrs: Any) -> Markup:
    return tag("a", text, href=href, **attrs)


def url(path: str, **query: Any) -> str:
    q = {k: v for k, v in query.items() if v is not None}
    return path + ("?" + urlencode(q) if q else "")


# --- forms ---
def form(action: str, csrf: str, *content: Any, **attrs: Any) -> Markup:
    token = void("input", type="hidden", name="csrf", value=csrf)
    return tag("form", token, *content, method="post", action=action, **attrs)


def field(label: str, name: str, value: Any = "", kind: str = "text", **attrs: Any) -> Markup:
    box = void("input", type=kind, name=name, value=value, id="f-" + name, **attrs)
    return tag("p", tag("label", label, for_="f-" + name), box)


def select(label: str, name: str, options: list[Any], current: str) -> Markup:
    """``options``: values, or (value, label) pairs."""
    opts = []
    for o in options:
        value, text = o if isinstance(o, tuple) else (o, o)
        opts.append(tag("option", text, value=value, selected=(value == current)))
    return tag("p", tag("label", label, for_="f-" + name), tag("select", *opts, name=name, id="f-" + name))


def checkbox(label: str, name: str, checked: bool) -> Markup:
    return tag("p", tag("label", void("input", type="checkbox", name=name, checked=checked), " ", label))


def button(text: str, cls: str = "") -> Markup:
    return tag("button", text, type="submit", class_=cls or None)


def inline_post(action: str, csrf: str, text: str, cls: str = "", **hidden: str) -> Markup:
    """A one-button POST form (delete, toggle) with hidden fields."""
    fields = [void("input", type="hidden", name=k, value=v) for k, v in hidden.items()]
    return form(action, csrf, *fields, button(text, cls), class_="inline")


def table(headers: list[str], rows: list[list[Any]]) -> Markup:
    head = tag("tr", *[tag("th", h) for h in headers])
    body = [tag("tr", *[tag("td", c) for c in row]) for row in rows]
    if not body:
        body = [tag("tr", tag("td", "nothing here yet", colspan=len(headers), class_="muted"))]
    return tag("table", tag("thead", head), tag("tbody", *body))


def pager(path: str, offset: int, limit: int, total: int) -> Markup:
    prev = link("newer", url(path, offset=max(0, offset - limit), limit=limit)) if offset > 0 else ""
    nxt = link("older", url(path, offset=offset + limit, limit=limit)) if offset + limit < total else ""
    shown = f"{min(total, offset + 1)}-{min(total, offset + limit)} of {total} "
    return tag("p", shown, prev, " ", nxt, class_="pager")


# --- page layout ---
NAV = (("/", "Dashboard"), ("/models", "Models"), ("/providers", "Providers"), ("/classes", "Classes"),
       ("/aliases", "Aliases"), ("/billing", "Billing"), ("/logs", "Logs"), ("/responses", "Text log"),
       ("/settings", "Settings"), ("/password", "Password"))

_LAYOUT = Template("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title - kiko</title><link rel="stylesheet" href="/static/style.css"></head>
<body><header><strong>kiko</strong>$nav$logout</header>
<main>$flash<h1>$title</h1>$body</main>$script</body></html>
""")


def layout(title: str, body: Any, *, active: str = "", csrf: str = "",
           flashes: Iterable[tuple[str, str]] = (), script: str = "") -> str:
    """A full page. With a csrf token the user is logged in (menu and log-out button are shown)."""
    nav = tag("nav", *[link(t, p, aria_current="page" if p == active else None) for p, t in NAV]) if csrf else ""
    logout = inline_post("/logout", csrf, "Log out", "secondary") if csrf else ""
    flash = join(tag("p", msg, class_="flash " + kind) for kind, msg in flashes)
    scr = tag("script", "", src="/static/" + script, defer=True) if script else ""
    return _LAYOUT.substitute(title=esc(title), nav=nav, logout=logout, flash=flash, body=esc(body), script=scr)
