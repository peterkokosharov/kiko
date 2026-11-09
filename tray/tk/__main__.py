# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""``python -m kiko_tk [--url http://127.0.0.1:4001] [--key CLIENT_KEY]``"""
from __future__ import annotations

import argparse
import os
import tkinter as tk

from .app import App

DEFAULT_URL = "http://127.0.0.1:4001"  # owner home port; the sandbox uses 4100


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kiko_tk", description="tkinter tray + window for kiko-lite")
    p.add_argument("--url", default=os.environ.get("KIKO_URL", DEFAULT_URL), help=f"kiko-lite base URL (default {DEFAULT_URL})")
    p.add_argument("--key", default=os.environ.get("KIKO_TK_KEY", ""), help="client key, only if kiko-lite runs with auth; prefer env KIKO_TK_KEY "
                                                           "(a command-line key shows in the process list)")
    p.add_argument("--hidden", action="store_true", help="start in the tray only (needs Tk 9)")
    a = p.parse_args(argv)
    root = tk.Tk()
    app = App(root, a.url, a.key)
    if a.hidden and app.has_tray:
        root.withdraw()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
