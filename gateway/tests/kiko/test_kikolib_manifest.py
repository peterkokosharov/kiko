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


def test_stray_pyc_is_reported_but_pycache_is_ignored(tmp_path):
    tool = _tool()
    pkg = tmp_path / "kikolib"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"x")
    tool.main(["write", str(pkg)])
    assert tool.check(pkg) == []
    (pkg / "evil.pyc").write_bytes(b"x")
    assert tool.check(pkg) == ["not in manifest: evil.pyc"]
