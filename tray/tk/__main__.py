# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""``python -m kiko_tk [--url http://127.0.0.1:4001] [--key CLIENT_KEY]``"""
from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk
from pathlib import Path

from .app import App

DEFAULT_PORT = 4000  # the installed chain; env KIKO_PORT or kiko-tray.ini (--app-dir) override


def core_module():
    """kiko_tray_core (staged next to the kiko_tk folder, or tray/wincore in the repo); None if not found."""
    try:
        import kiko_tray_core as core
        return core
    except ImportError:
        pass
    for d in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parents[1] / "wincore"):
        if (d / "kiko_tray_core.py").is_file():
            sys.path.insert(0, str(d))
            import kiko_tray_core as core
            return core
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kiko_tk", description="tkinter tray + window for kiko-lite")
    p.add_argument("--url", default=os.environ.get("KIKO_URL", ""),
                   help="kiko-lite base URL (default http://127.0.0.1:PORT; PORT = 4000, env KIKO_PORT or the ini)")
    p.add_argument("--app-dir", default="", help="installed app folder with kiko-tray.ini (chain, port, token file)")
    p.add_argument("--key", default=os.environ.get("KIKO_TK_KEY", ""), help="client key, only if kiko-lite runs with auth; prefer env KIKO_TK_KEY "
                                                           "(a command-line key shows in the process list)")
    p.add_argument("--hidden", action="store_true", help="start in the tray only (needs Tk 9)")
    a = p.parse_args(argv)
    core = core_module()
    chain, key, url = None, a.key, a.url
    if core is not None:
        cfg = core.load_config(Path(a.app_dir) if a.app_dir else None)
        if not cfg.port:
            cfg.port = DEFAULT_PORT
        url = url or cfg.base
        key = key or core.read_token(cfg)
        if cfg.python and cfg.chain:
            chain = core.ChainOwner(cfg)
    url = url or f"http://127.0.0.1:{DEFAULT_PORT}"
    root = tk.Tk()
    app = App(root, url, key, chain)
    if a.hidden and app.has_tray:
        root.withdraw()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
