# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Thread-safe registry of pluggable implementations.

Each kind (e.g. "keystore") has several named implementations. ``register`` adds one
with a probe (is it usable here?) and a loader. ``choose`` picks: explicit override,
else env KIKO_<KIND>_IMPL, else the first probe-ok by priority (lower first). The
choice is cached per kind; ``report`` describes it for ``kiko doctor``.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

_lock = threading.RLock()
_impls: dict[str, dict[str, dict[str, Any]]] = {}
_chosen: dict[str, dict[str, Any]] = {}


def register(
    kind: str,
    name: str,
    *,
    probe: Callable[[], tuple[bool, str]],
    load: Callable[[], Any],
    priority: int = 100,
) -> None:
    """Register implementation ``name`` of ``kind`` (replaces a same-named one)."""
    with _lock:
        _impls.setdefault(kind, {})[name] = {"probe": probe, "load": load, "priority": priority}
        _chosen.pop(kind, None)


def _probe(entry: dict[str, Any]) -> tuple[bool, str]:
    try:
        ok, reason = entry["probe"]()
    except Exception as exc:  # a broken probe means "not available"
        return False, f"probe failed: {type(exc).__name__}"
    return bool(ok), str(reason)


def _candidates(kind: str) -> list[tuple[str, dict[str, Any]]]:
    items = _impls.get(kind, {})
    return sorted(items.items(), key=lambda kv: (kv[1]["priority"], kv[0]))


def choose(kind: str, override: str | None = None) -> Any:
    """Return the implementation object for ``kind`` (cached after the first call)."""
    with _lock:
        cur = _chosen.get(kind)
        if cur is not None and (not override or override == cur["name"]):
            return cur["impl"]
        items = _impls.get(kind)
        if not items:
            raise LookupError(f"no implementations registered for kind {kind!r}")
        env_name = f"KIKO_{kind.upper()}_IMPL"
        name, source = None, "auto"
        if override:
            name, source = override, "override"
        elif os.environ.get(env_name):
            name, source = os.environ[env_name], "env"
        reason = ""
        if name is not None:
            if name not in items:
                raise LookupError(
                    f"{kind}: unknown implementation {name!r} (from {source}); known: {sorted(items)}"
                )
            ok, reason = _probe(items[name])
            if not ok:
                raise RuntimeError(f"{kind}: implementation {name!r} (from {source}) unavailable: {reason}")
        else:
            for cname, entry in _candidates(kind):
                ok, reason = _probe(entry)
                if ok:
                    name = cname
                    break
            if name is None:
                raise RuntimeError(f"{kind}: no implementation available; known: {sorted(items)}")
        impl = items[name]["load"]()
        _chosen[kind] = {"name": name, "source": source, "reason": reason, "impl": impl}
        return impl


def available(kind: str) -> list[tuple[str, Any]]:
    """Return (name, impl) for every probe-ok implementation of ``kind``, in priority order."""
    with _lock:
        return [(n, e["load"]()) for n, e in _candidates(kind) if _probe(e)[0]]


def report() -> list[dict[str, Any]]:
    """Describe every registered kind (for doctor). chosen is None if nothing is usable."""
    out = []
    with _lock:
        for kind in sorted(_impls):
            cands = []
            for name, entry in _candidates(kind):
                ok, reason = _probe(entry)
                cands.append({"name": name, "ok": ok, "reason": reason})
            ch = _chosen.get(kind)
            if ch is None:
                try:
                    choose(kind)
                except (LookupError, RuntimeError):
                    pass
                ch = _chosen.get(kind)
            out.append({
                "kind": kind,
                "chosen": ch["name"] if ch else None,
                "source": ch["source"] if ch else None,
                "reason": ch["reason"] if ch else "nothing available",
                "candidates": cands,
            })
    return out


def reset() -> None:
    """Forget cached choices (tests). Registrations are kept."""
    with _lock:
        _chosen.clear()


def clear() -> None:
    """Forget registrations and choices (tests)."""
    with _lock:
        _impls.clear()
        _chosen.clear()
