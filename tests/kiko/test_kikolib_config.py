# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.config: kiko.toml loader (missing / broken file never raises)."""
from kikolib import config


def test_missing_and_broken_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    assert config.load_config() == {}
    (tmp_path / "kiko.toml").write_text("this is = = not toml", encoding="utf-8")
    assert config.load_config() == {}
    assert config.webui_port() == 4001 and config.proxy_port() == 4000


def test_values(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("KIKO_DB_PATH", raising=False)
    (tmp_path / "kiko.toml").write_text('[webui]\nport = 5000\nhost = "127.0.0.1"\n[database]\npath = "x.db"\n',
                                        encoding="utf-8")
    assert config.webui_port() == 5000 and config.webui_host() == "127.0.0.1"
    assert config.database_path() == tmp_path / "x.db"
    assert config.get("webui.nope", 7) == 7 and config.get("webui.port.deeper", 1) == 1


# ---- TASK #124: one source for paths and ports ----
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _toml(tmp_path, text):
    (tmp_path / "kiko.toml").write_text(text, encoding="utf-8")


def test_paths_come_from_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("KIKO_DB_PATH", raising=False)
    assert config.config_dir() == tmp_path and config.toml_path() == tmp_path / "kiko.toml"
    assert config.database_path() == tmp_path / "kiko.db" and config.env_path() == tmp_path / ".env"
    assert config.log_dir() == tmp_path / "logs"
    assert config.password_file("web") == tmp_path / "web-initial-password"
    assert config.password_file("webui") == tmp_path / "web-ui-initial-password"
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "other.db"))
    assert config.database_path() == tmp_path / "other.db"
    monkeypatch.delenv("KIKO_CONFIG_DIR")
    assert config.config_dir() == Path.home() / ".kiko"


def test_ports_from_base_and_per_section_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    assert (config.proxy_port(), config.web_port(), config.spy_port()) == (4000, 4001, 4002)
    assert config.proxy_host() == config.web_host() == config.spy_host() == "127.0.0.1"
    _toml(tmp_path, "[kiko]\nbase_port = 5000\n")
    assert (config.base_port(), config.proxy_port(), config.web_port(), config.spy_port()) == (5000, 5000, 5001, 5002)
    _toml(tmp_path, '[kiko]\nbase_port = 5000\n[web]\nport = 6001\nhost = "0.0.0.0"\n[spy]\nport = 7000\n')
    assert (config.proxy_port(), config.web_port(), config.spy_port()) == (5000, 6001, 7000)
    assert config.web_host() == "0.0.0.0" and config.proxy_host() == "127.0.0.1"
    _toml(tmp_path, "[webui]\nport = 4999\n")  # the older section name still counts for the web
    assert config.web_port() == 4999
    _toml(tmp_path, "[webui]\nport = 4999\n[web]\nport = 4888\n")
    assert config.web_port() == 4888


