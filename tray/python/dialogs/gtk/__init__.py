# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/gtk/__init__.py — GTK import guard.

Import GTK3 only when a display is available.  Sets GTK_AVAILABLE to False
gracefully so that the logic layer can be imported in headless test environments.
"""
GTK_AVAILABLE = False

try:
    import gi  # type: ignore[import]
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa: F401  # type: ignore[import]
    GTK_AVAILABLE = True
except Exception:  # noqa: BLE001
    pass
