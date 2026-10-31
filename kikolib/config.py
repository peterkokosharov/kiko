# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""Configuration loader for kiko (TOML + env)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11


def find_config_dir() -> Path:
    """Find ~/.kiko directory."""
    # Check env var first
    if os.environ.get("KIKO_CONFIG_DIR"):
        return Path(os.environ["KIKO_CONFIG_DIR"]).expanduser().resolve()

    # Default: ~/.kiko
    return Path.home() / ".kiko"


def load_config() -> dict[str, Any]:
    """Load kiko.toml from config directory."""
    config_dir = find_config_dir()
    config_file = config_dir / "kiko.toml"

    if not config_file.exists():
        return {}

    with open(config_file, "rb") as f:
        return tomllib.load(f)


def get_config_path(key: str, default: Any = None) -> Any:
    """Get nested config value by dot notation (e.g., 'proxy.port')."""
    config = load_config()
    parts = key.split(".")
    value = config
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return default
        if value is None:
            return default
    return value


def get_database_path() -> Path:
    """Get database path from config or default."""
    config_dir = find_config_dir()
    db_path = get_config_path("database.path", "kiko.db")
    return (config_dir / db_path).expanduser().resolve()


def get_proxy_host() -> str:
    return get_config_path("proxy.host", "127.0.0.1")


def get_proxy_port() -> int:
    return get_config_path("proxy.port", 4002)


def get_webui_host() -> str:
    return get_config_path("webui.host", "127.0.0.1")


def get_webui_port() -> int:
    return get_config_path("webui.port", 4008)


def get_log_config() -> dict[str, Any]:
    return get_config_path("logging", {
        "level": "INFO",
        "file": "kiko.log",
        "max_size_mb": 10,
        "max_files": 5,
        "console": True,
    })


# Load once at module import
CONFIG_DIR = find_config_dir()
CONFIG = load_config()