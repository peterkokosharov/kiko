# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Every file with an Author header must carry an "AI models:" line."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import check_ai_header  # noqa: E402


class AiHeaderTest(unittest.TestCase):
    def test_all_author_headers_have_ai_models(self):
        self.assertEqual(check_ai_header.missing(ROOT), [])


if __name__ == "__main__":
    unittest.main()
