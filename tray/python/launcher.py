# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Launcher of the pystray tray: ``pythonw -I kiko_tray_py.pyw`` (the installer stages this file under that name).

Works staged (app folder with a ``tray/`` package and kiko_tray_core.py next to it) and from the repo
(tray/python/launcher.py: the folder is registered as the package ``tray``, tray/wincore holds the core).
Errors go to logs/tray-error.log in the app folder (pythonw has no console).
"""
import importlib.util
import sys
import traceback
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the recorded app folder
HERE = Path(__file__).resolve().parent


def prepare() -> Path | None:
    """Make ``tray.*`` and ``kiko_tray_core`` importable; returns the app folder (None in the repo)."""
    if (HERE / "tray" / "app.py").is_file():  # staged
        sys.path.insert(0, str(HERE))
        return HERE
    spec = importlib.util.spec_from_file_location("tray", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["tray"] = pkg
    spec.loader.exec_module(pkg)
    sys.path.insert(0, str(HERE.parent / "wincore"))
    return None


def main() -> int:
    try:
        app_dir = prepare()
        sys.path.insert(0, str(HERE))
        from tray.winapp import Tray
        Tray(app_dir).run()
        return 0
    except BaseException:
        if isinstance(sys.exc_info()[1], SystemExit):
            raise
        log = HERE / "logs"
        try:
            log.mkdir(exist_ok=True)
            (log / "tray-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
