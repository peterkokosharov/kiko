# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/control.py — start/stop/restart action logic."""
from dataclasses import dataclass

from tray.config import Settings
from tray.control import KikoControl


@dataclass
class ControlResult:
    success: bool
    message: str


def start_kiko(settings: Settings, control: KikoControl) -> ControlResult:
    """Start kiko; return ControlResult with outcome."""
    try:
        control.start(settings)
        return ControlResult(success=True, message="kiko started")
    except FileNotFoundError:
        return ControlResult(success=False, message="kiko executable not found")
    except Exception as exc:  # noqa: BLE001
        return ControlResult(success=False, message=str(exc))


def stop_kiko(settings: Settings, control: KikoControl) -> ControlResult:
    """Stop kiko; return ControlResult with outcome."""
    try:
        control.stop()
        return ControlResult(success=True, message="kiko stopped")
    except Exception as exc:  # noqa: BLE001
        return ControlResult(success=False, message=str(exc))


def restart_kiko(settings: Settings, control: KikoControl) -> ControlResult:
    """Restart kiko: stop then start."""
    stop_result = stop_kiko(settings, control)
    if not stop_result.success:
        return stop_result
    return start_kiko(settings, control)
