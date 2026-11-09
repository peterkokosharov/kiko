# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite must stay stdlib + kikolib + kiko_lite only (never litellm) and parse on Python 3.11."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "kiko_lite"
FILES = sorted(ROOT.rglob("*.py"))


def test_files_exist():
    assert FILES


def test_parse_py311():
    for f in FILES:
        ast.parse(f.read_text(encoding="utf-8"), filename=str(f), feature_version=(3, 11))


def test_imports_allowed():
    allowed = set(sys.stdlib_module_names) | {"kikolib", "kiko_lite"}
    for f in FILES:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            for m in mods:
                assert m.split(".")[0] in allowed, f"{f}: forbidden import {m}"
