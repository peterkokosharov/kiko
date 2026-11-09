# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Read-only status page of ``serve``: ``GET /ui`` (one self-contained HTML page) and ``GET /ui/api/status`` (JSON).

The JSON has, per model, the alias, provider / model id, class, enabled, the key NAME and WHERE it is found
(env / store / dotenv / NOT FOUND; never a value, a prefix or a length), the last health result (``models.status`` /
``last_check``); the last ``RECENT`` requests of the billing table (error texts shortened); version, uptime, port,
database path. Nothing here writes, probes or calls an upstream.

Access: loopback only (the Host header must be localhost / 127.x / ::1 AND the peer address must be loopback, else
403, also under a non-loopback bind).
The browser guard of the app stays on: the page's own Origin (http://<Host>) is accepted for ``GET /ui*`` only
(``App.same_origin_paths``); ``/v1/*`` and every POST still refuse any Origin. No CORS headers. The page itself holds
no data and needs no key; ``/ui/api/status`` needs the same client key as every other call (header ``x-api-key``
or ``Authorization: Bearer``) unless the server runs without auth; the page asks for the key on a 401 and keeps it
in memory only.

Key lookups (the OS key store can take many seconds, e.g. a locked wallet) never run in a request: ONE background
thread (single-flight, guarded by a lock) refreshes the names, at most once per ``KEY_CACHE_SECONDS``; a status call
returns the last known result, ``checking`` before the first one, ``store timeout`` when the store did not answer.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import kikolib
from kikolib import keystore, server
from kikolib.server import HTTPError, JsonResponse, Request, StreamResponse

from . import keys
from .resolver import MappingError, custom_provider_names, map_row

RECENT = 30  # requests shown
ERROR_CHARS = 120
KEY_CACHE_SECONDS = 30.0  # the OS key store may be slow: look each key name up at most this often


def _loopback(req: Request) -> None:
    host = req.headers.get("host")
    peer = req.client[0] if req.client else ""
    if host is None or not server._is_loopback(server._host_only(host)) or not server._is_loopback(peer):
        raise HTTPError(403, "permission_error", "the status page is for loopback only")


def _iso_age(ts: Any) -> str | None:
    return ts if isinstance(ts, str) and ts else None


class StatusPage:
    def __init__(self, db: Any, db_path: Path, port: int, env_file: Mapping[str, str] | Any | None = None) -> None:
        self.db, self.db_path, self.port = db, Path(db_path), port
        self.env_file = env_file
        self.started = time.time()
        self._src: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def _lookup_one(self, name: str) -> str:
        before = keystore.timeout_count()
        try:
            where = keys.key_source(name, self.env_file)
        except Exception:
            where = None
        if where:
            return where
        return "store timeout" if keystore.timeout_count() > before else "NOT FOUND"

    def _refresh(self, names: list[str]) -> None:
        try:
            for name in names:
                where = self._lookup_one(name)
                with self._lock:
                    self._src[name] = (time.monotonic(), where)
        finally:
            with self._lock:
                self._worker = None

    def refresh_if_stale(self, names: list[str]) -> None:
        """Start the one background lookup thread for names not looked up within the cache time (never waits)."""
        now = time.monotonic()
        with self._lock:
            if self._worker is not None:
                return
            stale = [n for n in dict.fromkeys(names)
                     if n not in self._src or now - self._src[n][0] > KEY_CACHE_SECONDS]
            if not stale:
                return
            self._worker = threading.Thread(target=self._refresh, args=(stale,), daemon=True)
            self._worker.start()

    def key_where(self, name: str) -> str:
        with self._lock:
            hit = self._src.get(name)
        return hit[1] if hit else "checking"

    def models(self) -> list[dict[str, Any]]:
        out = []
        rows = self.db.models()
        custom = custom_provider_names(self.db)
        self.refresh_if_stale([str(r.get("api_key_env")) for r in rows if r.get("api_key_env")])
        for r in rows:
            try:
                m = map_row(r, custom)
                target, problem = f"{m.provider}/{m.model_id}", None
            except MappingError as e:
                target, problem = str(r.get("provider") or ""), str(e)[:ERROR_CHARS]
            name = str(r.get("api_key_env") or "")
            where = self.key_where(name) if name else "NOT FOUND"
            status = r.get("status") or "unknown"
            out.append({"alias": r["alias"], "target": target, "class": r.get("model_class"),
                        "enabled": bool(r.get("enabled")), "key_name": name, "key_found": where,
                        "health": status, "last_check": _iso_age(r.get("last_check")), "mapping_error": problem,
                        # green: enabled, key found, last health ok; red: enabled but broken; grey: off / not yet known
                        "state": ("off" if not r.get("enabled") else
                                  "bad" if status == "error" or where == "NOT FOUND" or problem else
                                  "off" if where in ("checking", "store timeout") else
                                  "ok" if status == "ok" else "off")})
        return out

    def requests(self) -> list[dict[str, Any]]:
        try:
            rows = self.db.query(
                "SELECT ts, model, upstream_model, route, latency_ms, error FROM billing ORDER BY id DESC LIMIT ?",
                (RECENT,))
        except Exception:  # an old schema without the newer columns
            return []
        return [{"time": r["ts"], "model": r["model"], "upstream": r["upstream_model"], "route": r["route"],
                 "ok": not r["error"], "latency_ms": r["latency_ms"],
                 "error": (str(r["error"])[:ERROR_CHARS] if r["error"] else None)} for r in rows]

    def status(self) -> dict[str, Any]:
        return {"server": {"name": "kiko-lite", "version": getattr(kikolib, "__version__", ""),
                           "uptime_seconds": int(time.time() - self.started), "port": self.port,
                           "db": str(self.db_path)},
                "models": self.models(), "requests": self.requests()}


def install(app: server.App, db: Any, db_path: Path, port: int, env_file: Any | None = None) -> StatusPage:
    """Add ``GET /ui``, ``/ui/`` and ``/ui/api/status`` to ``app`` (the guard exception is ``same_origin_paths``)."""
    page = StatusPage(db, db_path, port, env_file)
    app.same_origin_paths = ("/ui",)

    def html(req: Request) -> StreamResponse:
        _loopback(req)
        return StreamResponse([PAGE.encode("utf-8")], content_type="text/html; charset=utf-8",
                              headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                                  "script-src 'unsafe-inline'; connect-src 'self'; "
                                                                  "frame-ancestors 'none'",
                                       "X-Frame-Options": "DENY",
                                       "X-Content-Type-Options": "nosniff"})

    def api(req: Request) -> JsonResponse:
        _loopback(req)
        return JsonResponse(page.status(), headers={"Cache-Control": "no-store"})

    app.route("GET", "/ui")(html)
    app.route("GET", "/ui/")(html)
    app.route("GET", "/ui/api/status")(api)
    return page


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>kiko-lite status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:14px system-ui,sans-serif;margin:1.2em;background:#111;color:#ddd}
h1{font-size:1.2em} h2{font-size:1em;margin-top:1.5em}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:3px 8px;border-bottom:1px solid #333}
.ok{background:#14532d;color:#bbf7d0} .bad{background:#7f1d1d;color:#fecaca} .off{background:#333;color:#999}
.dot{display:inline-block;width:.8em;height:.8em;border-radius:50%;margin-right:.4em;vertical-align:middle}
#meta,#err{color:#999} #err{color:#f87171} #auth{display:none;margin:.5em 0}
code{color:#93c5fd}
</style></head><body>
<h1>kiko-lite status <small id="meta"></small></h1>
<div id="err"></div>
<div id="auth">Client key: <input id="key" type="password" autocomplete="off"> <button id="go">use</button></div>
<h2>Models</h2>
<table><thead><tr><th></th><th>alias</th><th>provider / model</th><th>class</th><th>enabled</th><th>key name</th>
<th>key found</th><th>health</th><th>last check</th></tr></thead><tbody id="models"></tbody></table>
<h2>Recent requests</h2>
<table><thead><tr><th></th><th>time</th><th>model</th><th>route</th><th>latency</th><th>error</th></tr></thead>
<tbody id="reqs"></tbody></table>
<script>
var key = "", busy = false;
function td(tr, text, cls){var c = document.createElement("td"); c.textContent = text == null ? "" : text;
  if(cls) c.className = cls; tr.appendChild(c); return c;}
function dot(tr, cls){var c = document.createElement("td"), d = document.createElement("span");
  d.className = "dot " + cls; c.appendChild(d); tr.appendChild(c);}
function fill(id, rows, draw){var b = document.getElementById(id); b.textContent = "";
  rows.forEach(function(r){var tr = document.createElement("tr"); draw(tr, r); b.appendChild(tr);});}
function show(s){
  var v = s.server;
  document.getElementById("meta").textContent = v.name + " " + v.version + " | port " + v.port +
    " | up " + v.uptime_seconds + " s | db " + v.db;
  fill("models", s.models, function(tr, m){
    dot(tr, m.state);
    td(tr, m.alias); td(tr, m.target); td(tr, m["class"]); td(tr, m.enabled ? "yes" : "no");
    td(tr, m.key_name); td(tr, m.key_found, m.key_found == "NOT FOUND" ? "bad" : "");
    td(tr, m.mapping_error || m.health, m.health == "ok" ? "ok" : m.health == "error" ? "bad" : "");
    td(tr, m.last_check);});
  fill("reqs", s.requests, function(tr, r){
    dot(tr, r.ok ? "ok" : "bad"); td(tr, r.time); td(tr, r.model + (r.upstream ? " > " + r.upstream : ""));
    td(tr, r.route); td(tr, r.latency_ms == null ? "" : r.latency_ms + " ms"); td(tr, r.error);});
}
function poll(){
  if(busy) return;
  busy = true;
  var h = key ? {"x-api-key": key} : {}, ac = new AbortController(), t = setTimeout(function(){ac.abort();}, 10000);
  fetch("/ui/api/status", {headers: h, cache: "no-store", signal: ac.signal}).then(function(r){
    if(r.status == 401){document.getElementById("auth").style.display = "block"; throw new Error("client key needed");}
    if(!r.ok) throw new Error("HTTP " + r.status);
    document.getElementById("auth").style.display = "none"; return r.json();
  }).then(function(s){document.getElementById("err").textContent = ""; show(s);})
  .catch(function(e){document.getElementById("err").textContent = String(e.message || e);})
  .then(function(){clearTimeout(t); busy = false;});
}
document.getElementById("go").onclick = function(){key = document.getElementById("key").value; poll();};
poll(); setInterval(poll, 5000);
</script></body></html>
"""
