# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Bounded in-memory event buffers for the spy: ``RingBuffer`` (one channel) and ``Hub`` (two channels, one
global sequence, one wake-up condition for the SSE and WebSocket readers)."""

from __future__ import annotations

import threading
from collections import deque

Item = tuple[int, str, str]  # (seq, channel, text)


class RingBuffer:
    """Keeps the newest ``maxlen`` items; older ones fall off. Thread-safe."""

    def __init__(self, maxlen: int) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be at least 1")
        self._items: deque[Item] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, item: Item) -> None:
        with self._lock:
            self._items.append(item)

    def items(self, after: int = 0) -> list[Item]:
        """Items with seq greater than ``after``, oldest first."""
        with self._lock:
            return [i for i in self._items if i[0] > after]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class Hub:
    """One ``RingBuffer`` per channel; ``publish`` numbers events 1, 2, 3 ... across all channels."""

    def __init__(self, channels: tuple[str, ...] = ("server", "llm"), maxlen: int = 1000) -> None:
        self.rings = {c: RingBuffer(maxlen) for c in channels}
        self._cond = threading.Condition()
        self._seq = 0
        self._closed = False

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, channel: str, text: str) -> int:
        ring = self.rings[channel]  # KeyError on a typo: a bug, not data
        with self._cond:
            self._seq += 1
            seq = self._seq
            ring.append((seq, channel, text))
            self._cond.notify_all()
        return seq

    def since(self, after: int = 0) -> list[Item]:
        """All buffered events with seq > ``after`` from every channel, in order."""
        out = [i for ring in self.rings.values() for i in ring.items(after)]
        out.sort(key=lambda i: i[0])
        return out

    def wait(self, after: int, timeout: float) -> list[Item]:
        """Events newer than ``after``; blocks up to ``timeout`` seconds when there are none (or until closed)."""
        with self._cond:
            if self._seq <= after and not self._closed:
                self._cond.wait(timeout)
        return self.since(after)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
