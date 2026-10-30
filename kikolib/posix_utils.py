# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""OS-process helpers: signals, memory, systemd notify/watchdog, private files.

POSIX first; on Windows every helper is a safe no-op or has a Windows equivalent.
Standard library only (ctypes for the Windows memory query).
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
from typing import Callable


def install_signal_handlers(
    on_terminate: Callable[[], None],
    on_reload: Callable[[], None] | None = None,
) -> None:
    """Route SIGTERM/SIGINT to ``on_terminate`` and SIGHUP to ``on_reload`` (POSIX).

    On Windows: SIGINT and SIGBREAK (if present). Must be called from the main thread.

    ``on_terminate`` runs inside the signal handler on the main thread, so it must not
    block: e.g. start a thread that calls HTTPServer.shutdown(), never call it directly.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("install_signal_handlers must be called from the main thread")

    def _term(signum: int, frame: object) -> None:
        on_terminate()

    def _reload(signum: int, frame: object) -> None:
        if on_reload is not None:
            on_reload()

    if sys.platform == "win32":
        names = ("SIGINT", "SIGBREAK")
    else:
        names = ("SIGTERM", "SIGINT")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _term)
    if sys.platform != "win32" and on_reload is not None and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _reload)


def rss_bytes() -> int | None:
    """Resident set size of this process in bytes, or None when unknown."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", "rb") as f:
                pages = int(f.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "win32":
            return _rss_windows()
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def _rss_windows() -> int | None:
    import ctypes
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return None
    return int(pmc.WorkingSetSize)


def sd_notify(state: str) -> bool:
    """Send ``state`` (e.g. "READY=1") to systemd via NOTIFY_SOCKET. Never raises."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(addr)
            s.sendall(state.encode("utf-8"))
        return True
    except (OSError, ValueError):
        return False


def watchdog_interval() -> float | None:
    """Seconds between watchdog pings (half of WATCHDOG_USEC), or None if not enabled."""
    try:
        usec = int(os.environ.get("WATCHDOG_USEC", ""))
    except ValueError:
        return None
    if usec <= 0:
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if pid:
        try:
            if int(pid) != os.getpid():
                return None
        except ValueError:
            return None
    return usec / 1_000_000 / 2


def make_private(path: str | os.PathLike[str]) -> None:
    """chmod 0o600 on POSIX; no-op on Windows."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def write_private(path: str | os.PathLike[str], data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with mode 0o600 (temp file + os.replace)."""
    path = os.fspath(path)
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    flags = (os.O_CREAT | os.O_EXCL | os.O_TRUNC | os.O_WRONLY
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        make_private(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
