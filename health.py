# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Background health prober: keeps ``models.status`` / ``last_check`` true so the resolver can put models that
work first.

One daemon thread (started by ``serve``) walks the enabled models every ``health_check_interval`` seconds
(setting; default 300, 0 = off) and sends a minimal request (``max_tokens`` 1) through the gateway's own
``probe``: no gate, no billing row, no retry. Probes cost free-tier quota or money and are invisible in billing,
so only models in status error / unknown are probed (never ok ones, never one with a live success within the
interval, never a cooling one, a paid one only after it failed), with a per-model backoff that doubles up to 6 h.
Setting ``health_no_probe`` (comma separated aliases) opts models out. The number of probes sent is logged.
The thread never touches request threads, never logs a key and stops
promptly on ``stop()`` (the ``serve`` shutdown path).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger("kiko_lite.health")

DEFAULT_INTERVAL = 300.0
MIN_INTERVAL = 10.0
MAX_BACKOFF = 6 * 3600.0


def parse_interval(raw: str | None) -> float:
    """Setting text -> seconds: unset = default, 0 = off, a number below ``MIN_INTERVAL`` is raised to it."""
    if raw in (None, ""):
        return DEFAULT_INTERVAL
    try:
        v = float(raw)  # type: ignore[arg-type]
    except ValueError:
        logger.warning("settings.health_check_interval is not a number: using %.0fs", DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL
    if v != v or v <= 0:  # NaN or 0 / negative: off
        return 0.0
    return max(v, MIN_INTERVAL)


class HealthProber:
    """``gateway`` needs ``probe(pname, upstream) -> bool | None`` (None: no verdict) and ``cooldowns`` (``remaining``,
    ``since_ok``, ``clock``); ``catalog`` needs ``snapshot()``."""

    def __init__(self, gateway, catalog, on_status: Callable[[str, bool], None],
                 interval: float | None = None, clock: Callable[[], float] | None = None) -> None:
        self.gateway, self.catalog, self.on_status = gateway, catalog, on_status
        self.interval = interval  # None: read the setting at start()
        self.clock = clock  # None: the gateway's cooldown clock (one clock for passive health and backoff)
        self.last_sent = 0  # probes sent by the last probe_once
        self.sent = 0  # probes sent since start (each one costs quota or money)
        self._next: dict[tuple[str, str], float] = {}  # model -> earliest time of its next probe
        self._delay: dict[tuple[str, str], float] = {}  # model -> current backoff
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _now(self) -> float:
        return (self.clock or self.gateway.cooldowns.clock)()

    @staticmethod
    def _paid(row) -> bool:
        """A nonzero price in the row; NULL / unknown counts as free."""
        return any((row.get(c) or 0) > 0 for c in ("price_in_per_mtok", "price_out_per_mtok"))

    def probe_once(self, interval: float | None = None) -> dict[str, bool]:
        """Probe the models that need it once; {alias: worked} for those that gave a verdict.

        Probes cost quota or money and are invisible in billing, so only status error / unknown models are
        probed: not ok ones, not ones with a live success within ``interval``, not cooling ones, not paid ones
        that never failed (unknown), not aliases listed in setting ``health_no_probe``; a failing model backs
        off (interval, doubled per failed probe, up to ``MAX_BACKOFF``) and a success resets that."""
        interval = interval or self.interval or DEFAULT_INTERVAL
        snap = self.catalog.snapshot()
        skip = {a.strip() for a in (snap.settings.get("health_no_probe") or "").split(",") if a.strip()}
        now = self._now()
        out: dict[str, bool] = {}
        sent = 0
        for info in snap.models:
            if self._stop.is_set():
                break
            if info.mapped is None or not info.row["enabled"] or info.row["alias"] in skip:
                continue
            key = (info.pname, info.mapped.model_id)
            st = info.row["status"]
            if st == "ok":
                self._next.pop(key, None)
                self._delay.pop(key, None)
                continue
            if st == "unknown" and self._paid(info.row):
                continue  # a paid model is probed only after a failure
            since = self.gateway.cooldowns.since_ok(key)
            if since is not None and since < interval:
                continue  # passive health: it worked live a moment ago
            if self.gateway.cooldowns.remaining(key) or now < self._next.get(key, 0.0):
                continue
            sent += 1
            self.sent += 1
            try:
                ok = self.gateway.probe(*key)
            except Exception:  # a probe must never kill the thread
                logger.error("probe of %s failed unexpectedly", info.row["alias"], exc_info=True)
                ok = None  # no verdict: no status write, back off only
            if ok:
                self._next.pop(key, None)
                self._delay.pop(key, None)
            else:
                d = self._delay.get(key)
                d = interval if d is None else min(d * 2, MAX_BACKOFF)
                self._delay[key] = d
                self._next[key] = now + d
            if ok is None:
                continue  # an answer that says nothing about the model: status unchanged
            out[info.row["alias"]] = ok
            try:
                self.on_status(info.pname, ok)
            except Exception:
                logger.error("status write for %s failed", info.row["alias"], exc_info=True)
        self.last_sent = sent
        return out

    def _run(self, interval: float) -> None:
        while not self._stop.is_set():
            res = self.probe_once(interval)
            logger.info("health probe: %d sent, %d ok, %d failing (%d sent in total)", self.last_sent,
                        sum(res.values()), len(res) - sum(res.values()), self.sent)
            if self._stop.wait(interval):
                return

    def start(self) -> bool:
        """Start the thread; False when off (interval 0) or already running."""
        if self._thread is not None:
            return False
        interval = self.interval
        if interval is None:
            try:
                interval = parse_interval(self.catalog.snapshot().settings.get("health_check_interval"))
            except Exception:
                logger.error("cannot read health_check_interval", exc_info=True)
                interval = DEFAULT_INTERVAL
        if interval <= 0:
            return False
        self._thread = threading.Thread(target=self._run, args=(interval,), name="kiko-health", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the thread to stop and wait a little (a probe in flight is abandoned; the thread is a daemon)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
