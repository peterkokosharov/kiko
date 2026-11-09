# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/config.py — paths and settings for kiko-tray."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def default_config_path() -> Path:
    """Return config path: $KIKO_CONFIG env var or ~/.kircho/kiko-config.yaml."""
    env = os.environ.get("KIKO_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".kircho" / "kiko-config.yaml"


@dataclass
class Settings:
    config_path: Path
    port: int = 4000
    kiko_executable: str = "kiko"
    pystray_backend: str | None = None


def _parse_kiko_proxy_section(text: str) -> dict:
    """Minimal line-by-line parser for the kiko_proxy: section of a YAML file.

    Handles only simple key: value pairs nested one level under kiko_proxy:.
    No PyYAML dependency.
    """
    result: dict = {}
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("kiko_proxy:"):
            in_section = True
            continue
        if in_section:
            # Check if this is still indented (part of kiko_proxy section)
            if line and not line[0].isspace():
                # New top-level key — we're done
                break
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                result[key.strip()] = val.strip()
    return result


def load_settings(config_path: Path | None = None) -> "Settings":
    """Load settings from YAML config file.

    Falls back to defaults if file is missing or key is absent.
    Only reads kiko_proxy.port and kiko_proxy.executable.
    """
    path = config_path if config_path is not None else default_config_path()
    settings = Settings(config_path=path)
    if not path.exists():
        return settings
    try:
        text = path.read_text(encoding="utf-8")
        data = _parse_kiko_proxy_section(text)
        if "port" in data:
            try:
                settings.port = int(data["port"])
            except ValueError:
                pass
        if "executable" in data:
            val = data["executable"]
            if val:
                settings.kiko_executable = val
        if "pystray_backend" in data:
            val = data["pystray_backend"]
            if val:
                settings.pystray_backend = val
    except OSError:
        pass
    return settings
