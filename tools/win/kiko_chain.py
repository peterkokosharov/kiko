# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Start the Claude chain of the portable kiko install (standard library only).

    python -I kiko_chain.py [--detach]   gateway 127.0.0.1:4100, then kiko-lite 127.0.0.1:4000
    python -I kiko_chain.py --init       create data/ (tokens, database); safe to repeat
    python -I kiko_chain.py --stop       stop the running chain

Lives in the app folder next to kiko-lite/ and gateway/; kikolib is found in ../kikolib (or the folder named by
env KIKO_KIKOLIB_DIR). Runtime data (tokens, database, logs) is in data/. Auth stays on: the gateway needs its
token, kiko-lite needs the master key. Both servers are children; when this process ends both are stopped.
See docs/WINDOWS-PORTABLE-INSTALL.md.
"""
import argparse
import csv
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = DATA / "logs"
HOST = "127.0.0.1"
GW_PORT, KIKO_PORT = 4100, 4000
GW_BASE = f"http://{HOST}:{GW_PORT}/v1"
MODELS = (("haiku", "HAIKU"), ("sonnet", "SONNET"), ("opus", "OPUS"))  # Anthropic only
PIDFILE = DATA / "chain.pid"


def kikolib_dir() -> Path:
    return Path(os.environ.get("KIKO_KIKOLIB_DIR") or HERE.parent / "kikolib")


def secret(name: str) -> str:
    """Read data/<name>, creating it with a new random value when missing."""
    p = DATA / name
    if not p.is_file() or not p.read_text(encoding="utf-8").strip():
        DATA.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def kiko_env() -> dict:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([str(HERE / "kiko-lite"), str(kikolib_dir())]),
        KIKO_CONFIG_DIR=str(DATA), KIKO_DB_PATH=str(DATA / "kiko.db"),
        PYTHONDONTWRITEBYTECODE="1",  # no __pycache__ inside the recorded app folders
        PYTHONDONTWRITEBYTECODE="1",  # no __pycache__ inside the recorded app folders
        CGW_TOKEN=secret("gwtoken"), MASTER_KEY=secret("masterkey"),
    )
    return env


def kiko_cmd(*args: str) -> list:
    # -P: do not put the current folder on sys.path (-I would also drop PYTHONPATH, which carries kikolib)
    return [sys.executable, "-P", "-m", "kiko_lite", *args]


def init_data() -> None:
    """Create tokens and the database with the Anthropic models; add-provider/add-model are idempotent."""
    LOGS.mkdir(parents=True, exist_ok=True)
    env = kiko_env()
    steps = [["add-provider", "cgw", "--api-base", GW_BASE, "--api-key-env", "CGW_TOKEN"]]
    for alias, cls in MODELS:
        steps.append(["add-model", alias, "--provider", f"cgw/{alias}", "--api-base", GW_BASE,
                      "--api-key-env", "CGW_TOKEN", "--class", cls,
                      "--price-in", "0", "--price-out", "0", "--price-cache", "0"])
    for s in steps:
        r = subprocess.run(kiko_cmd(*s), env=env, cwd=str(DATA), capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"kiko_lite {s[0]} {s[1]} failed: {(r.stderr or r.stdout).strip()}")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _run_text(cmd: list) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def proc_cmdline(pid: int):
    """(image name, command line) of a running process, or None when it cannot be found or read."""
    if sys.platform == "win32":
        out = _run_text(["wmic", "process", "where", f"processid={pid}", "get", "commandline,name", "/format:list"])
        info = {}
        for line in out.splitlines():
            k, _, v = line.strip().partition("=")
            if k in ("CommandLine", "Name"):
                info[k] = v
        if info.get("Name"):
            return info["Name"], info.get("CommandLine", "")
        return None
    out = _run_text(["ps", "-p", str(pid), "-o", "comm=,args="])
    if not out.strip():
        return None
    parts = out.strip().split(None, 1)
    return os.path.basename(parts[0]), (parts[1] if len(parts) > 1 else "")


def proc_image(pid: int):
    """Image name of a running process (Windows: tasklist), or None."""
    if sys.platform != "win32":
        info = proc_cmdline(pid)
        return info[0] if info else None
    rows = list(csv.reader(io.StringIO(_run_text(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]))))
    for row in rows:
        if len(row) > 1 and row[1].strip() == str(pid):
            return row[0]
    return None


def proc_start(pid: int) -> str:
    """Creation time of a process as text (Windows, ctypes), '' when unknown. Guards against pid reuse."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return ""
        return str((t[0].dwHighDateTime << 32) | t[0].dwLowDateTime)
    finally:
        k32.CloseHandle(h)


