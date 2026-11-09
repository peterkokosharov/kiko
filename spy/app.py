# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The spy: polls the kiko log file and the kiko database (read-only), keeps the newest lines in a ``Hub`` and
serves them as a page, an SSE stream (``/events``, primary) and a WebSocket (``/ws``, secondary).

Read-only and decoupled: no ingest endpoint, nothing here writes the database or the log, and the proxy does not
know the spy exists. The browser guard of ``kikolib.server`` is on (Host allow-list, no foreign Origin)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from kikolib import config as kconfig
from kikolib import server as kserver
from kikolib import sse, websocket

from . import ui
from .ring import Hub
from .sources import DbPoll, LogTail

logger = logging.getLogger("kiko.spy")

POLL_SECONDS = 0.5
PING_SECONDS = 15.0
SEC_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _event(seq: int, channel: str, text: str) -> dict[str, object]:
    return {"seq": seq, "ch": channel, "text": text}


class Spy:
    """Owns the hub, the two sources and the polling thread. ``poll_once`` is what the thread runs (tests call it)."""

    def __init__(self, log_path: Callable[[], Path | None], db_path: Callable[[], Path], maxlen: int = 1000,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self.hub = Hub(("server", "llm"), maxlen)
        self.tail = LogTail(log_path)
        self.db = DbPoll(db_path)
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> int:
        n = 0
        for channel, source in (("server", self.tail), ("llm", self.db)):
            try:
                lines = source.poll()
            except Exception:  # a broken source must not kill the viewer
                logger.debug("spy: %s poll failed", channel, exc_info=True)
                continue
            for line in lines:
                self.hub.publish(channel, line)
                n += 1
        return n

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kiko-spy-poll", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_seconds)

    def stop(self) -> None:
        self._stop.set()
        self.hub.close()
        if self._thread is not None:
            self._thread.join(2.0)

    # --- HTTP ---
    def _after(self, req: kserver.Request) -> int:
        raw = req.headers.get("last-event-id") or req.query.get("after") or "0"
        try:
            after = max(0, int(raw))
        except ValueError:
            after = 0
        return 0 if after > self.hub.seq else after  # a client from before a spy restart starts over

    def _stream(self, after: int) -> Iterator[bytes]:
        yield b"retry: 3000\n\n"
        last_sent = time.monotonic()
        while not self.hub.closed:
            items = self.hub.wait(after, 1.0)
            for seq, ch, text in items:
                after = seq
                yield b"id: %d\n" % seq + sse.encode("line", _event(seq, ch, text))
                last_sent = time.monotonic()
            if not items and time.monotonic() - last_sent >= PING_SECONDS:
                yield sse.ping()
                last_sent = time.monotonic()

    def _ws(self, ws: websocket.WebSocket, req: kserver.Request) -> None:
        after = self._after(req)
        done = threading.Event()

        def pump() -> None:
            nonlocal after
            try:
                while not done.is_set() and not ws.closed and not self.hub.closed:
                    for seq, ch, text in self.hub.wait(after, 1.0):
                        after = seq
                        ws.send_text(json.dumps(_event(seq, ch, text), separators=(",", ":")))
            except (websocket.WebSocketClosed, OSError):
                pass

        sender = threading.Thread(target=pump, name="kiko-spy-ws", daemon=True)
        sender.start()
        try:
            while ws.recv() is not None:  # the client sends nothing we act on; recv drives pings and close
                pass
        finally:
            done.set()
            sender.join(2.0)

    def build_app(self) -> kserver.App:
        app = kserver.App(max_body=1024)
        app.browser_guard = True
        app.allow_same_origin = True  # the page's own Origin (http://<Host>) is fine; any other is a 403

        def page(body: str, ctype: str) -> kserver.StreamResponse:
            return kserver.StreamResponse([body.encode("utf-8")], headers=SEC_HEADERS, content_type=ctype)

        @app.route("GET", "/")
        def index(req: kserver.Request) -> kserver.StreamResponse:
            return page(ui.HTML, "text/html; charset=utf-8")

        @app.route("GET", "/app.js")
        def script(req: kserver.Request) -> kserver.StreamResponse:
            return page(ui.JS, "text/javascript; charset=utf-8")

        @app.route("GET", "/app.css")
        def style(req: kserver.Request) -> kserver.StreamResponse:
            return page(ui.CSS, "text/css; charset=utf-8")

        @app.route("GET", "/events")
        def events(req: kserver.Request) -> kserver.StreamResponse:
            return kserver.StreamResponse(self._stream(self._after(req)), headers=SEC_HEADERS)

        @websocket.route(app, "/ws", max_message=4096)
        def ws_route(ws: websocket.WebSocket, req: kserver.Request) -> None:
            self._ws(ws, req)

        return app


def default_spy() -> Spy:
    """A spy on the configured log file and database (paths are resolved on every poll, so a changed
    ``kiko.toml`` is picked up)."""
    return Spy(kconfig.log_file, kconfig.database_path)
