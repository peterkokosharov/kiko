# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The typed settings page (web/settings.py): it edits kiko.toml through the writer, validates, shows the restart
notice and the client URL, and every saved value is read back by the code that uses it. Temp dirs, ephemeral ports."""
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from kiko_lite.db import Database
from kikolib import config
from kikolib import server as kserver
from web import security
from web.ui import WebUI

from .test_web_ui import PASSWORD, Browser, Client, REPO, real_server  # noqa: F401  (the e2e fixture)

TOML = """# my own comment
[kiko]
base_port = 4000  # base

[proxy]
host = "127.0.0.1"
"""


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "kiko.db")
    db.set_setting(security.HASH_KEY, security.hash_password(PASSWORD, iterations=1000))
    ui = WebUI(db, password_file=tmp_path / "pw")
    srv = kserver.Server(ui.build_app(), "127.0.0.1", 0)
    srv.start()
    c = Client(srv.port)
    assert c.login()[0].status == 303
    yield c, db, ui, srv
    srv.request_stop()
    srv.close()


def file_data():
    return tomllib.loads(config.toml_path().read_text(encoding="utf-8"))


def test_form_has_every_setting_with_help_default_and_groups(env):
    c, *_ = env
    resp, html = c.get("/settings")
    assert resp.status == 200
    for s in config.SETTINGS:
        assert f'name="{s.name}"' in html, s.name
        assert s.shown_default in html
    for group in ("Servers", "Billing", "Health", "Logs"):
        assert f"<legend>{group}" in html
    assert "Used by: spy (not built yet)." in html and "Used by spy (not built yet)." in html
    assert "key=value" not in html and 'name="key"' not in html  # the raw editor is gone
    assert html.count('type="checkbox"') == 3  # proxy.no_auth, billing.disable_billing, logs.store_text


def test_client_url_line_follows_the_file(env):
    c, *_ = env
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:4000" in c.get("/settings")[1]
    config.toml_path().write_text('[kiko]\nbase_port = 5000\n[proxy]\nhost = "10.0.0.5"\n', encoding="utf-8")
    assert "ANTHROPIC_BASE_URL=http://10.0.0.5:5000" in c.get("/settings")[1]


def test_save_round_trip_file_changes_and_readers_use_it(env):
    c, db, *_ = env
    config.toml_path().write_text(TOML, encoding="utf-8")
    resp, html = c.post("/settings/save", {
        "kiko.base_port": "5000", "proxy.host": "127.0.0.1", "proxy.port": "", "web.port": "5100",
        "billing.monthly_cap_usd": "12.5", "billing.disable_billing": "on", "health.check_interval": "60",
        "health.no_probe": "a, b ,,c", "logs.retention_days": "30", "logs.file": "logs/k.log", "logs.store_text": "on"})
    assert resp.status == 303
    text = config.toml_path().read_text(encoding="utf-8")
    assert "# my own comment" in text and "base_port = 5000  # base" in text  # comments and order kept
    data = file_data()
    assert data["kiko"]["base_port"] == 5000 and data["web"]["port"] == 5100
    assert data["billing"] == {"monthly_cap_usd": 12.5, "disable_billing": True}
    assert data["health"] == {"check_interval": 60.0, "no_probe": "a,b,c"}
    assert data["logs"] == {"retention_days": 30, "file": "logs/k.log"}
    assert "port" not in data["proxy"]  # an empty box: no key
    # the readers: ports from the base, typed settings, the overlay the proxy's catalog uses, the log file
    assert (config.proxy_port(), config.web_port(), config.spy_port()) == (5000, 5100, 5002)
    assert config.value("billing.monthly_cap_usd") == 12.5 and config.value("logs.retention_days") == 30
    over = config.overlay_settings(db.settings())
    assert (over["monthly_cap_usd"], over["disable_billing"], over["health_check_interval"],
            over["health_no_probe"]) == ("12.5", "true", "60", "a,b,c")
    assert config.log_file() == config.config_dir() / "logs" / "k.log"
    assert db.settings().get("monthly_cap_usd") is None  # nothing went into the DB
    html = c.get("/settings")[1]  # and the page shows it back
    assert 'value="12.5"' in html and 'value="5100"' in html and "Settings saved" in html


def test_an_unchanged_field_is_not_pinned_into_the_file(env):
    c, db, *_ = env
    db.set_setting("monthly_cap_usd", "7")  # the old DB value is the current value
    resp, _ = c.post("/settings/save", {"billing.monthly_cap_usd": "7", "kiko.base_port": "4000",
                                        "proxy.port": "4000", "logs.retention_days": "0", "logs.store_text": "on"})
    assert resp.status == 303 and not config.toml_path().exists()  # nothing differs from what already applies
    assert "Nothing changed" in c.get("/settings")[1]
    c.post("/settings/save", {"billing.monthly_cap_usd": "8", "logs.store_text": "on"})
    assert file_data() == {"billing": {"monthly_cap_usd": 8.0}}  # a real change moves it to the file
    assert db.settings()["monthly_cap_usd"] == "7"  # the DB row stays (fallback if the key is removed)
    c.post("/settings/save", {"billing.monthly_cap_usd": "", "logs.store_text": "on"})  # empty: no value in the file, the DB cap applies again
    assert file_data().get("billing", {}) == {} and config.value("billing.monthly_cap_usd", db_settings=db.settings()) == 7.0
    c.post("/settings/save", {"billing.monthly_cap_usd": "0"})  # an explicit 0 turns the cap off
    assert config.value("billing.monthly_cap_usd", db_settings=db.settings()) == 0.0
    from kiko_lite.resolver import Catalog, cap_gate
    db.add_billing("m", 1, 1, 50.0)
    assert cap_gate(db, Catalog(db, ttl=0))() is None  # 0 does not block everything


@pytest.mark.parametrize("field,value", [
    ("proxy.port", "0"), ("proxy.port", "65536"), ("web.port", "abc"), ("spy.port", "-1"), ("kiko.base_port", "65535"),
    ("proxy.host", "bad host!"), ("billing.monthly_cap_usd", "lots"), ("billing.monthly_cap_usd", "-2"),
    ("health.check_interval", "x"), ("logs.retention_days", "1.5"), ("logs.retention_days", "-1"),
    ("logs.file", "a\x01b")])
def test_validation_refuses_and_keeps_the_file(env, field, value):
    c, *_ = env
    config.toml_path().write_text(TOML, encoding="utf-8")
    resp, html = c.post("/settings/save", {field: value, "web.port": "5100" if field != "web.port" else value})
    assert resp.status == 400 and "flash error" in html
    assert config.toml_path().read_text(encoding="utf-8") == TOML  # nothing written
    assert (f'value="{value}"' in html) or "\x01" in value  # what was typed stays in the box


def test_no_auth_on_a_non_loopback_host_is_refused(env):
    c, *_ = env
    resp, html = c.post("/settings/save", {"proxy.no_auth": "on", "proxy.host": "0.0.0.0"})
    assert resp.status == 400 and "refusing no-auth on 0.0.0.0" in html and not config.toml_path().exists()
    resp, _ = c.post("/settings/save", {"proxy.no_auth": "on", "proxy.host": "127.0.0.1"})
    assert resp.status == 303 and file_data()["proxy"]["no_auth"] is True
    resp, html = c.post("/settings/save", {"proxy.no_auth": "on", "proxy.host": "192.168.0.2"})  # existing no_auth + host
    assert resp.status == 400 and file_data()["proxy"].get("host") != "192.168.0.2"
    resp, _ = c.post("/settings/save", {"proxy.host": "192.168.0.2"})  # no_auth off again: allowed
    assert resp.status == 303 and file_data()["proxy"] == {"no_auth": False, "host": "192.168.0.2"}


def test_restart_notice_when_a_server_value_differs_from_what_started(env):
    c, db, ui, srv = env
    assert "Restart needed" not in c.get("/settings")[1]
    c.post("/settings/save", {"web.port": "5100", "logs.retention_days": "3"})
    html = c.get("/settings")[1]
    assert "Restart needed" in html and f"web.port: running with {ui.started['web.port']}, file says 5100" in html
    c.post("/settings/save", {"web.port": ""})  # back to what is running
    assert "Restart needed" not in c.get("/settings")[1]
    c.post("/settings/save", {"billing.disable_billing": "on"})  # a non-server value never needs a restart
    assert "Restart needed" not in c.get("/settings")[1]


def test_db_value_is_shown_in_the_box_and_a_change_moves_it_to_the_file(env):
    c, db, *_ = env
    db.set_setting("health_no_probe", "old1,old2")
    html = c.get("/settings")[1]
    assert 'value="old1,old2"' in html and "(from DB" in html
    c.post("/settings/save", {"health.no_probe": "new1"})
    assert file_data()["health"]["no_probe"] == "new1" and db.settings()["health_no_probe"] == "old1,old2"


def test_a_file_the_writer_cannot_edit_is_refused_and_untouched(env):
    c, *_ = env
    bad = '[proxy]\nport = """\n4000\n"""\n'  # the old value spans several lines
    config.toml_path().write_text(bad, encoding="utf-8")
    resp, html = c.post("/settings/save", {"proxy.port": "4100"})
    assert resp.status == 400 and "Not saved" in html and config.toml_path().read_text(encoding="utf-8") == bad


def test_settings_post_is_guarded(env):
    c, *_ = env
    anon = Client(env[3].port)
    resp, _ = anon.post("/settings/save", {"logs.retention_days": "1"}, csrf=False)
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    resp, html = c.post("/settings/save", {"logs.retention_days": "1"}, csrf="forged")
    assert resp.status == 403 and "CSRF" in html and not config.toml_path().exists()
    assert PASSWORD not in c.get("/settings")[1] and security.HASH_KEY not in c.get("/settings")[1]


def test_password_page_is_still_there(env):
    resp, html = env[0].get("/password")
    assert resp.status == 200 and "Change password" in html


def test_e2e_save_in_the_browser_flow_then_a_second_process_reads_it(real_server):
    port, password, proc, cfg = real_server
    b = Browser(port)
    _, html = b.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    b.request("POST", "/login", form={"csrf": token, "password": password})
    new = "brand-new-pass1"
    assert "Password changed" in b.post("/password", {"current": password, "new": new, "again": new})[1]
    resp, html = b.get("/settings")
    assert resp.status == 200 and f"ANTHROPIC_BASE_URL=http://127.0.0.1:{config.proxy_port({})}" in html
    resp, html = b.post("/settings/save", {"kiko.base_port": "5000", "web.host": "127.0.0.1",
                                           "billing.monthly_cap_usd": "4", "logs.retention_days": "2"})
    assert resp.status == 200 and b.hops[-1] == "/settings" and "Settings saved" in html
    assert "Restart needed" in html and "base_port" in html and "ANTHROPIC_BASE_URL=http://127.0.0.1:5000" in html
    assert "[kiko]" in (cfg / "kiko.toml").read_text(encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "kiko_lite", "config", "--json"], cwd=REPO, capture_output=True, text=True,
                       timeout=60, env={**os.environ, "KIKO_CONFIG_DIR": str(cfg), "PYTHONPATH": str(REPO)})
    info = json.loads(r.stdout)
    assert (info["proxy_port"], info["web_port"]) == (5000, 5001)  # the CLI reads what the page wrote
    assert info["settings"]["billing.monthly_cap_usd"] == 4.0 and info["settings"]["logs.retention_days"] == 2
    assert proc.poll() is None
