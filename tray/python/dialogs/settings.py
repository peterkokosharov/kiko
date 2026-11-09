# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/settings.py — settings dialog data + actions."""
import json
import os
from pathlib import Path

from tray.config import Settings, load_settings

_TRAY_JSON = Path.home() / ".config" / "kiko" / "tray.json"


def get_settings(config_path: Path | None = None) -> Settings:
    """Load and return current Settings."""
    return load_settings(config_path)


def save_settings(s: Settings, tray_json: Path | None = None) -> None:
    """Persist tray-specific settings to tray.json (JSON format).

    Writes port, kiko_executable, and pystray_backend.
    The config_path itself is not persisted (it is an env/default value).
    """
    path = tray_json if tray_json is not None else _TRAY_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "port": s.port,
        "kiko_executable": s.kiko_executable,
        "pystray_backend": s.pystray_backend,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
