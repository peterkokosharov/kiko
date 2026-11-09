# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Guard (TASK #124): production code takes ports and paths only from kikolib.config. This greps kiko_lite, kikolib,
web, web-ui, tools and dist for literal ports (4000, 4001, 4002, 4008, 4080) and for the path names ``.kiko``,
``kiko.db`` and ``kiko.toml`` outside kikolib/config.py, and fails on any hit.

Not scanned: tests, docs, comments and docstrings (they describe, they do not decide), the legacy LiteLLM stack
(litellm/, kiko_build.py, kiko.service, install.sh, deb/, kiko-config.yaml, kiko-sample.yaml, dist/kiko_start.py and
the dist zips/yaml), kiko-tray (another repo) and tools/kiko_lite_tests_conftest.py (the bundle's test support: it
builds a fake home for the tests, it is not production code).
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("kiko_lite", "kikolib", "web", "web-ui", "tools", "dist")
EXEMPT = {"kikolib/config.py", "tools/kiko_lite_tests_conftest.py", "dist/kiko_start.py",
          "dist/start.bat", "dist/install.bat"}  # the last two: untracked (dist/ is ignored) legacy kiko-full scripts
SCRIPT_SUFFIXES = {".sh", ".bat", ".cmd", ".service", ".js", ".html"}
PORTS = re.compile(r"(?<![\w.])(4000|4001|4002|4008|4080)(?![\w])")
PATHS = re.compile(r"\.kiko\b|kiko\.db\b|kiko\.toml\b")
PORT_INTS = {4000, 4001, 4002, 4008, 4080}
COMMENT_PREFIXES = ("#", "rem ", "REM ", "::", "//", "*", "/*")


def files():
    for d in DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if not p.is_file() or "__pycache__" in p.parts or rel in EXEMPT or "/static/tests/" in rel:
                continue
            if p.suffix == ".py" or p.suffix in SCRIPT_SUFFIXES:
                yield p, rel


def py_hits(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        v = node.value
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v in PORT_INTS:
            hits.append(f"{rel}:{node.lineno}: port {v}")
        elif isinstance(v, str):
            m = PORTS.search(v) or PATHS.search(v)
            if m:
                hits.append(f"{rel}:{node.lineno}: {m.group(0)!r} in a string")
    return hits


def script_hits(path: Path, rel: str) -> list[str]:
    hits = []
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.strip().startswith(COMMENT_PREFIXES):
            continue
        m = PORTS.search(line) or PATHS.search(line)
        if m:
            hits.append(f"{rel}:{n}: {m.group(0)!r}")
    return hits


def test_no_literal_port_or_config_path_outside_kikolib_config():
    hits, seen = [], 0
    for path, rel in files():
        seen += 1
        hits += py_hits(path, rel) if path.suffix == ".py" else script_hits(path, rel)
    assert seen > 40  # the walk really found the code
    assert not hits, "literal ports / config paths (take them from kikolib.config):\n" + "\n".join(hits)


def test_the_guard_itself_catches_hits(tmp_path):
    bad = tmp_path / "x.py"
    bad.write_text('"""doc 4000 .kiko"""\nPORT = 4000\nP = "~/.kiko/x"\nH = f"http://h:{p}/kiko.toml"\n# 4001\n', encoding="utf-8")
    assert len(py_hits(bad, "x.py")) == 3  # the docstring and the comment are fine
    sh = tmp_path / "x.sh"
    sh.write_text('# 4000\nrem 4001\necho 4000\nd=$HOME/.kiko\nok=14000\n', encoding="utf-8")
    assert len(script_hits(sh, "x.sh")) == 2


def test_config_py_is_the_one_place_that_names_them():
    text = (ROOT / "kikolib" / "config.py").read_text(encoding="utf-8")
    for needle in ("4000", '".kiko"', '"kiko.db"', '"kiko.toml"'):
        assert needle in text
