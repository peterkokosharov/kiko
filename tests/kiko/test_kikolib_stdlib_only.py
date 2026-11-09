# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""
Guard: kikolib must depend on the Python standard library only.

Tests:
  S1 — every top-level import in kikolib/*.py is stdlib (or kikolib itself)
"""
import ast
import sys
from pathlib import Path

KIKOLIB = Path(__file__).resolve().parents[2] / "kikolib"


def _top_level_modules(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_kikolib_imports_stdlib_only():
    files = sorted(f for f in KIKOLIB.rglob("*.py") if "__pycache__" not in f.parts)
    assert files, "no kikolib/*.py found"
    allowed = set(sys.stdlib_module_names) | {"kikolib"}
    bad = {
        str(f.relative_to(KIKOLIB)): sorted(m for m in _top_level_modules(f) if m not in allowed)
        for f in files
    }
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"non-stdlib imports in kikolib: {bad}"


def test_webui_imports_stdlib_only():
    files = sorted((KIKOLIB.parent / "web-ui").glob("*.py"))
    assert files, "no web-ui/*.py found"
    allowed = set(sys.stdlib_module_names) | {"kikolib", "kiko_lite"}
    bad = {f.name: sorted(m for m in _top_level_modules(f) if m not in allowed) for f in files}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"non-stdlib imports in web-ui: {bad}"
