# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/app.py — thin GUI layer for kiko-tray (pystray/Pillow imports inside functions)."""
import os
import subprocess
import sys

from tray.config import Settings, load_settings
from tray.control import KikoControl


def _make_icon(size: int = 32):
    """Generate a dark square with a white 'K' using Pillow (imported inside function)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (size, size), (30, 30, 30, 255))
    draw = ImageDraw.Draw(img)
    font_size = max(int(size * 0.65), 8)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
    text = "K"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)
    return img


def open_config(settings: Settings) -> None:
    """Open the config file with the OS default editor."""
    if sys.platform == "win32":
        os.startfile(str(settings.config_path))
    else:
        subprocess.run(["xdg-open", str(settings.config_path)])


class KikoTray:
    """System-tray manager for kiko.  Same public interface as the original monolith."""

    def __init__(self) -> None:
        self._settings: Settings = load_settings()
        self._control: KikoControl = KikoControl()

    # ── status ────────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._control.is_running(self._settings)

    def _status_text(self) -> str:
        return "● Running" if self.is_running() else "○ Stopped"

    # ── actions ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        try:
            self._control.start(self._settings)
        except FileNotFoundError:
            pass  # GUI layer would display an error; silently ignore here

    def stop(self) -> None:
        self._control.stop()

    def open_config(self) -> None:
        open_config(self._settings)

    # ── menu callbacks ────────────────────────────────────────────────────────

    def _on_start(self, icon) -> None:
        try:
            self._control.start(self._settings)
        except FileNotFoundError:
            pass

    def _on_stop(self, icon) -> None:
        self.stop()

    def _on_exit(self, icon) -> None:
        self.stop()
        icon.stop()

    # ── tray entry point ──────────────────────────────────────────────────────

    def run(self) -> None:
        from tray.backend import get_backend, set_backend
        set_backend(self._settings.pystray_backend or get_backend())

        import pystray as pt

        def _menu():
            cfg_missing = not self._settings.config_path.exists()
            running = self.is_running()
            return (
                pt.MenuItem(self._status_text(), None, enabled=False),
                pt.Menu.SEPARATOR,
                pt.MenuItem(
                    "Start KIKO",
                    lambda icon, item: self._on_start(icon),
                    enabled=not running and not cfg_missing,
                ),
                pt.MenuItem(
                    "Stop KIKO",
                    lambda icon, item: self._on_stop(icon),
                    enabled=running,
                ),
                pt.Menu.SEPARATOR,
                pt.MenuItem(
                    "Open config" + (" (missing)" if cfg_missing else ""),
                    lambda icon, item: self.open_config(),
                    enabled=not cfg_missing,
                ),
                pt.Menu.SEPARATOR,
                pt.MenuItem("Exit", lambda icon, item: self._on_exit(icon)),
            )

        icon = pt.Icon("kiko", _make_icon(32), "KIKO", pt.Menu(_menu))
        icon.run()
