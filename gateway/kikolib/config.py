# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The one place for kiko's configuration: paths, ports and settings (standard library ``tomllib``, Python 3.11+).

Every kiko module takes paths and ports only from here; nothing else hard-codes them.

* The config directory is ``$KIKO_CONFIG_DIR``, else ``~/.kiko``. Its ``kiko.toml`` holds all settings; the
  database, the ``.env`` file, the logs and the first-start password files live there too (the database path
  can be moved with ``$KIKO_DB_PATH`` or ``[database] path``). Two installs side by side differ in
  ``KIKO_CONFIG_DIR`` only.
* Ports come from a base: ``[kiko] base_port`` (default 4000) gives proxy = base, web = base + 1, spy = base + 2.
  Each server can be overridden in its own section (``[proxy] port``, ``[web] port``, ``[spy] port``).
* ``SETTINGS`` is the single table (name, section, type, default, help, used-by) that drives both the typed getters
  and the web settings form. Command-line flags override the file; the callers do that.

Nothing is read at import time: every call reads the file again, and a missing or broken file (or a bad value in
it) reads as the default with a logged warning, so a typo in kiko.toml can never stop a program from starting.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

logger = logging.getLogger("kikolib.config")
_warned_log: set[str] = set()

CONFIG_DIR_ENV = "KIKO_CONFIG_DIR"
DB_PATH_ENV = "KIKO_DB_PATH"
_DIR_NAME = ".kiko"
TOML_NAME = "kiko.toml"
DB_NAME = "kiko.db"
ENV_NAME = ".env"
LOG_DIR_NAME = "logs"
PASSWORD_FILES = {"web": "web-initial-password", "webui": "web-ui-initial-password"}  # new web UI, old web-ui
DEFAULT_BASE_PORT = 4000
SERVERS = ("proxy", "web", "spy")  # port = base + index
DEFAULT_HOST = "127.0.0.1"
MAX_BASE_PORT = 65535 - (len(SERVERS) - 1)
MAX_RETENTION_DAYS = 3650
MAX_TIMEOUT_SECONDS = 86400.0  # one day: an upstream timeout above it is a typo
TIMEOUT_KEYS = ("upstream_timeout", "probe_timeout")  # [proxy] seconds; must be > 0 (a socket timeout of 0 never waits)
LEGACY_WEB_SECTION = "webui"  # old name of [web] (host / port only), still read when [web] has no value


