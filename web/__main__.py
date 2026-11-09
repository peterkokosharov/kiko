# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Start the web UI: ``python -m web [--host HOST] [--port PORT] [--db PATH]`` (defaults: kiko.toml via kikolib.config)."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from kiko_lite.db import Database, default_path
from kikolib import config as kconfig
from kikolib import logs as klogs
from kikolib import server as kserver

from .security import is_loopback_host
from .ui import WebUI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m web", description="kiko web admin UI (loopback by default)")
    ap.add_argument("--host", default=None, help=f"bind address (default {kconfig.web_host()}: [web] host of "
                    f"{kconfig.TOML_NAME})")
    ap.add_argument("--port", type=int, default=None, help=f"port (default {kconfig.web_port()}: [web] port or "
                    f"base_port + 1 of {kconfig.TOML_NAME})")
    ap.add_argument("--db", default=None, help=f"database file (default {kconfig.database_path()})")
    args = ap.parse_args(argv)
    klogs.setup_logging()  # console, plus the file of [logs] file
    cfg = kconfig.load_config()
    host = args.host or kconfig.web_host(cfg)  # a flag overrides kiko.toml
    port = args.port if args.port is not None else kconfig.web_port(cfg)
    started = kconfig.server_values(cfg)  # what this process runs with, for the "restart needed" notice
    started["web.host"], started["web.port"] = host, port

    db = Database(default_path(args.db))
    password_file = kconfig.password_file("web")
    extra = frozenset(h.strip().lower() for h in os.environ.get("KIKO_ALLOWED_HOSTS", "").split(",") if h.strip())
    ui = WebUI(db, bind=host, allowed_hosts=extra, password_file=password_file, started=started)
    if ui.auth.ensure_password():
        print("first start: login with the default password, then change it", flush=True)
    if not is_loopback_host(host) and ui.auth.is_default():
        print(f"kiko web: refusing to listen on {host}: the admin password is still the default. "
              "Start on 127.0.0.1, log in, change the password, then start again.", file=sys.stderr, flush=True)
        return 2
    if not is_loopback_host(host):
        logging.getLogger("kiko.web").warning("bound to %s: plain HTTP, reachable from the network", host)
    print(f"kiko web on http://{host}:{port}  db={db.path}", flush=True)
    kserver.serve(ui.build_app(), host, port, allowed_hosts=extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
