# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""Server-Sent Events: encode events for the client, parse upstream streams.

``encode`` / ``ping`` build the bytes kiko-lite sends to Claude Code (Anthropic style).
``Parser`` / ``iter_events`` read an upstream SSE stream (OpenAI / Gemini) line by line,
following the WHATWG SSE rules; ``data: [DONE]`` is passed through as ordinary data.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


def encode(event: str | None, data: Any) -> bytes:
    """One SSE frame. dict/list data is compact JSON; a multi-line str gives several data lines."""
    if isinstance(data, (dict, list)):
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    elif isinstance(data, bytes):
        text = data.decode("utf-8")
    else:
        text = str(data)
    out = []
    if event:
        out.append(f"event: {event.replace(chr(10), ' ').replace(chr(13), ' ')}\n")
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        out.append(f"data: {line}\n")
    out.append("\n")
    return "".join(out).encode("utf-8")


def ping() -> bytes:
    """Anthropic-style keep-alive frame."""
    return encode("ping", {"type": "ping"})


@dataclass(frozen=True)
class Event:
    """A dispatched SSE event. ``event`` is None when the stream gave no event name."""

    event: str | None
    data: str
    id: str | None = None


class Parser:
    """Incremental parser: ``feed`` one line at a time, ``finish`` at end of stream."""

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []
        self._id: str | None = None

    def feed(self, line: str) -> Event | None:
        """Feed one line (trailing CR/LF is ignored). Returns an Event on a blank line."""
        line = line.rstrip("\r\n")
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            if "\0" not in value:
                self._id = value
        return None

    def finish(self) -> Event | None:
        """End of stream: dispatch a final event that had no trailing blank line."""
        return self._dispatch()

    def _dispatch(self) -> Event | None:
        ev, data = self._event, self._data
        self._event, self._data = None, []
        if not data:
            return None
        return Event(ev or None, "\n".join(data), self._id)


def iter_events(lines: Iterable[str]) -> Iterator[Event]:
    """Yield events from an iterable of lines (e.g. ``Response.iter_lines()``)."""
    p = Parser()
    for line in lines:
        ev = p.feed(line)
        if ev is not None:
            yield ev
    ev = p.finish()
    if ev is not None:
        yield ev
