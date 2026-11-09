# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
import os
import sys

SUPPORTED_BACKENDS = ["xorg", "gtk", "appindicator"]


def get_backend() -> str | None:
    """Return the effective pystray backend.

    Priority:
    1. PYSTRAY_BACKEND env var (user override)
    2. "xorg" on Linux (best for TDE/Trinity Desktop Environment)
    3. None on Windows/macOS (pystray picks automatically)
    """
    env = os.environ.get("PYSTRAY_BACKEND")
    if env:
        return env
    if sys.platform == "linux":
        return "xorg"
    return None


def set_backend(backend: str | None) -> None:
    """Set PYSTRAY_BACKEND env var so pystray picks it up on import.

    Must be called BEFORE pystray is imported (e.g. in app.py before
    ``import pystray``).  Passing None is a no-op.
    """
    if backend is not None:
        os.environ["PYSTRAY_BACKEND"] = backend