# ---- the table ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    """One setting. ``type``: port, base_port, host, int, float, bool, str, csv. ``default`` None = derived or unset
    (``default_text`` says which). ``db_key``: the key it had in the DB settings table (a fallback when the toml
    key is absent)."""

    group: str
    section: str
    key: str
    type: str
    default: Any
    help: str
    used_by: str
    default_text: str = ""
    db_key: str | None = None
    restart: bool = False  # a server value: a running process keeps what it started with

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"

    @property
    def shown_default(self) -> str:
        if self.default_text:
            return self.default_text
        if self.default is None or self.default == "":
            return "(unset)"
        return str(self.default).lower() if isinstance(self.default, bool) else str(self.default)


def _server(name: str, index: int, used_by: str, note: str = "") -> tuple[Setting, ...]:
    return (
        Setting("Servers", name, "host", "host", DEFAULT_HOST, f"Address the {name} listens on. Anything but "
                f"loopback (127.0.0.1, ::1, localhost) makes it reachable from the network.{note}", used_by,
                restart=True),
        Setting("Servers", name, "port", "port", None, f"Port of the {name}.{note}", used_by,
                default_text=f"base_port + {index}", restart=True),
    )


SETTINGS: tuple[Setting, ...] = (
    Setting("Servers", "kiko", "base_port", "base_port", DEFAULT_BASE_PORT,
            "Base port: proxy = base, web = base + 1, spy = base + 2. Two installs side by side use different bases "
            "(and different KIKO_CONFIG_DIR).", "python -m kiko_lite serve, python -m web", restart=True),
    *_server("proxy", 0, "python -m kiko_lite serve"),
    Setting("Servers", "proxy", "no_auth", "bool", False,
            "Home mode: the proxy accepts requests without a client key (MASTER_KEY ignored). Refused unless the "
            "proxy host is loopback.", "python -m kiko_lite serve", restart=True),
    Setting("Servers", "proxy", "upstream_timeout", "float", 300.0,
            "Seconds the proxy waits on a model without any answer: for the response head (the first byte), then "
            "between two chunks of the answer. A long streaming answer is never cut while bytes keep coming; it is "
            "not a limit on the whole answer. Slow free models may need more. A model's own timeout (add-model "
            "--timeout, set-timeout, the Models page) wins. More than 0, at most "
            f"{int(MAX_TIMEOUT_SECONDS)}; serve --upstream-timeout overrides it. Read when the proxy starts.",
            "python -m kiko_lite serve", restart=True),
    Setting("Servers", "proxy", "probe_timeout", "float", 20.0,
            "Seconds a background health probe waits on a model (same meaning as upstream_timeout). A model with "
            "its own timeout is probed with that timeout. More than 0. Read when the proxy starts.",
            "python -m kiko_lite serve (health prober)", restart=True),
    *_server("web", 1, "python -m web, web-ui/main.py"),
    *_server("spy", 2, "spy (not built yet)", " Used by spy (not built yet)."),
    Setting("Billing", "billing", "monthly_cap_usd", "float", None,
            "Monthly spending cap in USD: when this month's spend reaches it, requests are refused with 429. "
            "Empty = no cap in the file (an old cap that only exists in the database stays; it is shown in the box). "
            "Type 0 to turn the cap off: 0 means no cap, it does not block everything.", "the proxy (cap gate, doctor), the dashboard",
            db_key="monthly_cap_usd"),
    Setting("Billing", "billing", "disable_billing", "bool", False,
            "Do not write billing rows (no costs, no cap tracking).", "the proxy (billing writer)",
            db_key="disable_billing"),
    Setting("Health", "health", "check_interval", "float", 300.0,
            "Seconds between background health probes of failing models (0 = off; below 10 is raised to 10). "
            "Probes cost free-tier quota or money. Read when the proxy starts.", "the proxy (health prober)",
            db_key="health_check_interval"),
    Setting("Health", "health", "no_probe", "csv", "",
            "Model aliases (comma separated) the health prober must never probe.", "the proxy (health prober)",
            db_key="health_no_probe"),
    Setting("Logs", "logs", "retention_days", "int", 0,
            "Delete billing rows and stored responses older than this many days, at proxy start and once a day "
            f"(0 = keep everything; at most {MAX_RETENTION_DAYS}).", "the proxy (log pruner)"),
    Setting("Logs", "logs", "store_text", "bool", True,
            "Store the prompts and answers of every request in kiko.db (the Text log page of the web UI). "
            "false = store nothing; the billing rows are not affected. Old rows are pruned by retention_days.",
            "the proxy (text log)"),
    Setting("Logs", "logs", "file", "str", "",
            "Also write the log to this file: a relative path inside the config folder, ending in .log (no absolute "
            "path, drive, colon or ..; not a kiko file name). Empty = no file. Read when the proxy or the web starts.", "the proxy, the web"),
)
BY_NAME: dict[str, Setting] = {s.name: s for s in SETTINGS}
GROUPS = tuple(dict.fromkeys(s.group for s in SETTINGS))
SERVER_SETTINGS = tuple(s for s in SETTINGS if s.restart)


def parse_value(setting: Setting, raw: Any) -> Any:
    """Typed, validated value of ``raw`` (a form string or a toml value); ``ValueError`` with a plain message.

    Returns None for an empty string on a type that can be unset."""
    t = setting.type
    label = setting.name
    if isinstance(raw, str):
        raw = raw.strip()
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return raw.lower() in ("true", "1", "yes", "on")
        raise ValueError(f"{label}: expected true or false")
    if raw is None or raw == "":
        return None
    if t in ("port", "base_port", "int"):
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            raise ValueError(f"{label}: expected a whole number")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: expected a whole number") from None
        if t == "port" and not 1 <= n <= 65535:
            raise ValueError(f"{label}: a port must be 1-65535")
        if t == "base_port" and not 1 <= n <= MAX_BASE_PORT:
            raise ValueError(f"{label}: the base port must be 1-{MAX_BASE_PORT} (spy uses base + 2)")
        if t == "int" and n < 0:
            raise ValueError(f"{label}: must not be negative")
        if setting.name == "logs.retention_days" and n > MAX_RETENTION_DAYS:
            raise ValueError(f"{label}: at most {MAX_RETENTION_DAYS} days")
        return n
    if t == "float":
        if isinstance(raw, bool):
            raise ValueError(f"{label}: expected a number")
        try:
            x = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: expected a number") from None
        if not math.isfinite(x) or x < 0:
            raise ValueError(f"{label}: expected a number of 0 or more")
        if setting.key in TIMEOUT_KEYS and not 0 < x <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"{label}: seconds, more than 0 and at most {int(MAX_TIMEOUT_SECONDS)}")
        return x
    if not isinstance(raw, str):
        raise ValueError(f"{label}: expected text")
    if any(ord(c) < 32 or ord(c) == 127 or c in LINE_SEPARATORS for c in raw):
        raise ValueError(f"{label}: must not contain control or line separator characters")
    if t == "host":
        if not is_valid_host(raw):
            raise ValueError(f"{label}: not a valid host name or IP address")
        return raw.strip("[]")  # an IPv6 address is bound without brackets
    if setting.name == "logs.file":
        problem = log_path_problem(raw)
        if problem:
            raise ValueError(f"{label}: {problem}")
        return raw
    if t == "csv":
        return ",".join(p.strip() for p in raw.split(",") if p.strip())
    return raw


LINE_SEPARATORS = chr(0x85) + chr(0x2028) + chr(0x2029)


def log_path_problem(raw: str) -> str:
    """Why ``raw`` is not allowed as ``[logs] file`` (it must stay inside the config folder), or ``""``."""
    for pure in (PurePosixPath(raw), PureWindowsPath(raw)):
        if pure.anchor or pure.drive or pure.root:
            return "must be a relative path inside the config folder (no absolute path, no drive)"
        if ".." in pure.parts:
            return "must stay inside the config folder (no ..)"
    if raw.startswith(("/", "\\")) or raw[1:2] == ":":
        return "must be a relative path inside the config folder (no absolute path, no drive)"
    if ":" in raw:
        return "must not contain a colon"
    if not raw.lower().endswith(".log"):
        return "must end in .log"
    reserved = {TOML_NAME, ENV_NAME, *PASSWORD_FILES.values()}
    for part in raw.replace(chr(92), "/").split("/"):
        low = part.lower()
        if low in reserved or low.startswith(DB_NAME):
            return f"{part} is a kiko file; pick another name"
    return ""


def cap_label(raw: Any) -> str:
    """Text for a monthly cap setting: "none", "0 (no cap)" (0 means no cap) or the number."""
    if raw in (None, ""):
        return "none"
    try:
        return "0 (no cap)" if float(raw) <= 0 else str(raw)
    except (TypeError, ValueError):
        return str(raw)


CAP_ZERO_NOTE = ("the monthly cap in the database is 0 or less: 0 now means no cap (before it blocked every request); "
                 "delete it or set a positive number")


def db_cap_zero(db_settings: Mapping[str, str], cfg: dict[str, Any] | None = None) -> bool:
    """True when the cap that applies comes from the DB and is <= 0."""
    v, source = effective("billing.monthly_cap_usd", cfg, db_settings)
    return source == "db" and v is not None and v <= 0


def is_valid_host(host: str) -> bool:
    h = host.strip("[]")
    if not h or len(h) > 253:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return all(p and len(p) <= 63 and p[0] != "-" and p[-1] != "-" and all(c.isalnum() or c == "-" for c in p)
               for p in h.split("."))


def is_loopback_host(host: str) -> bool:
    h = host.strip().lower().strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# ---- directories and files ---------------------------------------------------------------------------

def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$KIKO_CONFIG_DIR`` if set, else ``~/.kiko``."""
    value = (os.environ if env is None else env).get(CONFIG_DIR_ENV)
    return Path(value).expanduser() if value else Path.home() / _DIR_NAME


