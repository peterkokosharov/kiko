# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Unit tests of the ctest summary parser in tools/run_tests.py (real outputs from the CI runs).

Run from the repo root: python -I -m unittest discover -s tools/tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_tests  # noqa: E402

LINUX = """Test project /home/runner/work/kiko/kiko/build
      Start  1: config
 1/7 Test  #1: config ............................   Passed    0.00 sec

100% tests passed, 0 tests failed out of 7

Total Test time (real) =   0.05 sec
"""

WINDOWS = """Test project D:/a/kiko/kiko/build
    Start 7: socket
7/7 Test #7: socket ............................   Passed    0.02 sec

100% tests passed out of 7

Total Test time (real) =   0.30 sec
"""

FAILING = """86% tests passed, 1 tests failed out of 7

The following tests FAILED:
	  3 - json (Failed)
"""

FAILING_NEW = "86% tests passed out of 7\n"


class ParseCtest(unittest.TestCase):
    def test_linux_form(self):
        self.assertEqual(run_tests.parse_ctest(LINUX), (7, 0))

    def test_windows_form(self):
        self.assertEqual(run_tests.parse_ctest(WINDOWS), (7, 0))

    def test_failures(self):
        self.assertEqual(run_tests.parse_ctest(FAILING), (6, 1))
        self.assertEqual(run_tests.parse_ctest(FAILING_NEW), (6, 1))

    def test_no_summary(self):
        self.assertIsNone(run_tests.parse_ctest("nothing here"))


if __name__ == "__main__":
    unittest.main()
