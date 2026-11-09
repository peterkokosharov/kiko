# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #124: the code that uses the settings really reads kikolib.config (serve flags, DB fallback, no-auth refusal,
log file, retention, config --json, the .env place). Temp dirs only: KIKO_CONFIG_DIR points into tmp_path."""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from kiko_lite import cli, keys
from kiko_lite.db import Database
from kiko_lite.resolver import Catalog, cap_gate
from kikolib import config, logs
from kikolib import server as kserver

MASTER = "master-value-not-to-leak-123"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(d))
    monkeypatch.delenv("KIKO_DB_PATH", raising=False)
    monkeypatch.delenv("KIKO_AUTH", raising=False)
    monkeypatch.setenv("MASTER_KEY", MASTER)  # env wins: the OS key store is never asked
    return d


def toml(cfg, text):
    (cfg / "kiko.toml").write_text(text, encoding="utf-8")


@pytest.fixture
def served(monkeypatch):
    seen = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: seen.append((host, port)))
    return seen


# ---- serve: host and port ----
def test_serve_reads_host_and_port_from_the_file_and_the_flag_overrides(cfg, served):
    assert cli.main(["serve"]) == 0 and served[-1] == ("127.0.0.1", 4000)
    toml(cfg, '[kiko]\nbase_port = 5000\n')
    assert cli.main(["serve"]) == 0 and served[-1] == ("127.0.0.1", 5000)
    toml(cfg, '[kiko]\nbase_port = 5000\n[proxy]\nport = 5555\nhost = "0.0.0.0"\n')
    assert cli.main(["serve"]) == 0 and served[-1] == ("0.0.0.0", 5555)
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "6001"]) == 0 and served[-1] == ("127.0.0.1", 6001)


def test_serve_help_shows_the_resolved_values(cfg, capsys):
    toml(cfg, "[kiko]\nbase_port = 5000\n")
    with pytest.raises(SystemExit):
        cli.main(["serve", "--help"])
    assert "now 5000" in capsys.readouterr().out


def test_server_defaults_come_from_the_proxy_settings(cfg):
    toml(cfg, "[proxy]\nport = 0\n")  # invalid port 0 is ignored -> default
    toml(cfg, '[kiko]\nbase_port = 5000\n')
    srv = kserver.Server(kserver.App(), "127.0.0.1", 0)
    srv.close()
    assert config.proxy_port() == 5000


# ---- no_auth ----
@pytest.mark.parametrize("how", ["flag", "file", "env"])
def test_no_auth_is_refused_on_a_non_loopback_host(cfg, served, capsys, monkeypatch, how):
    argv = ["serve", "--host", "0.0.0.0"]
    if how == "flag":
        argv.append("--no-auth")
    elif how == "file":
        toml(cfg, "[proxy]\nno_auth = true\n")
    else:
        monkeypatch.setenv("KIKO_AUTH", "none")
    assert cli.main(argv) == 2 and served == []
    assert "refusing no-auth on 0.0.0.0" in capsys.readouterr().err


def test_no_auth_from_the_file_is_fine_on_loopback_and_with_a_key_on_the_network(cfg, served):
    toml(cfg, "[proxy]\nno_auth = true\n")
    assert cli.main(["serve"]) == 0 and served
    toml(cfg, '[proxy]\nhost = "0.0.0.0"\n')  # no no_auth: a client key is required, fine
    assert cli.main(["serve"]) == 0 and served[-1] == ("0.0.0.0", 4000)


# ---- moved settings: file first, DB fallback ----
def test_cap_gate_reads_the_file_first_and_the_db_as_fallback(cfg, tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_billing("m", 1, 1, 5.0)
    db.set_setting("monthly_cap_usd", "1000")
    catalog = Catalog(db, ttl=0)
    gate = cap_gate(db, catalog)
    assert gate() is None  # file silent: the DB value 1000 applies
    toml(cfg, "[billing]\nmonthly_cap_usd = 1.0\n")
    assert "cap of 1.00 USD" in gate()  # the file wins
    toml(cfg, "[health]\ncheck_interval = 0\n")
    assert gate() is None  # key absent again: DB fallback
    assert db.settings()["monthly_cap_usd"] == "1000"  # DB rows are never deleted


def test_snapshot_carries_billing_and_health_settings_from_the_file(cfg, tmp_path):
    db = Database(tmp_path / "t.db")
    db.set_setting("disable_billing", "true")
    db.set_setting("health_no_probe", "old")
    catalog = Catalog(db, ttl=0)
    assert catalog.snapshot().settings["disable_billing"] == "true"
    toml(cfg, '[billing]\ndisable_billing = false\n[health]\nno_probe = "a,b"\ncheck_interval = 45\n')
    s = catalog.snapshot().settings
    assert (s["disable_billing"], s["health_no_probe"], s["health_check_interval"]) == ("false", "a,b", "45")


def test_health_prober_start_reads_the_interval_from_the_file(cfg, tmp_path):
    from kiko_lite.resolver import build_db_app
    db = Database(tmp_path / "t.db")
    toml(cfg, "[health]\ncheck_interval = 0\n")
    app = build_db_app(db, MASTER)
    assert app.health.start() is False  # 0 = off, from the file


# ---- log file ----
@pytest.fixture
def clean_root_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()


def test_log_file_handler_relative_to_the_config_dir(cfg, clean_root_handlers):
    toml(cfg, '[logs]\nfile = "logs/kiko.log"\n')
    assert logs.setup_logging() == cfg / "logs" / "kiko.log"
    logging.getLogger("kiko.test").warning("hello-from-the-test")
    for h in clean_root_handlers.handlers:
        h.flush()
    assert "hello-from-the-test" in (cfg / "logs" / "kiko.log").read_text(encoding="utf-8")
    n = len(clean_root_handlers.handlers)
    logs.setup_logging()  # again: no second handler for the same file
    assert len(clean_root_handlers.handlers) == n


def test_no_log_file_without_the_setting_and_a_bad_path_is_harmless(cfg, clean_root_handlers, tmp_path):
    n = len(clean_root_handlers.handlers)
    assert logs.setup_logging() is None and len(clean_root_handlers.handlers) == n
    (cfg / "file").write_text("x", encoding="utf-8")  # a file where the log folder should be
    toml(cfg, '[logs]\nfile = "file/sub/x.log"\n')
    assert logs.setup_logging() is None  # cannot open: a warning, no exception


# ---- retention ----
def _seed_old_and_new(db):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db.add_billing("old", 1, 1, 1.0, ts=old)
    db.add_billing("new", 1, 1, 1.0)
    old_sql = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db.path) as con:
        con.execute("INSERT INTO responses(id, input_json, output_json, created_at) VALUES('o','[]','[]',?)", (old_sql,))
        con.execute("INSERT INTO responses(id, input_json, output_json) VALUES('n','[]','[]')")


def _rows(db):
    with sqlite3.connect(db.path) as con:
        return ([r[0] for r in con.execute("SELECT model FROM billing")],
                [r[0] for r in con.execute("SELECT id FROM responses")])


def test_prune_logs_deletes_only_old_rows(tmp_path):
    db = Database(tmp_path / "t.db")
    _seed_old_and_new(db)
    assert db.prune_logs(30) == 2
    assert _rows(db) == (["new"], ["n"])
    assert db.prune_logs(30) == 0


def test_pruner_runs_at_start_and_follows_the_setting(tmp_path):
    db = Database(tmp_path / "t.db")
    _seed_old_and_new(db)
    days = [0]
    p = logs.Pruner(db.prune_logs, lambda: days[0])
    assert p.run_once() == 0 and len(_rows(db)[0]) == 2  # 0 = keep everything
    days[0] = 30
    assert p.run_once() == 2 and _rows(db) == (["new"], ["n"])
    boom = logs.Pruner(lambda d: 1 / 0, lambda: 5)
    assert boom.run_once() == 0  # an error is logged, never raised


def test_pruner_thread_runs_once_at_start_then_waits(tmp_path):
    calls = []
    p = logs.Pruner(lambda d: calls.append(d) or 0, lambda: 7, interval=3600)
    p.start()
    p.stop()
    assert calls == [7]


def test_serve_prunes_at_start_with_retention_days_from_the_file(cfg, served, tmp_path):
    db_path = tmp_path / "p.db"
    _seed_old_and_new(Database(db_path))
    toml(cfg, "[logs]\nretention_days = 30\n")
    assert cli.main(["serve", "--db", str(db_path)]) == 0
    assert _rows(Database(db_path)) == (["new"], ["n"])


# ---- config --json ----
def test_config_json_prints_resolved_values_and_no_secret(cfg, capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-value-xyz")
    toml(cfg, '[kiko]\nbase_port = 5000\n[web]\nport = 5100\n[billing]\nmonthly_cap_usd = 3.5\n')
    assert cli.main(["config", "--json"]) == 0
    out = capsys.readouterr().out
    assert MASTER not in out and "gemini-secret-value-xyz" not in out
    info = json.loads(out)
    assert (info["proxy_port"], info["web_port"], info["spy_port"], info["base_port"]) == (5000, 5100, 5002, 5000)
    assert info["proxy_host"] == info["web_host"] == info["spy_host"] == "127.0.0.1"
    assert info["config_dir"] == str(cfg) and info["toml_path"] == str(cfg / "kiko.toml")
    assert info["database_path"] == str(cfg / "kiko.db") and info["env_path"] == str(cfg / ".env")
    assert info["anthropic_base_url"] == "http://127.0.0.1:5000"
    assert info["settings"]["billing.monthly_cap_usd"] == 3.5 and info["settings"]["spy.port"] == 5002
    assert info["settings"]["logs.retention_days"] == 0 and info["settings"]["proxy.no_auth"] is False
    assert "\n  \"proxy_port\": 5000," in out  # one key per line: a shell script can sed it
    assert not (cfg / "kiko.db").exists()  # showing the config never creates the database


def test_config_json_uses_the_db_fallback_and_the_db_flag(cfg, capsys, tmp_path):
    db = Database(tmp_path / "x.db")
    db.set_setting("monthly_cap_usd", "8")
    assert cli.main(["config", "--json", "--db", str(db.path)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["database_path"] == str(db.path) and info["settings"]["billing.monthly_cap_usd"] == 8.0


def test_config_get_and_plain_output(cfg, capsys):
    toml(cfg, "[kiko]\nbase_port = 5000\n")
    assert cli.main(["config", "--get", "web_port"]) == 0 and capsys.readouterr().out.strip() == "5001"
    assert cli.main(["config", "--get", "nope"]) == 2
    capsys.readouterr()
    assert cli.main(["config"]) == 0 and "proxy_port: 5000" in capsys.readouterr().out


# ---- .env place ----
def test_env_file_is_in_the_config_dir_with_a_one_time_fallback(cfg, tmp_path, caplog):
    db_path = tmp_path / "elsewhere" / "k.db"
    db_path.parent.mkdir()
    assert keys.env_file_path(db_path) == cfg / ".env"
    (db_path.parent / ".env").write_text("OLD_KEY=old\n", encoding="utf-8")
    keys._warned.clear()
    with caplog.at_level(logging.WARNING):
        assert keys.dotenv_values(db_path) == {"OLD_KEY": "old"}
        keys.dotenv_values(db_path)
    assert caplog.text.count("old place") == 1  # one warning only
    (cfg / ".env").write_text("NEW_KEY=new\n", encoding="utf-8")
    assert keys.dotenv_values(db_path) == {"NEW_KEY": "new"}  # the config dir wins


def test_set_setting_notes_that_the_file_wins(cfg, capsys, tmp_path):
    toml(cfg, "[billing]\nmonthly_cap_usd = 1\n")
    assert cli.main(["set-setting", "monthly_cap_usd", "5", "--db", str(tmp_path / "s.db")]) == 0
    assert "file wins" in capsys.readouterr().out
