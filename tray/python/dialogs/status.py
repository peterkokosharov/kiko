# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/status.py — status dialog data logic."""
import subprocess
import time
from dataclasses import dataclass

from tray.config import Settings
from tray.control import KikoControl


@dataclass
class StatusData:
    running: bool
    port: int
    uptime_seconds: float | None  # None if not running
    version: str | None           # from kiko --version output or None


def _get_version(settings: Settings) -> str | None:
    """Run kiko --version and return the output string, or None on failure."""
    try:
        result = subprocess.run(
            [settings.kiko_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def get_status(settings: Settings, control: KikoControl) -> StatusData:
    """Return current status data for the status dialog."""
    running = control.is_running(settings)
    uptime: float | None = None
    if running and control.started_at is not None:
        uptime = time.monotonic() - control.started_at
    version = _get_version(settings) if running else None
    return StatusData(
        running=running,
        port=settings.port,
        uptime_seconds=uptime,
        version=version,
    )
