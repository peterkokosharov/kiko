# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The two ``[logs]`` settings of kiko.toml: a log file and the retention of old request logs.

``setup_logging`` is the one place that configures logging for the proxy and the web (the console handler plus the
optional file handler of ``[logs] file``). ``Pruner`` deletes old rows now and then (``[logs] retention_days``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from . import config

FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
DAY = 86400.0
logger = logging.getLogger("kikolib.logs")


def add_file_handler(path: Path, root: logging.Logger | None = None) -> logging.Handler | None:
    """Add a UTF-8 ``FileHandler`` for ``path`` (parent folders are created) unless one for it exists; None when
    the file cannot be opened (a warning is logged; a bad log path never stops a server)."""
    root = root or logging.getLogger()
    target = str(path.resolve())
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and h.baseFilename == target:
            return h
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        logger.warning("cannot open the log file %s: %s", path, type(e).__name__)
        return None
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    return handler


def setup_logging(level: int = logging.INFO) -> Path | None:
    """Console logging, plus the file of ``[logs] file`` when set. Returns the file path used, or None."""
    logging.basicConfig(level=level, format=FORMAT)
    path = config.log_file()
    if path is not None and add_file_handler(path) is not None:
        return path
    return None


class Pruner:
    """Calls ``prune(days) -> deleted rows`` when started and then once a day, for as long as ``days()`` > 0.

    ``days`` is read each time, so a change of ``[logs] retention_days`` applies at the next run."""

    def __init__(self, prune: Callable[[int], int], days: Callable[[], int], interval: float = DAY) -> None:
        self.prune, self.days, self.interval = prune, days, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        try:
            days = int(self.days())
            if days <= 0:
                return 0
            deleted = int(self.prune(days))
            if deleted:
                logger.info("log retention: deleted %d row(s) older than %d day(s)", deleted, days)
            return deleted
        except Exception:
            logger.error("log retention failed", exc_info=True)
            return 0

    def _run(self) -> None:
        while True:
            self.run_once()
            if self._stop.wait(self.interval):
                return

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kiko-prune", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
