#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Build the portable kiko-lite zip (stdlib only).

Usage, from anywhere:

    python tools/kiko_lite_bundle.py [--out DIR]       # default DIR = <repo>/dist

Writes ``kiko-lite-<short git sha, else YYYYMMDD>.zip`` holding ONLY ``kikolib/``, ``kiko_lite/`` (no
``__pycache__``, no tests inside them), ``tools/linux_keystore_check.py``, ``docs/LINUX-KEYSTORE-TEST.md``,
``docs/LINUX-INSTALL.md``, ``docs/GITHUB-DEPLOY.md`` (never a Bulgarian ``.bg.`` or ``bg/`` file), ``kiko-lite.service`` and
``install-service.sh`` (systemd user service, from ``tools/``), the kiko-lite unit tests
(every ``tests/kiko/test_kiko_lite_*.py`` except the bundle test itself, plus ``conftest.py``), ``README.md`` (from
``docs/KIKO-LITE-README.md``), ``LICENSE`` (from ``docs/KIKO-LITE-LICENSE``; the repo LICENSE also covers
kiko-full), a ``VERSION`` file ("kiko-lite <sha> <commit date>") and the launchers ``kiko-lite`` (sh) and
``kiko-lite.cmd`` (Windows). The file list is sorted and the zip timestamps are fixed, so the same commit gives
the same zip. The file list and all file bytes come from git HEAD (``git ls-tree``, ``git show``), not from the
working tree, so CRLF checkouts cannot leak into the zip. Refuses to build when a bundled path (or an untracked
file inside a bundled package) differs from HEAD, listing the paths. Refuses to build when any bundled ``.py``
file imports litellm. Prints the zip path and the file count, never any secret.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("kikolib", "kiko_lite")
SINGLE_FILES = (
    "tools/linux_keystore_check.py",
    "docs/LINUX-KEYSTORE-TEST.md",
    "docs/LINUX-INSTALL.md",
    "docs/GITHUB-DEPLOY.md",
)
# repo path -> path inside the zip (the repo README and LICENSE belong to the LiteLLM fork, so kiko-lite has its own)
RENAMED = {
    "docs/KIKO-LITE-README.md": "README.md",
    "docs/KIKO-LITE-LICENSE": "LICENSE",
    "tools/kiko-lite.service": "kiko-lite.service",  # systemd user unit template + installer (zip root)
    "tools/install-service.sh": "install-service.sh",
    # the repo's tests/kiko/conftest.py holds litellm fixtures; the zip gets a stdlib-only one
    "tools/kiko_lite_tests_conftest.py": "tests/kiko/conftest.py",
}
# unit tests shipped in the zip: all test_kiko_lite_* files except the bundle test (they need the repo's git)
TEST_EXTRA = ("tests/__init__.py", "tests/kiko/__init__.py")
TEST_EXCLUDE = ("tests/kiko/test_kiko_lite_bundle.py",)
WRAPPER_NAME = "kiko-lite"
WRAPPER = (
    "#!/bin/sh\n"
    "# kiko-lite launcher: run from anywhere (symlinks are followed), no install needed.\n"
    'me="$0"\n'
    'if real=$(readlink -f "$me" 2>/dev/null) && [ -n "$real" ]; then me="$real"; fi\n'
    'here=$(CDPATH= cd -- "$(dirname -- "$me")" && pwd)\n'
    'if command -v python3 >/dev/null 2>&1; then py=python3; else py=python; fi\n'
    'exec env PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" "$py" -m kiko_lite "$@"\n'
)
WRAPPER_CMD_NAME = "kiko-lite.cmd"
WRAPPER_CMD = (
    "@echo off\r\n"
    "rem kiko-lite launcher for Windows: py -3 if available, else python. No install needed.\r\n"
    "setlocal\r\n"
    'if defined PYTHONPATH (set "PYTHONPATH=%~dp0;%PYTHONPATH%") else set "PYTHONPATH=%~dp0"\r\n'
    "where py >nul 2>nul\r\n"
    "if errorlevel 1 goto nopy\r\n"
    "py -3 -m kiko_lite %*\r\n"
    "exit /b\r\n"
    ":nopy\r\n"
    "python -m kiko_lite %*\r\n"
    "exit /b\r\n"
)
_FIXED_TIME = (2026, 1, 1, 0, 0, 0)
_SKIP_DIRS = {"__pycache__", "tests"}


class BundleError(Exception):
    pass


def _git(root: Path, *args: str) -> bytes:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise BundleError(f"cannot run git: {e}") from e
    if r.returncode != 0:
        raise BundleError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _skipped(name: str) -> bool:
    return bool(set(name.split("/")) & _SKIP_DIRS) or name.endswith(".pyc")


