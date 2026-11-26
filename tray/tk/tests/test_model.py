# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Model logic of the tk tray (no Tk needed).

Run from the repo root: python -I -m unittest discover -s tray/tk/tests -t tray
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tk import model  # noqa: E402  (the package is tray/tk; imported as "tk" from tray/)

OK = {"models": [{"alias": "a", "state": "ok", "enabled": True, "class": "c"}], "requests": [{"ok": True}]}


class State(unittest.TestCase):
    def test_none_states(self):
        self.assertEqual(model.overall_state(None, ""), "unknown")
        self.assertEqual(model.overall_state(None, "down"), "down")
        # /health answered but the panels could not be read: still green (the colour follows /health)
        self.assertEqual(model.overall_state(None, "auth"), "ok")
        self.assertEqual(model.overall_state(None, "nodata"), "ok")

    def test_ok_and_errors(self):
        self.assertEqual(model.overall_state(OK), "ok")
        bad_model = {"models": [{"state": "bad"}], "requests": []}
        self.assertEqual(model.overall_state(bad_model), "errors")
        bad_req = {"models": [], "requests": [{"ok": False}]}
        self.assertEqual(model.overall_state(bad_req), "errors")

    def test_only_recent_requests_count(self):
        reqs = [{"ok": True}] * model.RECENT_FOR_STATE + [{"ok": False}]
        self.assertEqual(model.overall_state({"models": [], "requests": reqs}), "ok")

    def test_junk_entries_ignored(self):
        self.assertEqual(model.overall_state({"models": ["x", 3, None], "requests": "no"}), "ok")


class Text(unittest.TestCase):
    def test_tooltip(self):
        self.assertIn("not reachable", model.tooltip("down", None, "http://h:4000", "down"))  # NOSONAR - test literal, no network
        self.assertIn("client key", model.tooltip("ok", None, "b", "auth"))
        self.assertIn("1 ok / 0 bad of 1", model.tooltip("ok", OK, "b"))

    def test_colors_cover_states(self):
        for s in ("ok", "errors", "down", "unknown"):
            self.assertIn(s, model.COLORS)

    def test_rows(self):
        self.assertEqual(model.server_rows({"server": {"uptime_seconds": 3700}})[3], ("uptime", "1 h 1 min"))
        self.assertEqual(model.server_rows({})[3], ("uptime", "0 h 0 min"))
        rows = model.request_rows({"requests": [{"ok": False, "model": "m", "upstream": "u", "latency_ms": 5}]})
        self.assertEqual(rows[0][0], "ERR")
        self.assertEqual(rows[0][2], "m > u")
        self.assertEqual(rows[0][4], "5 ms")
        cls = model.class_rows({"models": [{"alias": "a", "class": "x", "enabled": True},
                                           {"alias": "b", "class": "x"}, {"alias": "c"}]})
        self.assertEqual(cls[0], ("-", "1", "0", "c"))
        self.assertEqual(cls[1], ("x", "2", "1", "a, b"))


class Roles(unittest.TestCase):
    def test_model_rows_show_roles_primary_first_or_fall_back_to_class(self):
        rows = model.model_rows({"models": [
            {"alias": "opus", "class": "OPUS", "roles": ["OPUS", "EYES", "CODER"], "roles_text": "OPUS, EYES, CODER"},
            {"alias": "old", "class": "HAIKU"}]})
        self.assertEqual(rows[0][2], "OPUS, EYES, CODER")
        self.assertEqual(rows[1][2], "HAIKU")


class Tokens(unittest.TestCase):
    def test_fmt_tokens_same_as_cli(self):
        self.assertEqual([model.fmt_tokens(x) for x in (None, 0, True, "5", 1_000_000, 128_000, 131_072, 200_000)],
                         ["?", "?", "?", "?", "1M", "128k", "131072", "200k"])

    def test_model_rows_have_context_and_max_out(self):
        rows = model.model_rows({"models": [{"alias": "a", "enabled": True, "context_window": 1_000_000,
                                             "max_output": 64_000}, {"alias": "b"}]})
        self.assertEqual(rows[0][3:6], ("yes", "1M", "64k"))
        self.assertEqual(rows[1][3:6], ("no", "?", "?"))


if __name__ == "__main__":
    unittest.main()
