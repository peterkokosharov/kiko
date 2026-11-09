# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.posix: signals, rss, sd_notify, watchdog, private files."""
import os
import signal
import socket
import sys
import threading

import pytest

from kikolib import posix

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


@pytest.fixture(autouse=True)
def _restore_signals():
    names = [n for n in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, n)]
    olds = {n: signal.getsignal(getattr(signal, n)) for n in names}
    yield
    for n, h in olds.items():
        signal.signal(getattr(signal, n), h)


def test_signal_handlers_main_thread_only():
    err = []

    def run():
        try:
            posix.install_signal_handlers(lambda: None)
        except RuntimeError as e:
            err.append(e)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert err


def test_signal_handler_sigint_calls_terminate():
    old = signal.getsignal(signal.SIGINT)
    hits = []
    try:
        posix.install_signal_handlers(lambda: hits.append(1))
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert hits == [1]
    finally:
        signal.signal(signal.SIGINT, old)


@posix_only
def test_sighup_reload():
    olds = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    hits = []
    try:
        posix.install_signal_handlers(lambda: hits.append("t"), lambda: hits.append("r"))
        signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert hits == ["r", "t"]
    finally:
        for s, h in olds.items():
            signal.signal(s, h)


def test_rss_bytes():
    v = posix.rss_bytes()
    if sys.platform == "win32" or sys.platform.startswith("linux"):
        assert isinstance(v, int) and v > 1_000_000
    else:
        assert v is None or v > 0


def test_sd_notify_unset(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert posix.sd_notify("READY=1") is False


def test_sd_notify_bad_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "nothing"))
    assert posix.sd_notify("READY=1") is False


@posix_only
def test_sd_notify_delivers(monkeypatch, tmp_path):
    path = str(tmp_path / "n.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        srv.bind(path)
        srv.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert posix.sd_notify("READY=1") is True
        assert srv.recv(100) == b"READY=1"
    finally:
        srv.close()


@posix_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="abstract sockets are Linux only")
def test_sd_notify_abstract(monkeypatch):
    name = f"kikolib-test-{os.getpid()}"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        srv.bind("\0" + name)
        srv.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        assert posix.sd_notify("WATCHDOG=1") is True
        assert srv.recv(100) == b"WATCHDOG=1"
    finally:
        srv.close()


def test_watchdog_interval(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert posix.watchdog_interval() is None
    monkeypatch.setenv("WATCHDOG_USEC", "10000000")
    assert posix.watchdog_interval() == 5.0
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert posix.watchdog_interval() == 5.0
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert posix.watchdog_interval() is None
    monkeypatch.setenv("WATCHDOG_PID", "x")
    assert posix.watchdog_interval() is None
    monkeypatch.delenv("WATCHDOG_PID")
    monkeypatch.setenv("WATCHDOG_USEC", "abc")
    assert posix.watchdog_interval() is None


def test_make_private(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    posix.make_private(p)  # must not raise anywhere
    if sys.platform != "win32":
        assert (p.stat().st_mode & 0o777) == 0o600
