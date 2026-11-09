# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #124 fix 2: a Settings save keeps DB-sourced values, cap 0 notices, log file name rules."""
import logging
import tomllib
from html.parser import HTMLParser

import pytest

from kiko_lite import cli
from kiko_lite.resolver import Catalog, cap_gate
from kikolib import config
from kikolib import server as kserver

from .test_web_settings import env  # noqa: F401  (logged-in web client + DB)


class FormFields(HTMLParser):
    """The submit values of the settings form as a browser would send them."""

    def __init__(self):
        super().__init__()
        self.values, self.in_form = {}, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("action") == "/settings/save":
            self.in_form = True
        if self.in_form and tag == "input" and a.get("name"):
            if a.get("type") == "checkbox":
                if "checked" in a:
                    self.values[a["name"]] = "on"
            else:
                self.values[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def rendered_form(client):
    p = FormFields()
    p.feed(client.get("/settings")[1])
    return p.values


def file_data():
    return tomllib.loads(config.toml_path().read_text(encoding="utf-8")) if config.toml_path().exists() else {}


def test_form_as_rendered_keeps_a_db_cap(env):
    c, db, *_ = env
    db.set_setting("monthly_cap_usd", "7")
    db.set_setting("disable_billing", "false")
    db.set_setting("health_no_probe", "x,y")
    form = rendered_form(c)
    assert form["billing.monthly_cap_usd"] == "7"  # the DB value is IN the box
    assert form["health.no_probe"] == "x,y"
    assert "(from DB" in c.get("/settings")[1]
    assert c.post("/settings/save", form, csrf=False)[0].status == 303  # submitted untouched
    assert config.value("billing.monthly_cap_usd", db_settings=db.settings()) == 7.0
    db.add_billing("m", 1, 1, 8.0)
    assert "cap of 7.00 USD" in cap_gate(db, Catalog(db, ttl=0))()  # still blocks after a spend over 7


def test_unrelated_change_with_a_db_cap_does_not_remove_it(env):
    c, db, *_ = env
    db.set_setting("monthly_cap_usd", "7")
    form = rendered_form(c)
    form["logs.retention_days"] = "30"
    assert c.post("/settings/save", form, csrf=False)[0].status == 303
    assert file_data()["logs"]["retention_days"] == 30
    assert config.value("billing.monthly_cap_usd", db_settings=db.settings()) == 7.0
    # even a bare post that leaves the cap box empty does not unlimit the DB cap
    assert c.post("/settings/save", {"logs.retention_days": "31"})[0].status == 303
    assert config.value("billing.monthly_cap_usd", db_settings=db.settings()) == 7.0
    # clearing the cap needs an explicit 0
    assert c.post("/settings/save", {"billing.monthly_cap_usd": "0"})[0].status == 303
    assert config.value("billing.monthly_cap_usd", db_settings=db.settings()) == 0.0


def test_cap_zero_display(env):
    c, db, *_ = env
    assert "(cap: none)" in c.get("/")[1]
    db.set_setting("monthly_cap_usd", "0")
    assert "(cap: 0 (no cap))" in c.get("/")[1]
    assert config.cap_label("0") == "0 (no cap)" and config.cap_label("") == "none" and config.cap_label("3.5") == "3.5"


def test_doctor_and_serve_warn_about_a_db_cap_of_zero(tmp_path, capsys, caplog, monkeypatch):
    from kiko_lite.db import Database
    db = Database(tmp_path / "z.db")
    db.set_setting("monthly_cap_usd", "0")
    assert cli.main(["doctor", "--db", str(db.path)]) in (0, 1)
    out = capsys.readouterr().out
    assert "0 now means no cap" in out and "Monthly cap: 0 (no cap)" in out
    monkeypatch.setenv("MASTER_KEY", "m")
    monkeypatch.setattr(kserver, "serve", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING):
        assert cli.main(["serve", "--db", str(db.path)]) == 0
    assert "0 now means no cap" in caplog.text
    db.set_setting("monthly_cap_usd", "5")
    assert not config.db_cap_zero(db.settings())


@pytest.mark.parametrize("bad", ["x.txt", "x", "x.log.txt", "a:b.log", "kiko.toml", "logs/kiko.toml.log.x", "KIKO.DB.log",
                                 "kiko.db-wal.log", ".env", "x/web-initial-password", "web-ui-initial-password",
                                 "logs/KIKO.TOML", "x.log:stream", "a/.env"])
def test_log_file_name_rules(bad, tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        config.parse_value(config.BY_NAME["logs.file"], bad)
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(d))
    (d / "kiko.toml").write_text("[logs]\nfile = '" + bad + "'\n", encoding="utf-8")
    assert config.log_file() is None


@pytest.mark.parametrize("good", ["k.log", "logs/kiko.log", "a/b/c.LOG", "kiko-web.log"])
def test_log_file_good_names(good):
    assert config.parse_value(config.BY_NAME["logs.file"], good) == good


def test_form_refuses_bad_log_names(env):
    c, *_ = env
    for bad in ("x.txt", "kiko.toml", "a:b.log"):
        assert c.post("/settings/save", {"logs.file": bad})[0].status == 400
    assert not config.toml_path().exists()
