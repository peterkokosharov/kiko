"""Tests for the portable build artefacts."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
import zipfile
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
DIST = REPO_ROOT / "dist"


def test_start_bat_no_pip():
    """start-portable.bat must not call pip (no pip on embedded Python)."""
    bat = DIST / "start-portable.bat"
    assert bat.exists(), "dist/start-portable.bat not found"
    text = bat.read_text(encoding="utf-8", errors="replace")
    assert "pip" not in text.lower(), "start-portable.bat must not use pip"


def test_start_bat_no_userprofile():
    """start-portable.bat must keep config/DB inside the portable folder."""
    bat = DIST / "start-portable.bat"
    text = bat.read_text(encoding="utf-8", errors="replace")
    assert "USERPROFILE" not in text
    assert "KIKO_DIR" in text


def test_start_bat_checks_python_exe():
    """start-portable.bat must verify python\\python.exe exists before use."""
    bat = DIST / "start-portable.bat"
    text = bat.read_text(encoding="utf-8", errors="replace")
    assert "python.exe" in text
    # Should have an existence check
    assert "not exist" in text.lower() or "if exist" in text.lower()


def test_kiko_start_py_inserts_lib():
    """kiko_start.py must add lib/ to sys.path before run_server."""
    src = DIST / "kiko_start.py"
    assert src.exists(), "dist/kiko_start.py not found"
    text = src.read_text(encoding="utf-8")
    assert "lib" in text
    assert "sys.path.insert" in text


def test_portable_config_no_absolute_paths():
    """kiko-portable-config.yaml must have no Windows absolute paths."""
    cfg = DIST / "kiko-portable-config.yaml"
    assert cfg.exists(), "dist/kiko-portable-config.yaml not found"
    text = cfg.read_text(encoding="utf-8")
    assert "USERPROFILE" not in text
    assert "C:\\" not in text


def test_kiko_build_has_portable_mode():
    """kiko_build.py must accept 'portable' as a mode."""
    src = REPO_ROOT / "kiko_build.py"
    assert src.exists(), "kiko_build.py not found"
    text = src.read_text(encoding="utf-8")
    assert "portable" in text


def test_kiko_build_downloads_embedded_python():
    """kiko_build.py portable mode must reference python.org embeddable URL."""
    src = REPO_ROOT / "kiko_build.py"
    text = src.read_text(encoding="utf-8")
    assert "python.org" in text or "embed-amd64.zip" in text
    assert "urllib" in text