def is_bundled_test(name: str) -> bool:
    """True for the repo-level test files that ship in the zip."""
    if name in TEST_EXCLUDE:
        return False
    if name in TEST_EXTRA:
        return True
    d, _, f = name.rpartition("/")
    return d == "tests/kiko" and f.startswith("test_kiko_lite_") and f.endswith(".py")


def dest_name(name: str) -> str:
    """Path inside the zip for repo path ``name``."""
    return RENAMED.get(name, name)


def _sources() -> tuple[str, ...]:
    return (*SINGLE_FILES, *RENAMED)


def _is_bundled(name: str) -> bool:
    if name in _sources() or is_bundled_test(name):  # (the stdlib-only conftest is in RENAMED)
        return True
    return name.split("/")[0] in PACKAGES and not _skipped(name)


def file_list(root: Path = ROOT) -> list[str]:
    """Sorted POSIX-style repo paths of every file that goes into the zip (launchers, VERSION excluded), from HEAD.

    Renamed files (README, LICENSE) are listed under their repo path; ``dest_name`` gives the zip path."""
    out = _git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *PACKAGES, *_sources(), "tests")
    in_head = {n for n in out.decode("utf-8").split("\0") if n}
    names = {n for n in in_head if _is_bundled(n)}
    for pkg in PACKAGES:
        if not any(n.startswith(pkg + "/") for n in names):
            raise BundleError(f"package folder {pkg} is not in git at HEAD")
    for f in (*_sources(), *TEST_EXTRA):
        if f not in in_head:
            raise BundleError(f"missing file {f} at git HEAD (commit it first)")
    return sorted(names)


def check_clean(root: Path = ROOT) -> None:
    """Refuse when a bundled path differs from HEAD (the zip ships HEAD, so such edits would be lost silently)."""
    out = _git(root, "status", "--porcelain", "-z", "--untracked-files=all", "--", *PACKAGES, *_sources(), "tests")
    bad = [e for e in out.decode("utf-8", "replace").split("\0") if e and _is_bundled(e[3:])]
    if bad:
        raise BundleError("bundled files differ from git HEAD (commit or revert them first):\n  "
                          + "\n  ".join(bad))


def imports_litellm(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "litellm" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "litellm" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "litellm":
            return True
    return False


def version_tag(root: Path = ROOT) -> str:
    """Short git sha of HEAD (the zip is always exactly HEAD), else the date."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                             timeout=20, check=True).stdout.strip()
        if sha:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return datetime.date.today().strftime("%Y%m%d")


def commit_date(root: Path = ROOT) -> str:
    """Committer date (YYYY-MM-DD) of HEAD, so the same commit gives the same VERSION file; else today."""
    try:
        d = subprocess.run(["git", "show", "-s", "--format=%cs", "HEAD"], cwd=root, capture_output=True, text=True,
                           timeout=20, check=True).stdout.strip()
        if d:
            return d
    except (OSError, subprocess.SubprocessError):
        pass
    return datetime.date.today().isoformat()


def read_git(root: Path, name: str) -> bytes:
    """Raw bytes of ``name`` as stored in git at HEAD (no autocrlf, no working-tree state)."""
    try:
        r = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise BundleError(f"cannot run git for {name}: {e}") from e
    if r.returncode != 0:
        raise BundleError(f"{name} is not in git at HEAD (commit it first): {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def build(out_dir: Path, root: Path = ROOT, tag: str | None = None) -> Path:
    check_clean(root)
    names = file_list(root)
    data = {n: read_git(root, n) for n in names}
    for n in names:
        if n.endswith(".py") and imports_litellm(data[n].decode("utf-8")):
            raise BundleError(f"{n} imports litellm; kiko-lite must stay litellm-free")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = tag or version_tag(root)
    zpath = out_dir / f"kiko-lite-{tag}.zip"
    extras = ((WRAPPER_NAME, WRAPPER, 0o755), (WRAPPER_CMD_NAME, WRAPPER_CMD, 0o644),
              ("VERSION", f"kiko-lite {tag} {commit_date(root)}\n", 0o644))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for wname, wdata, mode in extras:
            info = zipfile.ZipInfo(wname, _FIXED_TIME)
            info.create_system = 3  # Unix: unzip keeps the permission bits
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, wdata)
        for n in sorted(names, key=dest_name):
            info = zipfile.ZipInfo(dest_name(n), _FIXED_TIME)
            info.create_system = 3
            exe = (n.endswith(".py") and dest_name(n).startswith("tools/")) or dest_name(n) == "install-service.sh"
            info.external_attr = (0o755 if exe else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data[n])
    return zpath


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="build the portable kiko-lite zip")
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder (default: <repo>/dist)")
    args = ap.parse_args(argv)
    try:
        z = build(args.out)
    except BundleError as e:
        print(f"kiko_lite_bundle: {e}", file=sys.stderr)
        return 2
    with zipfile.ZipFile(z) as zf:
        print(f"{z}  ({len(zf.namelist())} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