def test_invalid_values_fall_back_to_the_default(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    _toml(tmp_path, '[kiko]\nbase_port = 99999\n[proxy]\nport = "x"\nhost = ""\n[web]\nport = 0\n')
    assert (config.base_port(), config.proxy_port(), config.web_port()) == (4000, 4000, 4001)
    assert config.proxy_host() == "127.0.0.1" and "base_port" in caplog.text


def test_two_installs_side_by_side(tmp_path):
    a, b = tmp_path / "old", tmp_path / "new"
    a.mkdir()
    b.mkdir()
    (b / "kiko.toml").write_text("[kiko]\nbase_port = 5000\n", encoding="utf-8")
    outs = []
    for d in (a, b):
        r = subprocess.run([sys.executable, "-m", "kiko_lite", "config", "--json"], cwd=ROOT, capture_output=True,
                           text=True, timeout=60, env={**{k: v for k, v in os.environ.items() if k != "KIKO_DB_PATH"},
                                      "KIKO_CONFIG_DIR": str(d), "PYTHONPATH": str(ROOT)})
        assert r.returncode == 0, r.stderr
        outs.append(json.loads(r.stdout))
    old, new = outs
    assert (old["proxy_port"], old["web_port"], old["spy_port"]) == (4000, 4001, 4002)
    assert (new["proxy_port"], new["web_port"], new["spy_port"]) == (5000, 5001, 5002)
    assert Path(old["database_path"]) != Path(new["database_path"]) and Path(new["database_path"]).parent == b
    assert new["anthropic_base_url"] == "http://127.0.0.1:5000"


def test_settings_table_is_complete():
    names = [s.name for s in config.SETTINGS]
    assert len(names) == len(set(names))
    for s in config.SETTINGS:
        assert s.help and s.used_by and s.group in config.GROUPS and s.type
    assert {"kiko.base_port", "proxy.host", "proxy.port", "proxy.no_auth", "web.host", "web.port", "spy.host",
            "spy.port", "proxy.upstream_timeout", "proxy.probe_timeout", "billing.monthly_cap_usd", "billing.disable_billing", "health.check_interval",
            "health.no_probe", "logs.retention_days", "logs.store_text", "logs.file"} == set(names)
    assert config.BY_NAME["spy.port"].used_by == "spy (not built yet)"


@pytest.mark.parametrize("name,raw,ok", [
    ("proxy.port", "1", True), ("proxy.port", "65535", True), ("proxy.port", "0", False), ("proxy.port", "65536", False),
    ("proxy.port", "abc", False), ("proxy.port", "1.5", False), ("kiko.base_port", "65534", False),
    ("proxy.host", "0.0.0.0", True), ("proxy.host", "localhost", True), ("proxy.host", "bad host", False),
    ("proxy.host", "::1", True), ("billing.monthly_cap_usd", "12.5", True), ("billing.monthly_cap_usd", "-1", False),
    ("billing.monthly_cap_usd", "nan", False), ("logs.retention_days", "-3", False), ("logs.retention_days", "30", True),
    ("logs.file", "a\nb", False), ("health.check_interval", "x", False),
])
def test_parse_value_validation(name, raw, ok):
    s = config.BY_NAME[name]
    if ok:
        config.parse_value(s, raw)
    else:
        with pytest.raises(ValueError):
            config.parse_value(s, raw)


def test_typed_values_toml_first_then_db_then_default(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    db = {"monthly_cap_usd": "7", "disable_billing": "true", "health_check_interval": "60", "health_no_probe": "a,b"}
    assert config.effective("billing.monthly_cap_usd", None, db) == (7.0, "db")
    assert config.effective("billing.disable_billing", None, db) == (True, "db")
    assert config.effective("billing.monthly_cap_usd", None, {}) == (None, "default")
    _toml(tmp_path, "[billing]\nmonthly_cap_usd = 9.5\ndisable_billing = false\n[health]\ncheck_interval = 0\n")
    assert config.effective("billing.monthly_cap_usd", None, db) == (9.5, "file")
    assert config.effective("billing.disable_billing", None, db) == (False, "file")  # the file wins, also false
    assert config.effective("health.no_probe", None, db) == ("a,b", "db")  # key absent in the file: DB fallback
    over = config.overlay_settings(db)
    assert over["monthly_cap_usd"] == "9.5" and over["disable_billing"] == "false"
    assert over["health_check_interval"] == "0" and over["health_no_probe"] == "a,b"
    assert db["monthly_cap_usd"] == "7"  # the input is not changed


def test_check_no_auth():
    assert config.check_no_auth("127.0.0.1", True) is None and config.check_no_auth("localhost", True) is None
    assert config.check_no_auth("::1", True) is None and config.check_no_auth("0.0.0.0", False) is None
    for host in ("0.0.0.0", "192.168.1.5", "::", "example.com"):
        assert "refusing" in config.check_no_auth(host, True)


def test_log_file_is_relative_to_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    assert config.log_file() is None
    _toml(tmp_path, '[logs]\nfile = "logs/k.log"\n')
    assert config.log_file() == tmp_path / "logs" / "k.log"