def is_our_chain(pid: int, started: str = "") -> bool:
    """True only when pid is a python process whose command line names kiko_chain.py. Without a readable command
    line (wmic is gone on new Windows) it needs the recorded creation time to match as well; else: no."""
    if started and proc_start(pid) != started:
        return False
    info = proc_cmdline(pid)
    if info:
        name, cmd = info
        return name.lower().startswith("python") and str(HERE / "kiko_chain.py").lower() in cmd.lower()
    name = proc_image(pid)
    return bool(name and name.lower().startswith("python") and started)


def stop() -> int:
    if not PIDFILE.is_file():
        print("no chain pid file; nothing to stop")
        return 0
    lines = PIDFILE.read_text(encoding="utf-8").split()
    try:
        pid = int(lines[0])
    except (IndexError, ValueError):
        print(f"{PIDFILE} is not a valid pid file; not stopping anything (remove it by hand)", file=sys.stderr)
        return 1
    if proc_cmdline(pid) is None and proc_image(pid) is None:
        PIDFILE.unlink(missing_ok=True)
        print(f"chain pid {pid} is not running; removed the stale pid file")
        return 0
    if not is_our_chain(pid, lines[1] if len(lines) > 1 else ""):
        print(f"pid {pid} in {PIDFILE} is not our chain (not a python kiko_chain.py process, or it cannot be "
              f"verified); NOT stopping it. If the chain is really gone, delete the pid file.", file=sys.stderr)
        return 1
    kill_tree(pid)
    PIDFILE.unlink(missing_ok=True)
    print(f"stopped chain (pid {pid})")
    return 0


def chain_ready() -> bool:
    """kiko-lite on :4000 answers /health AND accepts our master key (a foreign server fails the second)."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{KIKO_PORT}/health", timeout=3) as r:
            if r.status != 200 or json.loads(r.read().decode("utf-8")).get("status") != "ok":
                return False
        req = urllib.request.Request(f"http://{HOST}:{KIKO_PORT}/v1/models",
                                     headers={"x-api-key": secret("masterkey")})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def run() -> int:
    if port_open(GW_PORT) or port_open(KIKO_PORT):
        print(f"port {GW_PORT} or {KIKO_PORT} is already in use; stop the other chain first", file=sys.stderr)
        return 1
    if not (DATA / "kiko.db").is_file():
        init_data()
    LOGS.mkdir(parents=True, exist_ok=True)
    env = kiko_env()
    gw_script = HERE / "gateway" / "scripts" / "claude_cli_gateway.py"
    PIDFILE.write_text(f"{os.getpid()}\n{proc_start(os.getpid())}\n", encoding="utf-8")
    procs = []
    logs = []
    try:
        import signal
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # the finally block then stops the children
        for name, cmd, e in (
            ("gateway", [sys.executable, "-I", str(gw_script), "--port", str(GW_PORT),
                         "--token-file", str(DATA / "gwtoken")], None),
            ("kiko-lite", kiko_cmd("serve", "--host", HOST, "--port", str(KIKO_PORT)), env),
        ):
            if name == "kiko-lite":  # it probes the models through the gateway at start
                for _ in range(120):
                    if port_open(GW_PORT) or procs[0].poll() is not None:
                        break
                    time.sleep(0.5)
                if procs[0].poll() is not None:
                    print("gateway exited early; see data/logs/gateway.log", file=sys.stderr)
                    return 1
            log = open(LOGS / f"{name}.log", "ab")
            logs.append(log)
            procs.append(subprocess.Popen(cmd, env=e, cwd=str(DATA), stdout=log, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL))
        print(f"chain running: gateway {HOST}:{GW_PORT}, kiko-lite {HOST}:{KIKO_PORT} (Ctrl-C or --stop to end)")
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("a chain process exited; stopping the other", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                kill_tree(p.pid)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for log in logs:
            log.close()
        PIDFILE.unlink(missing_ok=True)


def detach() -> int:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    child = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve())], creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
    for _ in range(240):
        if port_open(KIKO_PORT) and chain_ready():
            print(f"chain started in the background: kiko-lite {HOST}:{KIKO_PORT}")
            return 0
        if child.poll() is not None:
            print(f"the chain process exited early (code {child.returncode}); see data/logs, or run "
                  f"kiko_chain.py in a console", file=sys.stderr)
            return 1
        time.sleep(0.5)
    print("chain did not come up (kiko-lite /health with our master key) in 120 s; see data/logs", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kiko_chain.py")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--init", action="store_true", help="create data/ and the database, then exit")
    g.add_argument("--stop", action="store_true", help="stop the running chain")
    g.add_argument("--detach", action="store_true", help="start in the background")
    a = ap.parse_args(argv)
    if a.init:
        init_data()
        print(f"data ready in {DATA}")
        return 0
    if a.stop:
        return stop()
    if a.detach:
        return detach()
    return run()


if __name__ == "__main__":
    sys.exit(main())
