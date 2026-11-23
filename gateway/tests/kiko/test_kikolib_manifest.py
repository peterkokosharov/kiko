# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""kikolib/SHA256SUMS must match the package (run `python gateway/tools/kikolib_manifest.py write` after edits)."""
import importlib.util
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[2]


def _tool():
    spec = importlib.util.spec_from_file_location("kikolib_manifest", GATEWAY / "tools" / "kikolib_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manifest_is_current():
    assert _tool().check(GATEWAY / "kikolib") == []


def test_version_is_pinned_release():
    import kikolib

    assert kikolib.__version__ == "0.2.0"
    assert "## 0.2.0" in (GATEWAY / "kikolib" / "CHANGES.md").read_text(encoding="utf-8")
