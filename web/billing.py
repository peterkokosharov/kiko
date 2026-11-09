# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Billing summary, request log (with an optional live view) and the text log of stored responses."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from urllib.parse import quote

from . import render as r
from .security import COOKIE, SEC_HEADERS, cookie_value
from .ui import Response, int_arg

LOG_COLS = "id, model, tokens_in, tokens_out, cost_usd, ts, route, attempts, latency_ms, upstream_model, error"
PREVIEW = 160  # characters of the prompt / answer shown per row of the Text log
DAY_CHOICES = (1, 7, 30, 90, 365)
PING_EVERY = 15  # seconds between SSE keep-alive comments
STREAM_MAX = 600.0  # a stream ends after this many seconds; the browser reconnects by itself


def _count(db, table: str) -> int:
    # `table` is always a constant of this module
    return int(db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


def _page(db, table: str, cols: str, order: str, req) -> tuple[list[dict], int, int, int]:
    limit = int_arg(req.query, "limit", 30, 1, 200)
    offset = int_arg(req.query, "offset", 0, 0, 10**9)
    rows = db.query(f"SELECT {cols} FROM {table} ORDER BY {order} DESC LIMIT ? OFFSET ?", (limit, offset))
    return rows, offset, limit, _count(db, table)


def log_line(row: dict) -> str:
    """One request as plain text (used by the live view; the browser shows it with textContent)."""
    err = f" ERROR {row['error']}" if row.get("error") else ""
    return (f"{row['ts']} {row['model']} in={row['tokens_in']} out={row['tokens_out']} "
            f"${row['cost_usd'] or 0:.4f} {row.get('latency_ms') or '-'}ms{err}")


def log_stream(db, poll: float = 1.0, alive: Callable[[], bool] | None = None,
               max_seconds: float = STREAM_MAX) -> Iterator[str]:
    """SSE frames for billing rows added after the connection was made. Ends when the client goes away, when
    ``alive()`` turns false (session ended; checked on every poll) or after ``max_seconds``."""
    last = db.query("SELECT COALESCE(MAX(id), 0) AS m FROM billing")[0]["m"]
    yield ": connected\n\n"
    idle = 0.0
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline and (alive is None or alive()):
        rows = db.query(f"SELECT {LOG_COLS} FROM billing WHERE id > ? ORDER BY id LIMIT 100", (last,))
        for row in rows:
            last = row["id"]
            yield f"data: {json.dumps(log_line(row))}\n\n"
        if rows:
            idle = 0.0
            continue
        time.sleep(poll)
        idle += poll
        if idle >= PING_EVERY:
            idle = 0.0
            yield ": ping\n\n"


def register(ui) -> None:  # ui: web.ui.WebUI
    @ui.get("/billing")
    def billing(req, sess):
        days = int_arg(req.query, "days", 30, 1, 3660)
        rows = ui.db.billing_summary(days)
        total = sum(x["cost_usd"] or 0 for x in rows)
        probes, probe_cost = ui.db.probe_summary(days)
        choices = r.join([r.link(f"{d} d", r.url("/billing", days=d)) for d in DAY_CHOICES], " | ")
        body = r.join([
            r.tag("p", "Period: ", choices),
            r.table(["Model", "Calls", "Tokens in", "Tokens out", "Cost USD", "Errors"],
                    [[x["model"], x["calls"], x["tokens_in"], x["tokens_out"], f"{x['cost_usd'] or 0:.4f}",
                      x["errors"]] for x in rows]),
            r.tag("p", r.tag("strong", f"Total: ${total:.4f}"), f" over {days} days; health probes: "
                  f"{probes} calls, ${probe_cost:.4f}")])
        return ui.render(sess, "Billing", body, "/billing")

    @ui.get("/logs")
    def logs(req, sess):
        rows, offset, limit, total = _page(ui.db, "billing", LOG_COLS, "ts", req)
        table = r.table(["Time", "Model", "Upstream", "In", "Out", "Cost USD", "Route", "Tries", "ms", "Error"],
                        [[x["ts"], x["model"], x["upstream_model"], x["tokens_in"], x["tokens_out"],
                          f"{x['cost_usd'] or 0:.4f}", x["route"], x["attempts"], x["latency_ms"], x["error"]]
                         for x in rows])
        live = r.tag("details", r.tag("summary", "Live view (needs JavaScript)"),
                     r.tag("pre", "", id="live", data_src="/logs/stream"))
        body = r.join([live, table, r.pager("/logs", offset, limit, total)])
        return ui.render(sess, "Request log", body, "/logs", script="live.js")

    @ui.get("/logs/stream")
    def logs_stream(req, sess):
        token = cookie_value(req, COOKIE)
        return Response(log_stream(ui.db, alive=lambda: ui.auth.valid(token)), 200, SEC_HEADERS, "text/event-stream")

    @ui.get("/responses")
    def responses(req, sess):
        limit = int_arg(req.query, "limit", 30, 1, 200)
        offset = int_arg(req.query, "offset", 0, 0, 10**9)
        rows = ui.db.query(
            "SELECT r.id, r.input_json, r.output_json, r.created_at, b.upstream_model, b.error FROM responses r"
            " LEFT JOIN billing b ON b.request_id = r.id ORDER BY r.created_at DESC, r.rowid DESC LIMIT ? OFFSET ?",
            (limit, offset))
        table = r.table(["Time", "Model", "Upstream", "Status", "Prompt", "Answer", ""],
                        [[x["created_at"], request_model(x["input_json"]), x["upstream_model"], status_of(x),
                          r.tag("pre", preview(last_prompt(x["input_json"]))),
                          r.tag("pre", preview(response_text(x["output_json"]))),
                          r.link("Open", "/responses/" + quote(x["id"], safe=""))] for x in rows])
        return ui.render(sess, "Text log (stored requests and answers)",
                         r.join([table, r.pager("/responses", offset, limit, _count(ui.db, "responses"))]),
                         "/responses")

    @ui.get("/responses/{id}")
    def response_detail(req, sess):
        rows = ui.db.query(
            "SELECT r.id, r.input_json, r.output_json, r.created_at, b.model, b.upstream_model, b.error, b.tokens_in,"
            " b.tokens_out, b.latency_ms FROM responses r LEFT JOIN billing b ON b.request_id = r.id"
            " WHERE r.id = ?", (req.params.get("id", ""),))
        if not rows:
            return ui.error(404, "No such stored request.")
        x = rows[0]
        facts = [("Id", x["id"]), ("Time (UTC)", x["created_at"]), ("Model", request_model(x["input_json"])),
                 ("Upstream", x["upstream_model"]), ("Status", status_of(x)), ("Tokens in", x["tokens_in"]),
                 ("Tokens out", x["tokens_out"]), ("Latency ms", x["latency_ms"])]
        body = r.join([
            r.tag("p", r.link("Back to the Text log", "/responses")),
            r.table(["Field", "Value"], [[k, v] for k, v in facts if v not in (None, "")]),
            r.tag("h2", "Prompt"), r.tag("pre", request_text(x["input_json"])),
            r.tag("h2", "Answer"), r.tag("pre", response_text(x["output_json"]))])
        return ui.render(sess, "Stored request", body, "/responses")


def _load(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None


def _blocks_text(content) -> str:
    """A message content (string or list of blocks) as plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False)
    out = []
    for b in content:
        if not isinstance(b, dict):
            out.append(str(b))
        elif b.get("type") == "text":
            out.append(str(b.get("text", "")))
        elif b.get("type") == "thinking":
            out.append("[thinking] " + str(b.get("thinking", "")))
        elif b.get("type") == "tool_use":
            out.append(f"[tool_use {b.get('name', '')}] " + json.dumps(b.get("input"), ensure_ascii=False))
        elif b.get("type") == "tool_result":
            out.append("[tool_result] " + _blocks_text(b.get("content")))
        else:
            out.append(json.dumps(b, ensure_ascii=False))
    return "\n".join(out)


def request_model(raw: str) -> str:
    data = _load(raw)
    return str(data.get("model", "")) if isinstance(data, dict) else ""


def request_text(raw: str) -> str:
    """The stored request as readable text: params, system, tools (names), then every message. Text that is not a
    kiko request body (kiko-full rows) is shown as it is."""
    data = _load(raw)
    if not isinstance(data, dict) or "messages" not in data:
        return raw or ""
    parts = []
    params = {k: v for k, v in data.items() if k not in ("messages", "system", "tools")}
    parts.append("Parameters: " + json.dumps(params, ensure_ascii=False))
    if data.get("system"):
        parts.append("System:\n" + _blocks_text(data["system"]))
    if data.get("tools"):
        names = [str(t.get("name", "")) if isinstance(t, dict) else str(t) for t in data["tools"]]
        parts.append("Tools: " + ", ".join(names))
    msgs = data["messages"] if isinstance(data["messages"], list) else []
    for m in msgs:
        role = m.get("role", "?") if isinstance(m, dict) else "?"
        parts.append(f"[{role}]\n" + _blocks_text(m.get("content") if isinstance(m, dict) else m))
    return "\n\n".join(parts)


def response_text(raw: str) -> str:
    data = _load(raw)
    if not isinstance(data, dict):
        return raw or ""
    if data.get("type") == "error" and isinstance(data.get("error"), dict):
        return f"ERROR {data['error'].get('type', '')}: {data['error'].get('message', '')}"
    if "content" not in data:
        return raw or ""
    text = _blocks_text(data["content"])
    if isinstance(data.get("error"), dict):  # a stream that failed part-way: the answer so far, then the error
        text += f"\n\nERROR {data['error'].get('type', '')}: {data['error'].get('message', '')}"
    return text


def last_prompt(raw: str) -> str:
    """The last user message of a stored request, for the list preview."""
    data = _load(raw)
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return raw or ""
    for m in reversed(data["messages"]):
        if isinstance(m, dict) and m.get("role") == "user":
            return _blocks_text(m.get("content"))
    return ""


def preview(text: str, n: int = PREVIEW) -> str:
    text = " ".join((text or "").split())
    return text[:n] + (" ..." if len(text) > n else "")


def status_of(row: dict) -> str:
    if row.get("error"):
        return str(row["error"])
    out = _load(row.get("output_json") or "")
    if isinstance(out, dict) and (out.get("type") == "error" or isinstance(out.get("error"), dict)):
        return "error"
    return "ok"
