# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""conftest.py of the kiko-lite zip (the bundle tool ships this file as tests/kiko/conftest.py).

Home isolation: runs at conftest import, before any kiko module is imported, so no test can resolve the real
~/.kiko/kiko.db. Also an empty KIKO_CONFIG_DIR per test (as the repo conftest does).
Standard library only (plus pytest); the repo's own tests/kiko/conftest.py also holds litellm fixtures.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="kiko_test_home_"))
atexit.register(shutil.rmtree, _ISOLATED_HOME, True)
_ISOLATED_DB = _ISOLATED_HOME / ".kiko" / "kiko.db"
_ISOLATED_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_ISOLATED_HOME)
os.environ["USERPROFILE"] = str(_ISOLATED_HOME)
os.environ["KIKO_DB_PATH"] = str(_ISOLATED_DB)


@pytest.fixture(autouse=True)
def _isolate_config_dir(monkeypatch, tmp_path_factory):
    """Every test gets its own empty KIKO_CONFIG_DIR (kiko.toml, .env, logs): none reads or writes a real one."""
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path_factory.mktemp("kiko_cfg")))
