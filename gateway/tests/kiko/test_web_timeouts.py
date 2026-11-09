# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #131: the web pages of the timeout settings (Settings page, Models page). Kept apart from
test_kiko_lite_timeouts.py because web/ is not part of the kiko-lite zip."""
from kikolib import config

from .test_web_ui import client, env  # noqa: F401  (the web fixtures)


def test_settings_page_shows_and_saves_the_two_settings(client, env):
    _, html = client.get("/settings")
    assert 'name="proxy.upstream_timeout"' in html and 'name="proxy.probe_timeout"' in html
    assert "Default: 300.0" in html and "Default: 20.0" in html
    resp, html = client.post("/settings/save", {"proxy.upstream_timeout": "600", "proxy.probe_timeout": "40",
                                                "logs.store_text": "on"})
    assert resp.status == 303
    assert config.value("proxy.upstream_timeout") == 600.0 and config.value("proxy.probe_timeout") == 40.0
    for bad in ("0", "-3", "x", "86401"):
        resp, html = client.post("/settings/save", {"proxy.upstream_timeout": bad, "logs.store_text": "on"})
        assert resp.status == 400 and "upstream_timeout" in html
    assert config.value("proxy.upstream_timeout") == 600.0  # a refused save changes nothing


def test_models_page_column_and_edit_field(client, env):
    db = env[0]
    form = {"alias": "gem", "provider": "gemini", "model_id": "x", "model_class": "SONNET", "priority": "7",
            "enabled": "on", "timeout": "600"}
    assert client.post("/models/save", form)[0].status == 303
    assert db.get_model("gem")["timeout"] == 600.0
    _, html = client.get("/models")
    assert "<th>Timeout</th>" in html and "600 s" in html
    _, html = client.get("/models/edit?alias=gem")
    assert 'name="timeout"' in html and 'value="600"' in html
    assert client.post("/models/save", {**form, "timeout": "1.5"})[0].status == 303
    assert db.get_model("gem")["timeout"] == 1.5
    assert client.post("/models/save", {k: v for k, v in form.items() if k != "timeout"})[0].status == 303
    assert db.get_model("gem")["timeout"] is None  # the form always carries the field: empty clears
    client.post("/models/save", form)
    assert client.post("/models/save", {**form, "timeout": "0"})[0].status == 303
    assert db.get_model("gem")["timeout"] is None
    client.post("/models/save", form)
    for bad in ("abc", "-1", "nan", "999999", "<b>x</b>"):
        resp, html = client.post("/models/save", {**form, "timeout": bad})
        assert resp.status == 400 and "timeout must be" in html and "<b>x</b>" not in html
        assert db.get_model("gem")["timeout"] == 600.0  # unchanged
    assert client.post("/models/save", {**form, "timeout": "7"}, csrf=False)[0].status in (400, 403)  # CSRF
    assert db.get_model("gem")["timeout"] == 600.0


def test_models_page_tolerates_a_bad_stored_timeout(client, env):
    import sqlite3
    db = env[0]
    form = {"alias": "gem", "provider": "gemini", "model_id": "x", "model_class": "SONNET", "priority": "7",
            "enabled": "on", "timeout": "600"}
    assert client.post("/models/save", form)[0].status == 303
    con = sqlite3.connect(db.path)
    con.execute("UPDATE models SET timeout='junk'")
    con.commit()
    con.close()
    resp, html = client.get("/models")
    assert resp.status == 200 and "gem" in html