find_config_dir = config_dir  # the older name


def toml_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / TOML_NAME


def env_path(env: Mapping[str, str] | None = None) -> Path:
    """The ``.env`` file with API key values (names of the keys are in the DB, values here, in env or the keystore)."""
    return config_dir(env) / ENV_NAME


def log_dir(env: Mapping[str, str] | None = None) -> Path:
    """Default folder for log files (``<config dir>/logs``); ``[logs] file`` must stay inside the config dir."""
    return config_dir(env) / LOG_DIR_NAME


def password_file(kind: str = "web", env: Mapping[str, str] | None = None) -> Path:
    """First-start password file: ``kind`` "web" (new web UI) or "webui" (old web-ui)."""
    return config_dir(env) / PASSWORD_FILES[kind]


def load_config(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Parsed ``kiko.toml`` of the config directory; ``{}`` when missing or invalid."""
    path = toml_path(env)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        logger.warning("cannot read %s: %s", path, type(e).__name__)
        return {}


def get(key: str, default: Any = None, config: dict[str, Any] | None = None) -> Any:
    """Nested raw value by dotted key (``"web.port"``); ``default`` when absent."""
    value: Any = load_config() if config is None else config
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return default if value is None else value


def database_path(config: dict[str, Any] | None = None, env: Mapping[str, str] | None = None) -> Path:
    """``$KIKO_DB_PATH``, else ``[database] path`` of kiko.toml (relative to the config dir), else
    ``<config dir>/kiko.db``."""
    environ = os.environ if env is None else env
    if environ.get(DB_PATH_ENV):
        return Path(environ[DB_PATH_ENV])
    if config is None:
        config = load_config(env)
    return (config_dir(env) / str(get("database.path", DB_NAME, config))).expanduser()


# ---- typed values ------------------------------------------------------------------------------------

def _file_value(setting: Setting, cfg: dict[str, Any]) -> Any:
    """Valid value of ``setting`` in the parsed toml, else None (absent, empty or invalid: a warning for invalid)."""
    section = cfg.get(setting.section)
    if not isinstance(section, dict) or setting.key not in section:
        return None
    try:
        return parse_value(setting, section[setting.key])
    except ValueError as e:
        logger.warning("kiko.toml: %s; using the default", e)
        return None


def base_port(cfg: dict[str, Any] | None = None) -> int:
    cfg = load_config() if cfg is None else cfg
    value = _file_value(BY_NAME["kiko.base_port"], cfg)
    return DEFAULT_BASE_PORT if value is None else value


def server_host(name: str, cfg: dict[str, Any] | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    value = _file_value(BY_NAME[f"{name}.host"], cfg)
    if value is None and name == "web":
        value = _legacy_web(cfg, "host")
    return DEFAULT_HOST if value is None else value


def server_port(name: str, cfg: dict[str, Any] | None = None) -> int:
    """Own ``[name] port``, else base + the server's index."""
    cfg = load_config() if cfg is None else cfg
    value = _file_value(BY_NAME[f"{name}.port"], cfg)
    if value is None and name == "web":
        value = _legacy_web(cfg, "port")
    return base_port(cfg) + SERVERS.index(name) if value is None else value


def _legacy_web(cfg: dict[str, Any], key: str) -> Any:
    section = cfg.get(LEGACY_WEB_SECTION)
    if not isinstance(section, dict) or key not in section:
        return None
    try:
        return parse_value(BY_NAME[f"web.{key}"], section[key])
    except ValueError:
        return None


def proxy_host(cfg: dict[str, Any] | None = None) -> str:
    return server_host("proxy", cfg)


def proxy_port(cfg: dict[str, Any] | None = None) -> int:
    return server_port("proxy", cfg)


def web_host(cfg: dict[str, Any] | None = None) -> str:
    return server_host("web", cfg)


def web_port(cfg: dict[str, Any] | None = None) -> int:
    return server_port("web", cfg)


def spy_host(cfg: dict[str, Any] | None = None) -> str:
    return server_host("spy", cfg)


def spy_port(cfg: dict[str, Any] | None = None) -> int:
    return server_port("spy", cfg)


webui_host = web_host  # the older names (the old web-ui)
webui_port = web_port


def effective(name: str, cfg: dict[str, Any] | None = None,
              db_settings: Mapping[str, str] | None = None) -> tuple[Any, str]:
    """(value, source) of one setting; source is "file", "db" (the old settings table) or "default"."""
    s = BY_NAME[name]
    cfg = load_config() if cfg is None else cfg
    if s.key == "base_port":
        v = _file_value(s, cfg)
        return (DEFAULT_BASE_PORT, "default") if v is None else (v, "file")
    if s.key in ("host", "port") and s.section in SERVERS:
        in_file = _file_value(s, cfg) is not None or (s.section == "web" and _legacy_web(cfg, s.key) is not None)
        value = server_host(s.section, cfg) if s.key == "host" else server_port(s.section, cfg)
        return value, "file" if in_file else "default"
    v = _file_value(s, cfg)
    if v is not None:
        return v, "file"
    if s.db_key and db_settings and db_settings.get(s.db_key) not in (None, ""):
        try:
            v = parse_value(s, db_settings[s.db_key])
        except ValueError:
            v = None
            logger.warning("settings.%s is not valid: using the default", s.db_key)
        if v is not None:
            return v, "db"
    return s.default, "default"


def value(name: str, cfg: dict[str, Any] | None = None, db_settings: Mapping[str, str] | None = None) -> Any:
    return effective(name, cfg, db_settings)[0]


def in_file(name: str, cfg: dict[str, Any] | None = None) -> bool:
    """True when the toml has a valid value for this setting."""
    s = BY_NAME[name]
    cfg = load_config() if cfg is None else cfg
    return _file_value(s, cfg) is not None


def _text(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def overlay_settings(db_settings: Mapping[str, str], cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """The DB settings dict with the moved settings read from kiko.toml first: a key that is in the toml wins, a key
    that is absent there keeps its DB value (the fallback). The DB rows are never changed."""
    cfg = load_config() if cfg is None else cfg
    out = dict(db_settings)
    for s in SETTINGS:
        if s.db_key:
            v = _file_value(s, cfg)
            if v is not None:
                out[s.db_key] = _text(v)
    return out


def server_values(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The server settings as the file says now (host, port per server, proxy no_auth, base port)."""
    cfg = load_config() if cfg is None else cfg
    out: dict[str, Any] = {"kiko.base_port": base_port(cfg), "proxy.no_auth": bool(value("proxy.no_auth", cfg))}
    for n in SERVERS:
        out[f"{n}.host"] = server_host(n, cfg)
        out[f"{n}.port"] = server_port(n, cfg)
    return out


def check_no_auth(host: str, no_auth: bool) -> str | None:
    """Error text when ``no_auth`` is combined with a host that is not loopback, else None."""
    if no_auth and not is_loopback_host(host):
        return (f"refusing no-auth on {host}: home mode without a client key is only allowed on loopback "
                "(127.0.0.1, ::1, localhost). Use a MASTER_KEY or bind to loopback.")
    return None


def log_file(cfg: dict[str, Any] | None = None, env: Mapping[str, str] | None = None) -> Path | None:
    """``[logs] file``: a relative path inside the config dir, or None when unset. A value that would leave the
    config dir (``..``, absolute, drive, or a symlink / junction pointing out) counts as unset, with one warning;
    this also covers a hand-edited kiko.toml."""
    cfg = load_config(env) if cfg is None else cfg
    v = value("logs.file", cfg)
    if not v:
        return None
    base = config_dir(env)
    try:
        target = base / v
        ok = not log_path_problem(v) and target.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError, RuntimeError):
        ok = False
    if not ok:
        if v not in _warned_log:
            _warned_log.add(v)
            logger.warning("[logs] file %r is not inside the config folder: no log file", v)
        return None
    return target


def resolved(args_db: str | os.PathLike[str] | None = None,
             db_settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Everything ``config --json`` shows: paths, hosts, ports and the settings. Never a secret."""
    cfg = load_config()
    out: dict[str, Any] = {
        "config_dir": str(config_dir()),
        "toml_path": str(toml_path()),
        "toml_exists": toml_path().is_file(),
        "database_path": str(Path(args_db) if args_db else database_path(cfg)),
        "env_path": str(env_path()),
        "log_dir": str(log_dir()),
        "log_file": str(log_file(cfg) or ""),
        "password_file": str(password_file("web")),
        "base_port": base_port(cfg),
    }
    for n in SERVERS:
        out[f"{n}_host"] = server_host(n, cfg)
        out[f"{n}_port"] = server_port(n, cfg)
    out["anthropic_base_url"] = f"http://{out['proxy_host']}:{out['proxy_port']}"
    out["settings"] = {s.name: value(s.name, cfg, db_settings) for s in SETTINGS}
    return out
