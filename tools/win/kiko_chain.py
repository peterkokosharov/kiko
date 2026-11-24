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
import os
import secrets
import socket
import subprocess
import sys
import time
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


def stop() -> int:
    if not PIDFILE.is_file():
        print("no chain pid file; nothing to stop")
        return 0
    pid = int(PIDFILE.read_text().strip())
    kill_tree(pid)
    PIDFILE.unlink(missing_ok=True)
    print(f"stopped chain (pid {pid})")
    return 0


def run() -> int:
    if port_open(GW_PORT) or port_open(KIKO_PORT):
        print(f"port {GW_PORT} or {KIKO_PORT} is already in use; stop the other chain first", file=sys.stderr)
        return 1
    if not (DATA / "kiko.db").is_file():
        init_data()
    LOGS.mkdir(parents=True, exist_ok=True)
    env = kiko_env()
    gw_script = HERE / "gateway" / "scripts" / "claude_cli_gateway.py"
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    procs = []
    logs = []
    try:
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
    subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve())], creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    for _ in range(240):
        if port_open(KIKO_PORT):
            print(f"chain started in the background: kiko-lite {HOST}:{KIKO_PORT}")
            return 0
        time.sleep(0.5)
    print("chain did not come up in 120 s; see data/logs", file=sys.stderr)
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
