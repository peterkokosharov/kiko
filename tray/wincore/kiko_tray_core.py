# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Shared behaviour contract of the non-native Windows trays (tk and python); standard library only.

- state: "ok" (GET /health on the port answers status ok), "down", "unknown" (nothing polled yet);
- Start/Stop of the installed chain through kiko_chain.py (--detach / --stop) of apps/kiko;
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
    """['python', '-I', 'kiko_chain.py', '--detach'|'--stop']; ValueError when the chain is not configured."""
    if action not in ("start", "stop"):
        raise ValueError(action)
    if not cfg.python or not cfg.chain:
        raise ValueError("no chain configured (kiko-tray.ini has no python/chain)")
    return [cfg.python, "-I", cfg.chain, "--detach" if action == "start" else "--stop"]


def run_chain(cfg: Config, action: str, timeout: float = 90.0) -> tuple[bool, str]:
    """Run kiko_chain.py --detach / --stop; (ok, last line of its output). Blocking: call from a worker thread."""
    try:
        cmd = chain_command(cfg, action)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    text = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, text[-1] if text else ""


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


