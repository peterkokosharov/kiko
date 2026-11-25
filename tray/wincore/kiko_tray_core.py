# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5, claude-opus-4-8
"""Shared behaviour contract of the non-native Windows trays (tk and python); standard library only.

- state: "ok" (GET /health on the port answers status ok), "down", "unknown" (nothing polled yet);
- Start runs apps/kiko/kiko_chain.py (run mode) as a child; Stop and quit end only that child (ChainOwner),
  through kiko_chain.py --stop (pid + start-time check); a chain started outside is only watched;
- the API token is read from apps/kiko/data/api-token and is never shown or logged.
The installer writes <app>/kiko-tray.ini; env KIKO_PORT / KIKO_API_TOKEN override it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 4000
INI_NAME = "kiko-tray.ini"
COLORS = {"ok": "#22c55e", "down": "#ef4444", "unknown": "#9ca3af"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Config:
    python: str = ""          # interpreter that runs kiko_chain.py (the one the chain was installed with)
    chain: str = ""           # path of kiko_chain.py
    port: int = DEFAULT_PORT
    token_file: str = ""

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def parse_ini(text: str) -> dict:
    """key = value lines; '#' and ';' start a comment line; values keep their text (surrounding quotes removed)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        out[k.strip().lower()] = v
    return out


def _port(v, default: int) -> int:
    try:
        p = int(str(v).strip())
        return p if 0 < p < 65536 else default
    except ValueError:
        return default


def load_config(app_dir: Path | None, env=None) -> Config:
    env = os.environ if env is None else env
    data = {}
    if app_dir is not None and (Path(app_dir) / INI_NAME).is_file():
        data = parse_ini((Path(app_dir) / INI_NAME).read_text(encoding="utf-8", errors="replace"))
    cfg = Config(python=data.get("python", ""), chain=data.get("chain", ""),
                 port=_port(data.get("port", DEFAULT_PORT), DEFAULT_PORT),
                 token_file=os.path.expandvars(data.get("token_file", "")))
    if env.get("KIKO_PORT"):
        cfg.port = _port(env["KIKO_PORT"], cfg.port)
    return cfg


def read_token(cfg: Config, env=None) -> str:
    """env KIKO_API_TOKEN, else the first line of the token file; "" when there is none."""
    env = os.environ if env is None else env
    if env.get("KIKO_API_TOKEN"):
        return env["KIKO_API_TOKEN"].strip()
    if cfg.token_file:
        try:
            lines = Path(cfg.token_file).read_text(encoding="utf-8").strip().splitlines()
            return lines[0].strip() if lines else ""
        except OSError:
            return ""
    return ""


def health(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("status") == "ok"
    except (OSError, ValueError, AttributeError):
        return False


def state(port: int, timeout: float = 2.0) -> str:
    return "ok" if health(port, timeout) else "down"


def chain_command(cfg: Config, action: str) -> list[str]:
    """['python', '-I', 'kiko_chain.py'] (run mode, start) or [..., '--stop']; ValueError when not configured."""
    if action not in ("start", "stop"):
        raise ValueError(action)
    if not cfg.python or not cfg.chain:
        raise ValueError("no chain configured (kiko-tray.ini has no python/chain)")
    cmd = [cfg.python, "-I", cfg.chain]
    return cmd + ["--stop"] if action == "stop" else cmd


def pidfile_pid(cfg: Config) -> int | None:
    """The pid in <kiko>/data/chain.pid (first line), or None."""
    try:
        return int((Path(cfg.chain).parent / "data" / "chain.pid").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


class ChainOwner:
    """Starts the chain in run mode as a child and stops ONLY that child (like the native tray's job).

    Stop goes through ``kiko_chain.py --stop``, which verifies pid and process start time first; it is only
    run when the pid file names the child we launched. A chain started elsewhere is never touched."""

    def __init__(self, cfg: Config) -> None:
        self.config = cfg
        self.proc: subprocess.Popen | None = None

    def owns(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def run(self, action: str, timeout: float = 30.0) -> tuple[bool, str]:
        """Blocking (use a worker thread). (ok, one-line message)."""
        try:
            cmd = chain_command(self.config, action)
        except ValueError as e:
            return False, str(e)
        if action == "start":
            if health(self.config.port):
                return False, "kiko is already running"
            try:
                self.proc = subprocess.Popen(cmd, creationflags=NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                return False, str(e)
            return True, "starting"
        if not self.owns():
            return False, ("kiko was started outside the tray" if health(self.config.port) else "kiko is not running")
        if pidfile_pid(self.config) != self.proc.pid:
            return False, "the chain pid file does not name our child; nothing killed"
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        lines = (r.stdout + r.stderr).strip().splitlines()
        return r.returncode == 0, lines[-1] if lines else "stopped"

    def quit(self) -> None:
        """Tray exit: stop our own child (and its tree), never a foreign chain."""
        if not self.owns():
            return
        self.run("stop")
        if self.owns():  # pid file not written yet or refused: our own child only, tree included
            kill_child_tree(self.proc)


def kill_child_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()
    # Reap it so owns()/poll() reflect the kill: POSIX kill() only sends SIGKILL and returns at once, so
    # without this wait the just-killed child can still look alive (taskkill already blocks on Windows).
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def copy_to_clipboard(text: str) -> bool:
    """Windows clip.exe (the text goes through stdin, never the command line)."""
    if sys.platform != "win32":
        return False
    try:
        subprocess.run(["clip"], input=text.encode("utf-8"), creationflags=NO_WINDOW, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def tray_ini_text(python: str, chain: str, token_file: str, port: int = DEFAULT_PORT) -> str:
    return f'python = "{python}"\r\nchain = "{chain}"\r\nport = {port}\r\ntoken_file = {token_file}\r\n'


