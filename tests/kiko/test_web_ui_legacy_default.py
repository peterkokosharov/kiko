# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The old web-ui shares webui_admin_hash with the new web: it must never serve the default password (TASK #121)."""
from kiko_lite.db import Database
from kikolib.server import Server

from .test_webui import Client, webui_mod  # noqa: F401  (fixtures)


def _db_with(tmp_path, webui_mod, password):
    db = Database(tmp_path / "kiko.db")
    # same format web/security.py stores; few iterations keep the test fast (the count is part of the hash)
    db.set_setting(webui_mod.HASH_KEY, webui_mod.hash_password(password, iterations=1000))
    return db


def _main(webui_mod, tmp_path, monkeypatch, host):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path / "cfg"))
    served = []
    monkeypatch.setattr(webui_mod.kserver, "serve", lambda app, h, p: served.append((h, p)))
    rc = webui_mod.main(["--host", host, "--port", "0", "--db", str(tmp_path / "kiko.db")])
    return rc, served


def test_default_constant_matches_new_web(webui_mod):
    from web.security import DEFAULT_PASSWORD, HASH_KEY, verify_password
    assert webui_mod.DEFAULT_PASSWORD == DEFAULT_PASSWORD
    assert webui_mod.HASH_KEY == HASH_KEY
    stored = webui_mod.hash_password(DEFAULT_PASSWORD, iterations=1000)  # and the formats cross-verify
    assert verify_password(DEFAULT_PASSWORD, stored) and webui_mod.verify_password(DEFAULT_PASSWORD, stored)


def test_non_loopback_start_refused_while_default(webui_mod, tmp_path, monkeypatch, capsys):
    _db_with(tmp_path, webui_mod, "admin")
    rc, served = _main(webui_mod, tmp_path, monkeypatch, "0.0.0.0")
    assert rc != 0 and not served
    assert "default" in capsys.readouterr().err


def test_loopback_start_ok_with_default(webui_mod, tmp_path, monkeypatch):
    _db_with(tmp_path, webui_mod, "admin")
    rc, served = _main(webui_mod, tmp_path, monkeypatch, "127.0.0.1")
    assert rc == 0 and served


def test_non_loopback_start_ok_with_real_password(webui_mod, tmp_path, monkeypatch):
    _db_with(tmp_path, webui_mod, "a-real-password-1")
    rc, served = _main(webui_mod, tmp_path, monkeypatch, "0.0.0.0")
    assert rc == 0 and served


def test_default_password_login_refused(webui_mod, tmp_path):
    ui = webui_mod.WebUI(_db_with(tmp_path, webui_mod, "admin"))
    with Server(ui.build_app(), port=0) as srv:
        c = Client(srv)
        st, cookie = c.login("admin")
        assert st == 403 and cookie is None
        st, body = c.json("POST", "/api/auth/login", {"username": "admin", "password": "admin"})
        assert st == 403 and "change" in body["error"]


def test_real_password_still_logs_in(webui_mod, tmp_path):
    ui = webui_mod.WebUI(_db_with(tmp_path, webui_mod, "a-real-password-1"))
    with Server(ui.build_app(), port=0) as srv:
        c = Client(srv)
        assert c.login("admin")[0] == 401
        assert c.login("a-real-password-1")[0] == 200
        assert c.json("GET", "/api/models")[0] == 200
