# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/logs.py — logs dialog data/logic layer."""
from pathlib import Path

from tray.logs import copy_last_n, get_log_folder, get_server_log, get_tray_log


def get_logs_data(n: int = 100) -> dict:
    """Return a dict with server lines, tray lines and log folder path."""
    return {
        'server_lines': get_server_log(n),
        'tray_lines': get_tray_log(n),
        'log_folder': str(get_log_folder()),
    }


def copy_logs(n: int = 100) -> str:
    """Return combined last *n* log lines (both logs) as a clipboard-ready string."""
    return copy_last_n(n)


def open_log_folder() -> None:
    """Open the log folder in the system file manager."""
    import subprocess
    import sys

    folder = get_log_folder()
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        import os
        os.startfile(str(folder))
    else:
        subprocess.run(['xdg-open', str(folder)], check=False)
