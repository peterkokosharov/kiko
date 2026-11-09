# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""
Unit tests for tray/backend.py.
pystray is NOT required — backend.py does not import it at module level.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tray.backend import SUPPORTED_BACKENDS, get_backend, set_backend


class TestGetBackend(unittest.TestCase):

    def _clean_env(self):
        """Return a copy of os.environ with PYSTRAY_BACKEND removed."""
        return {k: v for k, v in os.environ.items() if k != "PYSTRAY_BACKEND"}

    # B1 — Linux without env var → "xorg"
    def test_linux_default_is_xorg(self):
        with patch.dict(os.environ, self._clean_env(), clear=True), \
             patch.object(sys, "platform", "linux"):
            result = get_backend()
        self.assertEqual(result, "xorg")

    # B2 — env var set to "gtk" → returns "gtk" regardless of platform
    def test_env_var_overrides_platform(self):
        with patch.dict(os.environ, {"PYSTRAY_BACKEND": "gtk"}), \
             patch.object(sys, "platform", "linux"):
            result = get_backend()
        self.assertEqual(result, "gtk")

    # B3 — Windows without env var → None
    def test_windows_default_is_none(self):
        with patch.dict(os.environ, self._clean_env(), clear=True), \
             patch.object(sys, "platform", "win32"):
            result = get_backend()
        self.assertIsNone(result)

    # Extra: macOS without env var → None
    def test_macos_default_is_none(self):
        with patch.dict(os.environ, self._clean_env(), clear=True), \
             patch.object(sys, "platform", "darwin"):
            result = get_backend()
        self.assertIsNone(result)

    # Extra: env var set on Windows → returned as-is
    def test_env_var_on_windows_returned(self):
        with patch.dict(os.environ, {"PYSTRAY_BACKEND": "appindicator"}), \
             patch.object(sys, "platform", "win32"):
            result = get_backend()
        self.assertEqual(result, "appindicator")


class TestSetBackend(unittest.TestCase):

    # B4 — set_backend("xorg") → PYSTRAY_BACKEND=xorg in env
    def test_set_xorg_writes_env(self):
        env = {k: v for k, v in os.environ.items() if k != "PYSTRAY_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            set_backend("xorg")
            self.assertEqual(os.environ.get("PYSTRAY_BACKEND"), "xorg")

    # B5 — set_backend(None) → PYSTRAY_BACKEND NOT set
    def test_set_none_does_not_write_env(self):
        env = {k: v for k, v in os.environ.items() if k != "PYSTRAY_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            set_backend(None)
            self.assertNotIn("PYSTRAY_BACKEND", os.environ)

    def test_set_gtk_writes_env(self):
        env = {k: v for k, v in os.environ.items() if k != "PYSTRAY_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            set_backend("gtk")
            self.assertEqual(os.environ.get("PYSTRAY_BACKEND"), "gtk")


class TestSupportedBackends(unittest.TestCase):

    def test_supported_backends_contains_xorg(self):
        self.assertIn("xorg", SUPPORTED_BACKENDS)

    def test_supported_backends_contains_gtk(self):
        self.assertIn("gtk", SUPPORTED_BACKENDS)

    def test_supported_backends_contains_appindicator(self):
        self.assertIn("appindicator", SUPPORTED_BACKENDS)


if __name__ == "__main__":
    unittest.main()
