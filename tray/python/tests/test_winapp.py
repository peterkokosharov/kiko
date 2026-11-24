# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Logic of the python tray without pystray; the badge test needs Pillow and skips without it.

Run from the repo root: python -I -m unittest discover -s tray/python/tests -t tray/python
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

import kiko_tray_core as core
from tray import winapp


class Title(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(winapp.title_for("ok", 4000), "kiko ok (:4000)")
        self.assertIn("not reachable", winapp.title_for("down", 4000))
        self.assertTrue(winapp.title_for("ok", 4000, "x" * 300).__len__() <= 127)


class Tray(unittest.TestCase):
    def test_config_and_token_from_app_dir(self):
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "api-token"
            tok.write_text("secret\n", encoding="utf-8")
            (Path(td) / core.INI_NAME).write_text(core.tray_ini_text("py", "k.py", str(tok), 4007), encoding="utf-8")
            t = winapp.Tray(Path(td))
            self.assertEqual((t.cfg.port, t.token, t.state), (4007, "secret", "unknown"))

    def test_default_port_without_ini(self):
        self.assertEqual(winapp.Tray(None).cfg.port, 4000)


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow not installed")
class Badge(unittest.TestCase):
    def test_badge_colour_follows_state(self):
        img = winapp.make_icon("down", 32)
        self.assertEqual(img.size, (32, 32))
        r, g, b, a = img.getpixel((4, 16))
        self.assertEqual((r, g, b), (0xEF, 0x44, 0x44))


if __name__ == "__main__":
    unittest.main()
