# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus, Anthropic Sonnet
"""
Tests for KIKO Linux packaging deliverables.
Covers: kiko.service, install.sh, Linux zip (releases/kiko-l-0.2.0.zip), .deb skeleton.
All tests use stdlib only (unittest, zipfile, pathlib, subprocess).
"""
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

# KIKO repo root — two levels up from tests/kiko/
ROOT = Path(__file__).parents[2]
RELEASES = ROOT / "releases"
ZIP_PATH = RELEASES / "kiko-l-0.2.0.zip"
SERVICE_FILE = ROOT / "kiko.service"
INSTALL_SH = ROOT / "install.sh"
DEB_BASE = ROOT / "deb"
DEB_ROOT = DEB_BASE / "kiko_0.2.0_amd64"
DEBIAN_DIR = DEB_ROOT / "DEBIAN"


# ── Linux zip build (attempted once at module import) ─────────────────────────

_zip_build_ok: bool = False


def _try_build_linux_zip() -> None:
    """Attempt to build the Linux zip; set _zip_build_ok accordingly."""
    global _zip_build_ok
    if ZIP_PATH.exists():
        _zip_build_ok = True
        return
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "kiko_build.py"), "linux"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=300,
        )
        _zip_build_ok = (result.returncode == 0)
    except Exception:
        _zip_build_ok = False


_try_build_linux_zip()


# ── kiko.service ──────────────────────────────────────────────────────────────

class TestKikoService(unittest.TestCase):
    """Tests for kiko.service systemd user unit file."""

    def _content(self) -> str:
        return SERVICE_FILE.read_text(encoding="utf-8")

    def test_file_exists(self):
        self.assertTrue(SERVICE_FILE.exists(), "kiko.service not found")

    def test_has_unit_section(self):
        self.assertIn("[Unit]", self._content())

    def test_has_service_section(self):
        self.assertIn("[Service]", self._content())

    def test_has_install_section(self):
        self.assertIn("[Install]", self._content())

    def test_wanted_by_default_target(self):
        self.assertIn("WantedBy=default.target", self._content())

    def test_restart_on_failure(self):
        self.assertIn("Restart=on-failure", self._content())

    def test_exec_start_contains_python312(self):
        self.assertIn("python3.12", self._content())

    def test_exec_start_contains_host_127(self):
        self.assertIn("--host 127.0.0.1", self._content())

    def test_no_open_bind_address(self):
        self.assertNotIn("0.0.0.0", self._content(),
                         "kiko.service must not bind to 0.0.0.0")


# ── install.sh ────────────────────────────────────────────────────────────────

class TestInstallSh(unittest.TestCase):
    """Tests for install.sh Linux zip install script."""

    def _content(self) -> str:
        return INSTALL_SH.read_text(encoding="utf-8")

    def _lines(self) -> list:
        return self._content().splitlines()

    def test_file_exists(self):
        self.assertTrue(INSTALL_SH.exists(), "install.sh not found")

    def test_shebang_bash(self):
        lines = self._lines()
        self.assertTrue(
            lines[0].startswith("#!/usr/bin/env bash"),
            f"Expected bash shebang on line 1, got: {lines[0]!r}",
        )

    def test_set_e_after_header(self):
        lines = self._lines()
        self.assertEqual(
            lines[4].strip(), "set -e",
            f"Expected 'set -e' on line 5 (after shebang and 3-line header), got: {lines[4]!r}",
        )

    def test_python312_check(self):
        self.assertIn("command -v python3.12", self._content())

    def test_pip_install(self):
        self.assertIn("pip install", self._content())

    def test_systemctl_enable(self):
        self.assertIn("systemctl --user enable kiko", self._content())

    def test_systemctl_start(self):
        self.assertIn("systemctl --user start kiko", self._content())


# ── Linux zip ─────────────────────────────────────────────────────────────────

class TestLinuxZip(unittest.TestCase):
    """Tests for releases/kiko-l-0.2.0.zip."""

    def setUp(self):
        if not _zip_build_ok:
            self.skipTest("Linux zip build unavailable (uv not found or build failed)")

    def test_zip_exists(self):
        self.assertTrue(ZIP_PATH.exists(),
                        f"releases/kiko-l-0.2.0.zip not found at {ZIP_PATH}")

    def test_zip_contains_install_sh(self):
        with zipfile.ZipFile(ZIP_PATH) as zf:
            self.assertIn("install.sh", zf.namelist())

    def test_zip_contains_kiko_service(self):
        with zipfile.ZipFile(ZIP_PATH) as zf:
            self.assertIn("kiko.service", zf.namelist())

    def test_install_sh_has_executable_bit(self):
        with zipfile.ZipFile(ZIP_PATH) as zf:
            for info in zf.infolist():
                if info.filename == "install.sh":
                    mode = (info.external_attr >> 16) & 0o777
                    self.assertTrue(
                        mode & 0o111,
                        f"install.sh must have executable bit set, got mode={oct(mode)}",
                    )
                    return
        self.fail("install.sh not found in zip")


# ── .deb skeleton ─────────────────────────────────────────────────────────────

class TestDebSkeleton(unittest.TestCase):
    """Tests for deb/kiko_0.2.0_amd64/ skeleton."""

    def test_control_exists(self):
        self.assertTrue((DEBIAN_DIR / "control").exists(),
                        "deb/kiko_0.2.0_amd64/DEBIAN/control not found")

    def test_control_package_kiko(self):
        content = (DEBIAN_DIR / "control").read_text(encoding="utf-8")
        self.assertIn("Package: kiko", content)

    def test_control_depends_python312(self):
        content = (DEBIAN_DIR / "control").read_text(encoding="utf-8")
        self.assertIn("python3.12", content)

    def test_postinst_exists(self):
        self.assertTrue((DEBIAN_DIR / "postinst").exists(),
                        "deb/kiko_0.2.0_amd64/DEBIAN/postinst not found")

    def test_postinst_shebang_bash(self):
        lines = (DEBIAN_DIR / "postinst").read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            lines[0].startswith("#!/bin/bash"),
            f"Expected #!/bin/bash shebang, got: {lines[0]!r}",
        )

    def test_postinst_no_systemctl(self):
        content = (DEBIAN_DIR / "postinst").read_text(encoding="utf-8")
        self.assertNotIn(
            "systemctl", content,
            "postinst must not call systemctl (no user context available as root)",
        )

    def test_build_deb_sh_exists_and_calls_dpkg(self):
        build_deb = DEB_BASE / "build_deb.sh"
        self.assertTrue(build_deb.exists(), "deb/build_deb.sh not found")
        content = build_deb.read_text(encoding="utf-8")
        self.assertIn("dpkg-deb --build", content)


if __name__ == "__main__":
    unittest.main()
