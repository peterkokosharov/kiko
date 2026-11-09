# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Start the spy: ``python -m spy [--host HOST] [--port PORT]`` (defaults: kiko.toml via kikolib.config)."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from kikolib import config as kconfig
from kikolib import server as kserver

from .app import default_spy


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m spy", description="kiko spy: read-only live log viewer "
                                 "(loopback by default)")
    ap.add_argument("--host", default=None, help=f"bind address (default {kconfig.spy_host()}: [spy] host of "
                    f"{kconfig.TOML_NAME})")
    ap.add_argument("--port", type=int, default=None, help=f"port (default {kconfig.spy_port()}: [spy] port or "
                    f"base_port + 2 of {kconfig.TOML_NAME})")
    args = ap.parse_args(argv)
    cfg = kconfig.load_config()
    host = args.host or kconfig.spy_host(cfg)  # a flag overrides kiko.toml
    port = args.port if args.port is not None else kconfig.spy_port(cfg)
    spy = default_spy()
    spy.start()
    extra = frozenset(h.strip().lower() for h in os.environ.get("KIKO_ALLOWED_HOSTS", "").split(",") if h.strip())
    if host.strip().lower() not in ("127.0.0.1", "::1", "localhost"):
        logging.getLogger("kiko.spy").warning("bound to %s: plain HTTP, the logs are readable from the network", host)
    print(f"kiko spy on http://{host}:{port}  log={kconfig.log_file(cfg)}  db={kconfig.database_path(cfg)}",
          flush=True)
    try:
        kserver.serve(spy.build_app(), host, port, allowed_hosts=extra, drain_seconds=2.0)
    finally:
        spy.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
