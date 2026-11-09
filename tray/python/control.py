# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/control.py — process/service control logic for kiko-tray."""
import shutil
import socket
import subprocess
import sys
import time

from tray.config import Settings

# Avoid AttributeError on Linux where CREATE_NO_WINDOW doesn't exist
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_port_listening(host: str, port: int) -> bool:
    """Return True if something is listening on host:port (TCP connect, 0.3 s timeout)."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


class KikoProcess:
    """Thin wrapper around subprocess.Popen for the kiko server process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self, settings: Settings) -> None:
        """Launch kiko with the given settings. Does NOT check if already running."""
        self._proc = subprocess.Popen(
            [
                settings.kiko_executable,
                "--config",
                str(settings.config_path),
                "--port",
                str(settings.port),
            ],
            creationflags=_CREATE_NO_WINDOW,
        )

    def stop(self) -> None:
        """Terminate the managed process, killing it if it does not exit within 5 s."""
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    def pid(self) -> int | None:
        """Return PID of the running process, or None."""
        if self._proc is not None:
            return self._proc.pid
        return None


class KikoControl:
    """High-level control: health-based status, no-double-start guard."""

    def __init__(self) -> None:
        self._process = KikoProcess()
        self.started_at: float | None = None  # monotonic timestamp when kiko was started

    def is_running(self, settings: Settings) -> bool:
        """Return True if kiko's port is listening (works for externally started kiko)."""
        return is_port_listening("127.0.0.1", settings.port)

    def start(self, settings: Settings) -> None:
        """Start kiko unless already running.

        - No-op if the port is already listening (no double start).
        - Raises FileNotFoundError if the executable is not found.
        """
        if self.is_running(settings):
            return
        if shutil.which(settings.kiko_executable) is None:
            raise FileNotFoundError(
                f"kiko executable not found on PATH: {settings.kiko_executable!r}"
            )
        self._process.start(settings)
        self.started_at = time.monotonic()

    def stop(self) -> None:
        """Stop the managed kiko process."""
        self._process.stop()
        self.started_at = None
