# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""web-ui/main.py: auth, API, static files, guard interplay, loopback bind."""
import http.client
import importlib.util
import json
from pathlib import Path

import pytest

from kiko_lite.db import Database
from kikolib.server import Server

MAIN = Path(__file__).resolve().parents[2] / "web-ui" / "main.py"


@pytest.fixture(scope="module")
def webui_mod():
    spec = importlib.util.spec_from_file_location("kiko_webui_main", MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Client:
    def __init__(self, srv):
        self.srv, self.cookie = srv, None

    def call(self, method, path, data=None, headers=None, origin=True, raw=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if origin:
            h["Origin"] = f"http://{self.srv.host}:{self.srv.port}"
        if self.cookie:
            h["Cookie"] = self.cookie
        body = raw if raw is not None else (None if data is None else json.dumps(data).encode())
        c = http.client.HTTPConnection(self.srv.host, self.srv.port, timeout=5)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            payload = r.read()
            return r.status, payload, r
        finally:
            c.close()

    def json(self, *a, **kw):
        st, body, r = self.call(*a, **kw)
        return st, json.loads(body)

    def login(self, pw):
        st, body, r = self.call("POST", "/api/auth/login", {"username": "admin", "password": pw})
        sc = r.getheader("Set-Cookie")
        if st == 200:
            self.cookie = sc.split(";")[0]
        return st, sc


@pytest.fixture
def env(webui_mod, tmp_path):
    db = Database(tmp_path / "kiko.db")
    ui = webui_mod.WebUI(db)
    with Server(ui.build_app(), port=0) as srv:
        yield ui, Client(srv), srv


def test_binds_loopback_by_default(env, webui_mod):
    ui, c, srv = env
    assert srv.host == "127.0.0.1"
    from kikolib import config
    assert config.webui_host({}) == "127.0.0.1"
    assert webui_mod.main.__defaults__ == (None,)


def test_no_default_password_and_hash_stored(env, webui_mod):
    ui, c, _ = env
    assert ui.initial_password and ui.initial_password != "admin"
    assert c.login("admin")[0] == 401
    stored = ui.db.settings()[webui_mod.HASH_KEY]
    assert stored.startswith("pbkdf2_sha256$") and ui.initial_password not in stored
    # second start with the same DB keeps the password and generates no new one
    assert webui_mod.WebUI(ui.db).initial_password is None


def test_login_cookie_flags_and_session(env):
    ui, c, _ = env
    st, body = c.json("GET", "/api/models")
    assert st == 401
    st, cookie = c.login(ui.initial_password)
    assert st == 200
    for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert flag in cookie
    assert len(cookie.split(";")[0].split("=", 1)[1]) >= 40
    assert c.json("GET", "/api/models")[0] == 200
    assert c.json("GET", "/api/auth/status")[1]["authenticated"] is True
    c.call("POST", "/api/auth/logout", {})
    c.cookie = None
    assert c.json("GET", "/api/models")[0] == 401


def test_login_throttle(env):
    ui, c, _ = env
    for _ in range(5):
        assert c.login("wrong")[0] == 401
    assert c.login(ui.initial_password)[0] == 429


def test_session_expiry(webui_mod, tmp_path):
    ui = webui_mod.WebUI(Database(tmp_path / "k.db"), session_ttl=-1)
    with Server(ui.build_app(), port=0) as srv:
        c = Client(srv)
        c.login(ui.initial_password)
        assert c.json("GET", "/api/models")[0] == 401


def test_change_password(env):
    ui, c, _ = env
    c.login(ui.initial_password)
    bad = {"current_password": "bad", "new_password": "longenough1"}
    assert c.json("POST", "/api/auth/change-password", bad)[0] == 400
    short = {"current_password": ui.initial_password, "new_password": "x"}
    assert c.json("POST", "/api/auth/change-password", short)[0] == 400
    good = {"current_password": ui.initial_password, "new_password": "longenough1"}
    assert c.json("POST", "/api/auth/change-password", good)[0] == 200
    assert c.json("GET", "/api/models")[0] == 200  # own session survives
    other = Client(c.srv)
    assert other.login(ui.initial_password)[0] == 401
    assert other.login("longenough1")[0] == 200


def test_foreign_origin_and_host_blocked_same_origin_ok(env):
    ui, c, srv = env
    c.login(ui.initial_password)
    assert c.json("GET", "/api/models")[0] == 200  # same-origin Origin header passes the guard
    assert c.call("GET", "/api/models", headers={"Origin": "http://evil.example"}, origin=False)[0] == 403
    assert c.call("POST", "/api/settings", {"key": "a", "value": "b"},
                  headers={"Origin": "http://evil.example"}, origin=False)[0] == 403
    assert c.call("GET", "/api/models", headers={"Host": "evil.example"})[0] == 403
    assert c.call("POST", "/api/settings", raw=b"k=v", headers={"Content-Type": "text/plain"})[0] == 415


def test_models_classes_aliases_settings_flow(env, webui_mod):
    ui, c, _ = env
    c.login(ui.initial_password)
    st, r = c.json("POST", "/api/models", {"alias": "m1", "provider": "openai", "api_key_env": "M1_KEY",
                                           "model_class": "sonnet", "priority": "5", "price_in_per_mtok": ""})
    assert st == 200 and r == {"alias": "m1", "created": True}
    assert c.json("PUT", "/api/models/m1", {"provider": "openai", "api_key_env": "M1_KEY"})[1]["created"] is False
    assert c.json("GET", "/api/models/m1")[1]["priority"] == 100
    bad_class = {"alias": "m2", "provider": "x", "api_key_env": "K", "model_class": "NOPE"}
    assert c.json("POST", "/api/models", bad_class)[0] == 400
    bad_prio = {"alias": "m2", "provider": "x", "api_key_env": "K", "priority": "abc"}
    assert c.json("POST", "/api/models", bad_prio)[0] == 400
    st, r = c.json("GET", "/api/key-status/m1")
    assert st == 200 and r["key_name"] == "M1_KEY" and "value" not in r
    assert c.json("DELETE", "/api/models/m1")[0] == 200
    assert c.json("DELETE", "/api/models/m1")[0] == 404

    assert c.json("POST", "/api/classes", {"name": "tiny", "keyword": "tinyword", "role": "r"})[0] == 200
    assert any(x["name"] == "TINY" for x in c.json("GET", "/api/classes")[1]["classes"])
    assert c.json("DELETE", "/api/classes/TINY")[0] == 200
    assert c.json("DELETE", "/api/classes/TINY")[0] == 404

    assert c.json("POST", "/api/aliases", {"glob": "claude-*", "target": "m1"})[0] == 200
    assert c.json("GET", "/api/aliases")[1]["aliases"] == [{"glob": "claude-*", "target": "m1"}]
    assert c.json("DELETE", "/api/aliases/claude-*")[0] == 200

    assert c.json("POST", "/api/settings", {"key": "foo", "value": 1})[1] == {"key": "foo", "value": "1"}
    st, r = c.json("GET", "/api/settings")
    assert r["settings"]["foo"] == "1" and webui_mod.HASH_KEY not in r["settings"]
    assert c.json("POST", "/api/settings", {"key": webui_mod.HASH_KEY, "value": "x"})[0] == 403


def test_logs_billing_and_bad_numbers(env):
    ui, c, _ = env
    c.login(ui.initial_password)
    ui.db.add_billing("m1", 10, 20, 0.5)
    assert c.json("GET", "/api/logs/count")[1] == {"count": 1}
    assert c.json("GET", "/api/logs?limit=abc&offset=-5")[1]["logs"][0]["model"] == "m1"
    assert c.json("GET", "/api/responses/count")[1] == {"count": 0}
    assert c.json("GET", "/api/responses")[1] == {"responses": []}
    assert c.json("GET", "/api/billing?days=zzz")[1]["days"] == 30


def test_pages_static_and_traversal(env):
    ui, c, _ = env
    st, body, r = c.call("GET", "/")
    assert st == 302 and r.getheader("Location") == "/login"
    assert c.call("GET", "/login")[0] == 200
    assert b"admin / admin" not in c.call("GET", "/login")[1]
    c.login(ui.initial_password)
    assert c.call("GET", "/")[0] == 200
    assert c.call("GET", "/models/new")[0] == 200
    assert c.call("GET", "/some/spa/route")[0] == 200
    st, body, r = c.call("GET", "/static/style.css")
    assert st == 200 and "text/css" in r.getheader("Content-Type")
    assert c.call("GET", "/static/nope.js")[0] == 404
    for evil in ("/static/..%2fmain.py", "/static/..%2f..%2fkiko_lite%2fdb.py", "/static/%2e%2e/main.py",
                 "/static/..%5cmain.py", "/static/a%00b"):
        st, body, _ = c.call("GET", evil)
        assert st in (404, 400) and b"import" not in body, evil


def test_password_hash_roundtrip(webui_mod):
    h = webui_mod.hash_password("secret-pass")
    assert webui_mod.verify_password("secret-pass", h)
    assert not webui_mod.verify_password("other", h)
    assert not webui_mod.verify_password("x", "garbage")
