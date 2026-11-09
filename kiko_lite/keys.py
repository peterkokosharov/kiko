# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Where an API key is found (names only, never values); shared by the CLI and the web-ui."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from kikolib import config as kconfig
from kikolib import dotenv, keystore

logger = logging.getLogger("kiko_lite.keys")


_warned: set[str] = set()


def env_file_path(db_path: Path | None = None) -> Path:
    """The ``.env`` file: ``<config dir>/.env``. Fallback for an older install: when that file is missing and the
    database folder has one (the old place), that one is read and a single warning is logged."""
    primary = kconfig.env_path()
    if db_path is not None:
        legacy = Path(db_path).parent / kconfig.ENV_NAME
        if legacy != primary and not primary.is_file() and legacy.is_file():
            if str(legacy) not in _warned:
                _warned.add(str(legacy))
                logger.warning("reading API keys from the old place %s; move it to %s", legacy, primary)
            return legacy
    return primary


def dotenv_values(db_path: Path | None = None) -> dict[str, str]:
    """Values of the ``.env`` file (see ``env_file_path``; only there, never the current folder)."""
    try:
        return dotenv.parse(env_file_path(db_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return {}


class WatchedEnv:
    """The ``.env`` of the config dir, re-read when it changes, plus the old place (next to the database) as a
    fallback for keys the new file does not have; the first value taken from the old place logs one warning."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.primary = dotenv.WatchedFile(kconfig.env_path())
        legacy = Path(db_path).parent / kconfig.ENV_NAME if db_path is not None else None
        self.legacy = dotenv.WatchedFile(legacy) if legacy is not None and legacy != kconfig.env_path() else None
        self._legacy_path = legacy

    @property
    def ttl(self) -> float:
        return self.primary.ttl

    @ttl.setter
    def ttl(self, seconds: float) -> None:  # how often the files are re-checked (tests set 0)
        self.primary.ttl = seconds
        if self.legacy is not None:
            self.legacy.ttl = seconds

    def get(self, name: str) -> str | None:
        value = self.primary.get(name)
        if value or self.legacy is None:
            return value
        value = self.legacy.get(name)
        if value and str(self._legacy_path) not in _warned:
            _warned.add(str(self._legacy_path))
            logger.warning("reading API keys from the old place %s; move it to %s", self._legacy_path,
                           kconfig.env_path())
        return value

    def version(self) -> int:
        return self.primary.version() + (self.legacy.version() if self.legacy is not None else 0)


def key_source(name: str, env_file: Mapping[str, str] | None = None) -> str | None:
    """Where the key ``name`` is found: "env", "store" or "dotenv" (that order); never the value."""
    if os.environ.get(name) or os.environ.get(name.upper()):
        return "env"
    if keystore.get_secret(name, env={}):
        return "store"
    if env_file and env_file.get(name):
        return "dotenv"
    return None
