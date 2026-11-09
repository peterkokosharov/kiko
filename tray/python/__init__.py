# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray package — backwards-compat re-exports."""
from tray.app import KikoTray
from tray.config import Settings, default_config_path, load_settings
from tray.control import KikoControl, KikoProcess, is_port_listening

__all__ = [
    "KikoTray",
    "Settings",
    "default_config_path",
    "load_settings",
    "KikoControl",
    "KikoProcess",
    "is_port_listening",
]
