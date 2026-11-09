# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""``python -m kiko_lite <command>``."""
import sys

from .cli import main

sys.exit(main())
