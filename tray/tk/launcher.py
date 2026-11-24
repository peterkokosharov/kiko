# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Launcher of the tk tray: ``pythonw -I kiko_tray_tk.pyw`` (the installer stages this file under that name).

Staged layout: this file, kiko_tray_core.py and the kiko_tk/ package (tray/tk) side by side. Errors go to
logs/tray-error.log in the app folder (pythonw has no console).
"""
import sys
import traceback
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the recorded app folder
HERE = Path(__file__).resolve().parent


def main() -> int:
    sys.path.insert(0, str(HERE))
    try:
        from kiko_tk.__main__ import main as run
        return run(["--app-dir", str(HERE), "--hidden"])
    except SystemExit:
        raise
    except BaseException:
        try:
            (HERE / "logs").mkdir(exist_ok=True)
            (HERE / "logs" / "tray-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
