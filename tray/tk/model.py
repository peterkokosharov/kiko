# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""Pure data shaping: kiko-lite status JSON -> overall state, tooltip, icon colour, table rows (no tkinter)."""
from __future__ import annotations

from typing import Any

COLORS = {"ok": "#22c55e", "errors": "#f59e0b", "down": "#ef4444", "auth": "#a855f7", "unknown": "#9ca3af"}
RECENT_FOR_STATE = 5  # the newest requests looked at for the "errors" state


def _items(status: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """status[key] as a list of dicts; anything else (not a list, non-dict entries) is dropped."""
    v = status.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def overall_state(status: dict[str, Any] | None, problem: str = "") -> str:
    """ok / errors / down (not reachable) / auth (client key needed) / unknown (nothing fetched yet)."""
    if status is None:
        return {"": "unknown", "auth": "auth"}.get(problem, "down")
    bad_model = any(m.get("state") == "bad" for m in _items(status, "models"))
    bad_req = any(not r.get("ok", True) for r in _items(status, "requests")[:RECENT_FOR_STATE])
    return "errors" if bad_model or bad_req else "ok"


def tooltip(state: str, status: dict[str, Any] | None, base: str, problem: str = "") -> str:
    if status is None:
        if state == "auth":
            return "kiko-lite needs a client key (--key / KIKO_TK_KEY)"
        return f"kiko-lite not reachable ({base})" if state == "down" else "kiko-lite: waiting"
    models = _items(status, "models")
    bad = sum(1 for m in models if m.get("state") == "bad")
    ok = sum(1 for m in models if m.get("state") == "ok")
    return f"kiko-lite {state}: {ok} ok / {bad} bad of {len(models)} models"


def server_rows(status: dict[str, Any]) -> list[tuple[str, str]]:
    s = status.get("server")
    s = s if isinstance(s, dict) else {}
    up = s.get("uptime_seconds")
    up = up if isinstance(up, int) else 0
    return [("name", str(s.get("name", ""))), ("version", str(s.get("version", ""))),
            ("port", str(s.get("port", ""))), ("uptime", f"{up // 3600} h {up % 3600 // 60} min"),
            ("database", str(s.get("db", "")))]


def model_rows(status: dict[str, Any]) -> list[tuple[str, ...]]:
    """alias, target, class, enabled, key name, key found (never a value), health, last check, state."""
    return [(m.get("alias", "?"), m.get("target") or "", m.get("class") or "", "yes" if m.get("enabled") else "no",
             m.get("key_name") or "", m.get("key_found") or "", m.get("mapping_error") or m.get("health") or "",
             m.get("last_check") or "", m.get("state") or "off") for m in _items(status, "models")]


def request_rows(status: dict[str, Any]) -> list[tuple[str, ...]]:
    """ok, time, model (> upstream), route, latency, error."""
    out = []
    for r in _items(status, "requests"):
        model = str(r.get("model") or "") + (f" > {r['upstream']}" if r.get("upstream") else "")
        lat = r.get("latency_ms")
        out.append(("ok" if r.get("ok") else "ERR", r.get("time") or "", model, r.get("route") or "",
                    "" if lat is None else f"{lat} ms", r.get("error") or ""))
    return out


def class_rows(status: dict[str, Any]) -> list[tuple[str, ...]]:
    """Derived from the models: class, number of models, enabled, aliases (kiko-lite has no classes endpoint)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for m in _items(status, "models"):
        by.setdefault(m.get("class") or "-", []).append(m)
    return [(c, str(len(ms)), str(sum(1 for m in ms if m.get("enabled"))), ", ".join(str(m.get("alias", "?")) for m in ms))
            for c, ms in sorted(by.items())]
