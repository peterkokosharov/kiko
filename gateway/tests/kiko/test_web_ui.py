# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The server-rendered web UI (web/): login, CSRF, escaping, headers, key masking, stdlib-only. Temp DBs only."""
import ast
import http.client
import re
import sqlite3
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode

import pytest

import web
import web.__main__ as web_main
from kiko_lite.db import Database
from kikolib import config, tomlwrite
from kikolib import server as kserver
from web import security
from web.billing import log_stream
from web.ui import WebUI

PASSWORD = "correct horse 42"
HOSTILE = '"><script>x</script>'
QUOTE = 'x" onfocus="alert(1)'
WEB_DIR = Path(web.__file__).resolve().parent
SECRET = "sk-SUPER-SECRET-VALUE-123456"


class Client:
    """Minimal cookie-keeping HTTP client (no redirects followed)."""

    def __init__(self, port):
        self.port, self.cookies = port, {}

    def request(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = None
        if form is not None:
            body = urlencode(form)
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        for value in resp.headers.get_all("Set-Cookie") or []:
            jar = SimpleCookie(value)
            for k, m in jar.items():
                if m.value:
                    self.cookies[k] = m.value
                else:
                    self.cookies.pop(k, None)
        conn.close()
        return resp, data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def csrf(self, path="/settings"):
        _, html = self.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def post(self, path, form, csrf=True, **kw):
        form = dict(form)
        if csrf is True:
            form["csrf"] = self.csrf()
        elif csrf:
            form["csrf"] = csrf
        return self.request("POST", path, form=form, **kw)

    def login(self, password=PASSWORD):
        _, html = self.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        return self.request("POST", "/login", form={"csrf": token, "password": password})


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "kiko.db")
    db.set_setting(security.HASH_KEY, security.hash_password(PASSWORD, iterations=1000))
    ui = WebUI(db, password_file=tmp_path / "pw")
    srv = kserver.Server(ui.build_app(), "127.0.0.1", 0)
    srv.start()
    yield db, srv.port, tmp_path
    srv.request_stop()  # no drain: an open live-log stream would hold it for its keep-alive period
    srv.close()


@pytest.fixture
def client(env):
    c = Client(env[1])
    resp, _ = c.login()
    assert resp.status == 303 and c.cookies.get(security.COOKIE)
    return c


def seed_hostile(db):
    db.upsert_model(HOSTILE, QUOTE, "SOME_KEY_NAME", api_base=QUOTE)
    db.upsert_model("plain", "p/x", "OTHER_KEY")
    db.add_class("EVIL", "evilkw", HOSTILE)
    db.set_alias(QUOTE, HOSTILE)
    db.set_setting(QUOTE, HOSTILE)
    tomlwrite.set_value(config.toml_path(), "logs", "file", QUOTE)  # shown on the settings page
    db.add_billing(HOSTILE, 1, 2, 0.5, error=HOSTILE, upstream_model=QUOTE)
    with sqlite3.connect(db.path) as con:  # a stored response; no public writer exists
        con.execute("INSERT INTO responses(id, input_json, output_json) VALUES(?,?,?)", (QUOTE, HOSTILE, HOSTILE))


PAGES = ["/", "/models", "/providers", "/providers/new", "/classes", "/aliases", "/billing", "/logs", "/responses", "/settings", "/password",
         "/models/new"]


# --- defaults ---
def test_defaults_host_and_port(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(web_main.kserver, "serve", lambda app, host, port, **kw: seen.update(host=host, port=port))
    assert web_main.main(["--db", str(tmp_path / "k.db")]) == 0
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 4001)  # base_port 4000 + 1 (TASK #124; was the preview 4080)
    assert not hasattr(web, "DEFAULT_PORT")  # nothing fixed in the package: kikolib.config decides
    (tmp_path / "kiko.toml").write_text("[kiko]\nbase_port = 5000\n", encoding="utf-8")
    web_main.main(["--db", str(tmp_path / "k.db")])
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 5001)
    web_main.main(["--db", str(tmp_path / "k.db"), "--host", "127.0.0.2", "--port", "4090"])
    assert (seen["host"], seen["port"]) == ("127.0.0.2", 4090)


def test_port_4000_not_used_in_web():
    for path in WEB_DIR.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".md":
            assert not re.search(r"(?<![\d.])4000(?!\d)", path.read_text(encoding="utf-8")), path


def test_first_start_uses_the_default_password_and_writes_no_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path / "cfg"))
    seen = {}
    monkeypatch.setattr(web_main.kserver, "serve", lambda app, host, port, **kw: seen.update(host=host))
    assert web_main.main(["--db", str(tmp_path / "k.db")]) == 0
    out = capsys.readouterr().out
    assert "first start: login with the default password, then change it" in out
    assert not (tmp_path / "cfg" / "web-initial-password").exists()
    db = Database(tmp_path / "k.db")
    assert security.verify_password("admin", db.settings()[security.HASH_KEY])
    assert web_main.main(["--db", str(tmp_path / "k.db")]) == 0  # second start: no first-start message
    assert "first start" not in capsys.readouterr().out


def test_existing_hash_is_never_reset(tmp_path):
    db = Database(tmp_path / "k.db")
    old = security.hash_password("random-from-before", iterations=1000)
    db.set_setting(security.HASH_KEY, old)
    auth = WebUI(db).auth
    assert not auth.ensure_password() and db.settings()[security.HASH_KEY] == old
    assert auth.verify("random-from-before") and not auth.is_default()


def test_non_loopback_start_refused_while_password_is_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path / "cfg"))
    started = []
    monkeypatch.setattr(web_main.kserver, "serve", lambda app, host, port, **kw: started.append(host))
    db_path = str(tmp_path / "k.db")
    assert web_main.main(["--db", db_path, "--host", "0.0.0.0"]) == 2
    assert not started and "still the default" in capsys.readouterr().err
    assert web_main.main(["--db", db_path, "--host", "localhost"]) == 0  # loopback names are fine
    db = Database(tmp_path / "k.db")
    db.set_setting(security.HASH_KEY, security.hash_password("a-new-password", iterations=1000))
    assert web_main.main(["--db", db_path, "--host", "0.0.0.0"]) == 0 and started[-1] == "0.0.0.0"


def test_default_login_forces_the_password_page(tmp_path):
    db = Database(tmp_path / "kiko.db")
    pw = tmp_path / "web-initial-password"
    pw.write_text("old")
    ui = WebUI(db, password_file=pw)
    ui.auth.ensure_password()
    srv = kserver.Server(ui.build_app(), "127.0.0.1", 0)
    srv.start()
    try:
        c = Client(srv.port)
        assert c.login("admin")[0].status == 303
        for path in ("/", "/models", "/settings", "/logs"):
            resp, _ = c.get(path)
            assert (resp.status, resp.headers["Location"]) == (303, "/password"), path
        resp, _ = c.post("/settings/save", {"logs.retention_days": "5"}, csrf=c.csrf("/password"))
        assert (resp.status, resp.headers["Location"]) == (303, "/password")
        assert not (tmp_path / "cfg" / "kiko.toml").exists() and not config.toml_path().exists()
        resp, html = c.get("/password")
        assert resp.status == 200 and "default password" in html
        resp, _ = c.post("/password", {"current": "admin", "new": "brand-new-pass1", "again": "brand-new-pass1"},
                         csrf=c.csrf("/password"))
        assert resp.status == 303 and not pw.exists()
        assert c.get("/models")[0].status == 200  # forced redirect is over
        c2 = Client(srv.port)  # logout works while forced
        assert c2.login("brand-new-pass1")[0].status == 303 and c2.get("/")[0].status == 200
    finally:
        srv.request_stop()
        srv.close()


def test_logout_works_while_password_change_is_forced(tmp_path):
    db = Database(tmp_path / "kiko.db")
    ui = WebUI(db)
    ui.auth.ensure_password()
    srv = kserver.Server(ui.build_app(), "127.0.0.1", 0)
    srv.start()
    try:
        c = Client(srv.port)
        c.login("admin")
        resp, _ = c.post("/logout", {}, csrf=c.csrf("/password"))
        assert (resp.status, resp.headers["Location"]) == (303, "/login")
    finally:
        srv.request_stop()
        srv.close()


def test_password_hashes():
    assert security.hash_password("x").split("$")[1] == "600000"
    old = security.hash_password("pw", iterations=1000)  # an older hash with its own count still verifies
    assert security.verify_password("pw", old) and not security.verify_password("no", old)
    assert not security.verify_password("pw", "garbage")


# --- auth / CSRF ---
def test_login_required_everywhere(env):
    c = Client(env[1])
    for path in PAGES + ["/logs/stream", "/models/edit?alias=x"]:
        resp, _ = c.get(path)
        assert (resp.status, resp.headers["Location"]) == (303, "/login"), path
    for path in ["/models/save", "/models/delete", "/providers/save", "/providers/delete", "/settings/save", "/classes/add", "/logout", "/password"]:
        resp, _ = c.post(path, {"alias": "a"}, csrf=False)
        assert (resp.status, resp.headers["Location"]) == (303, "/login"), path


def test_login_wrong_password_and_cookie_flags(env):
    c = Client(env[1])
    resp, html = c.login("wrong")
    assert resp.status == 401 and "Wrong password" in html and security.COOKIE not in c.cookies
    resp, _ = c.login()
    cookie = [v for v in resp.headers.get_all("Set-Cookie") if v.startswith(security.COOKIE)][0]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert c.get("/")[0].status == 200


def test_login_needs_its_own_csrf_token(env):
    c = Client(env[1])
    c.get("/login")
    resp, _ = c.request("POST", "/login", form={"csrf": "forged", "password": PASSWORD})
    assert resp.status == 403 and security.COOKIE not in c.cookies


def test_login_throttle(env):
    c = Client(env[1])
    for _ in range(security.MAX_FAILS):
        assert c.login("wrong")[0].status == 401
    assert c.login(PASSWORD)[0].status == 429


def test_csrf_rejected(client, env):
    db = env[0]
    for token in (False, "forged", ""):
        resp, html = client.post("/models/save", {"alias": "m1", "provider": "openai", "model_id": "gpt-4o"}, csrf=token)
        assert resp.status == 403, token
        assert "CSRF" in html
    assert db.get_model("m1") is None
    resp, _ = client.post("/models/save", {"alias": "m1", "provider": "openai", "model_id": "gpt-4o"})
    assert resp.status == 303 and db.get_model("m1")


def test_cross_origin_post_rejected(client, env):
    token = client.csrf()
    form = {"csrf": token, "logs.retention_days": "9"}
    for origin in ("http://evil.example", "null", "https://127.0.0.1"):
        resp, _ = client.request("POST", "/settings/save", form=form, headers={"Origin": origin})
        assert resp.status == 403, origin
    assert not config.toml_path().exists()
    own = f"http://127.0.0.1:{env[1]}"
    resp, _ = client.request("POST", "/settings/save", form=form, headers={"Origin": own})
    assert resp.status == 303 and config.value("logs.retention_days") == 9


def test_foreign_host_header_rejected(env):
    resp, html = Client(env[1]).get("/login", headers={"Host": "evil.example"})
    assert resp.status == 403 and "Content-Security-Policy" in resp.headers
    assert resp.headers["Content-Type"].startswith("text/html") and "Host header not allowed" in html


def test_refused_origin_is_an_html_page(client):
    resp, html = client.request("POST", "/settings/save", form={"a": "b"}, headers={"Origin": "null"})
    assert resp.status == 403 and resp.headers["Content-Type"].startswith("text/html")
    assert "cross-origin request refused" in html and "Content-Security-Policy" in resp.headers


def test_post_must_be_a_form(client):
    resp, _ = client.request("POST", "/settings/save", headers={"Content-Type": "application/json"})
    assert resp.status == 415


def test_logout_ends_session(client):
    assert client.post("/logout", {})[0].status == 303
    assert client.get("/models")[0].status == 303


def test_password_change(client, env):
    resp, html = client.post("/password", {"current": "bad", "new": "newpassword1", "again": "newpassword1"})
    assert resp.status == 400 and "wrong" in html
    resp, html = client.post("/password", {"current": PASSWORD, "new": "short", "again": "short"})
    assert resp.status == 400
    (env[2] / "pw").write_text("initial")
    resp, _ = client.post("/password", {"current": PASSWORD, "new": "newpassword1", "again": "newpassword1"})
    assert resp.status == 303 and not (env[2] / "pw").exists()
    fresh = Client(env[1])
    assert fresh.login(PASSWORD)[0].status == 401 and fresh.login("newpassword1")[0].status == 303


# --- headers ---
def test_security_headers_on_every_kind_of_response(env, client):
    anon = Client(env[1])
    responses = [anon.get("/login")[0], anon.get("/")[0], anon.get("/static/style.css")[0],
                 anon.get("/static/../ui.py")[0], anon.get("/nope")[0], anon.request("DELETE", "/x")[0],
                 anon.request("POST", "/", form={})[0], anon.get("/login", headers={"Host": "evil"})[0]]
    responses += [client.get(p)[0] for p in PAGES] + [client.get("/models/edit?alias=zzz")[0]]
    for resp in responses:
        h = resp.headers
        assert h["Content-Security-Policy"] == security.CSP
        for part in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self'",
                     "frame-ancestors 'none'", "form-action 'self'"):
            assert part in h["Content-Security-Policy"]
        assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "same-origin"


def test_static_only_css_and_js(env):
    c = Client(env[1])
    assert c.get("/static/style.css")[0].status == 200
    assert c.get("/static/live.js")[0].status == 200
    assert c.get("/static/..%2F__init__.py")[0].status == 404


def test_pages_have_no_inline_script_or_style(client):
    for path in PAGES:
        _, html = client.get(path)
        assert "<style" not in html and not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert not re.search(r"\son\w+=", html), path


# --- escaping ---
def test_hostile_values_are_escaped_on_every_page(client, env):
    seed_hostile(env[0])
    paths = PAGES + ["/models/edit?alias=" + "%22%3E%3Cscript%3Ex%3C%2Fscript%3E", "/logs?limit=5", "/responses?offset=0"]
    for path in paths:
        resp, html = client.get(path)
        assert resp.status == 200, path
        assert "<script>x</script>" not in html, path
        assert QUOTE not in html, path
        assert 'onfocus="alert' not in html, path
    shown = "".join(client.get(p)[1] for p in ("/models", "/classes", "/aliases", "/settings", "/logs", "/responses"))
    assert "&quot;&gt;&lt;script&gt;x&lt;/script&gt;" in shown
    assert "x&quot; onfocus=&quot;alert(1)" in shown
    _, edit = client.get("/models/edit?alias=%22%3E%3Cscript%3Ex%3C%2Fscript%3E")
    assert 'value="&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in edit  # in an attribute too


def test_flash_messages_are_escaped(client):
    client.post("/aliases/delete", {"glob": HOSTILE})  # "Alias ... deleted." style text carrying user input
    client.post("/settings/save", {"health.no_probe": HOSTILE})  # accepted text, shown back in the box: must be escaped
    _, html = client.get("/settings")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    resp, html = client.post("/settings/save", {"proxy.port": HOSTILE, "logs.file": HOSTILE})  # error flash + redisplay
    assert resp.status == 400 and "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_markup_helpers_escape_by_default():
    from web import render as r
    assert str(r.tag("p", HOSTILE, title=QUOTE)) == ('<p title="x&quot; onfocus=&quot;alert(1)">'
                                                     '&quot;&gt;&lt;script&gt;x&lt;/script&gt;</p>')
    assert str(r.esc(None)) == "" and str(r.esc("'")) == "&#x27;"


# --- secrets ---
def test_key_values_never_in_pages(client, env, monkeypatch):
    monkeypatch.setenv("KIKO_TEST_WEB_KEY", SECRET)
    (env[2] / ".env").write_text(f"DOTENV_KEY={SECRET}\n", encoding="utf-8")
    db = env[0]
    db.upsert_model("m-env", "p/a", "KIKO_TEST_WEB_KEY")
    db.upsert_model("m-dot", "p/b", "DOTENV_KEY")
    db.upsert_model("m-pasted", "p/c", SECRET + " has spaces")  # a key pasted where the NAME belongs
    _, models = client.get("/models")
    assert "KIKO_TEST_WEB_KEY" in models and ">env<" in models and ">dotenv<" in models
    for path in PAGES + ["/models/edit?alias=m-pasted", "/models/edit?alias=m-env"]:
        assert SECRET not in client.get(path)[1], path
    assert SECRET + " has spaces" not in client.get("/models")[1]
    assert PASSWORD not in client.get("/settings")[1] and security.HASH_KEY not in client.get("/settings")[1]


def test_hash_setting_cannot_be_changed_or_deleted(client, env):
    before = env[0].settings()[security.HASH_KEY]
    for path in ("/settings/set", "/settings/delete"):  # the raw key=value editor is gone
        resp, _ = client.post(path, {"key": security.HASH_KEY, "value": "x"})
        assert resp.status == 404
    client.post("/settings/save", {security.HASH_KEY: "x"})  # not a field of the form: ignored
    assert env[0].settings()[security.HASH_KEY] == before


# --- features ---
def test_models_crud(client, env):
    db = env[0]
    form = {"alias": "gem", "provider": "gemini", "model_id": "x", "model_class": "SONNET",
            "priority": "7", "price_in_per_mtok": "1.5", "enabled": "on", "status": "ok"}
    assert client.post("/models/save", form)[0].status == 303
    row = db.get_model("gem")
    assert (row["priority"], row["price_in_per_mtok"], row["enabled"], row["status"]) == (7, 1.5, 1, "unknown")
    client.post("/models/toggle", {"alias": "gem"})
    assert db.get_model("gem")["enabled"] == 0
    resp, html = client.post("/models/save", {**form, "model_class": "NOPE"})
    assert resp.status == 400 and "model class must be one of" in html and 'value="gem"' in html
    resp, html = client.post("/models/save", {**form, "priority": "abc"})
    assert resp.status == 400
    client.post("/models/delete", {"alias": "gem"})
    assert db.get_model("gem") is None


def test_classes_and_aliases_and_settings(client, env):
    db = env[0]
    client.post("/classes/add", {"name": "vocal", "keyword": "vocalkw", "role": "v"})
    assert "VOCAL" in db.class_names()
    _, html = client.post("/classes/add", {"name": "vocal", "keyword": "other"})
    client.post("/classes/delete", {"name": "VOCAL"})
    assert "VOCAL" not in db.class_names()
    client.post("/aliases/set", {"glob": "claude-*", "target": "gem"})
    assert db.aliases() == [("claude-*", "gem")]
    assert "claude-*" in client.get("/aliases")[1]
    client.post("/aliases/delete", {"glob": "claude-*"})
    assert db.aliases() == []
    client.post("/settings/save", {"billing.monthly_cap_usd": "12"})
    assert "monthly_cap_usd" in client.get("/settings")[1] and "cap: 12" in client.get("/")[1]
    assert "monthly_cap_usd" not in db.settings()  # the file, not the DB
    client.post("/settings/save", {"billing.monthly_cap_usd": ""})
    assert "monthly_cap_usd" not in config.toml_path().read_text(encoding="utf-8")


def test_billing_logs_pagination(client, env):
    db = env[0]
    for i in range(35):
        db.add_billing("m%02d" % i, 10, 20, 0.01)
    _, html = client.get("/billing?days=7")
    assert "m00" in html and "Total: $0.3500" in html
    _, html = client.get("/logs")
    assert "1-30 of 35" in html and "offset=30" in html
    _, html = client.get("/logs?offset=30&limit=30")
    assert "31-35 of 35" in html
    _, html = client.get("/logs?limit=abc&offset=-5")
    assert resp_ok(html)
    assert 'src="/static/live.js"' in html and 'data-src="/logs/stream"' in html


def resp_ok(html):
    return "Request log" in html


def test_live_log_stream(env, client):
    db, port, _ = env
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/logs/stream", headers={"Cookie": f"{security.COOKIE}={client.cookies[security.COOKIE]}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.headers["Content-Type"] == "text/event-stream"
    assert "Content-Security-Policy" in resp.headers
    assert resp.readline().startswith(b": connected")
    db.add_billing(HOSTILE, 1, 2, 0.25)
    resp.readline()  # blank line of the first frame
    line = resp.readline().decode()
    assert line.startswith("data: ") and HOSTILE.replace('"', '\\"') in line  # JSON string, never HTML
    conn.close()


def test_log_stream_generator(tmp_path):
    db = Database(tmp_path / "k.db")
    db.add_billing("old", 1, 1, 0.1)
    gen = log_stream(db, poll=0.01)
    assert next(gen).startswith(": connected")
    db.add_billing("new", 1, 1, 0.1, error="boom")
    frame = next(gen)
    assert "new" in frame and "boom" in frame and "old" not in frame
    gen.close()


def test_live_js_never_uses_innerhtml():
    src = (WEB_DIR / "static" / "live.js").read_text(encoding="utf-8")
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "textContent" in src
    assert len(list((WEB_DIR / "static").glob("*.js"))) == 1


# --- stdlib only ---
def test_web_imports_only_stdlib_and_kiko():
    allowed = set(sys.stdlib_module_names) | {"web", "kikolib", "kiko_lite"}
    for path in WEB_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                assert name in allowed, f"{path.name} imports non-stdlib {name}"


# --- review round 1 (TASK #120) ---
def test_log_stream_ends_when_session_ends(env, client):
    db, port, _ = env
    token = client.cookies[security.COOKIE]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/logs/stream", headers={"Cookie": f"{security.COOKIE}={token}"})
    resp = conn.getresponse()
    assert resp.readline().startswith(b": connected")
    assert client.post("/logout", {})[0].status == 303
    assert resp.read().strip() == b""  # the server closed the stream within a poll interval
    conn.close()


def test_log_stream_generator_stops_on_dead_session_and_after_max_seconds(tmp_path):
    db = Database(tmp_path / "k.db")
    state = {"ok": True}
    gen = log_stream(db, poll=0.01, alive=lambda: state["ok"])
    assert next(gen).startswith(": connected")
    state["ok"] = False
    assert list(gen) == []
    assert list(log_stream(db, poll=0.01, max_seconds=0.05)) == [": connected\n\n"]


def test_live_js_opens_stream_only_with_details_open():
    src = (WEB_DIR / "static" / "live.js").read_text(encoding="utf-8")
    assert '"toggle"' in src and "source.close()" in src and "box.open" in src


def test_invalid_key_name_refused_and_nothing_stored(client, env):
    form = {"alias": "bad", "provider": "openai", "model_id": "gpt-4o", "api_key_env": SECRET,
            "model_class": "SONNET"}
    resp, html = client.post("/models/save", form)
    assert resp.status == 400 and "environment variable name" in html and SECRET not in html
    assert env[0].get_model("bad") is None
    resp, _ = client.post("/models/save", {**form, "api_key_env": "has space"})
    assert resp.status == 400 and env[0].get_model("bad") is None


def test_bad_prices_and_empty_provider_refused(client, env):
    form = {"alias": "pm", "provider": "openai", "model_id": "gpt-4o", "api_key_env": "GOOD_KEY",
            "model_class": "SONNET"}
    for field in ("price_in_per_mtok", "price_out_per_mtok", "price_cache_read_per_mtok"):
        for bad in ("nan", "inf", "-inf", "-1"):
            assert client.post("/models/save", {**form, field: bad})[0].status == 400, (field, bad)
            assert env[0].get_model("pm") is None
    assert client.post("/models/save", {**form, "provider": "  "})[0].status == 400
    assert env[0].get_model("pm") is None
    assert client.post("/models/save", {**form, "price_in_per_mtok": "0"})[0].status == 303


def test_throttle_counts_attempt_before_verification(env):
    auth = WebUI(env[0]).auth
    assert [auth.begin_attempt() for _ in range(security.MAX_FAILS + 1)] == [True] * security.MAX_FAILS + [False]


def test_tokens_rotate_after_password_change(client, env):
    old_cookie, old_csrf = client.cookies[security.COOKIE], client.csrf()
    resp, _ = client.post("/password", {"current": PASSWORD, "new": "newpassword1", "again": "newpassword1"})
    assert resp.status == 303
    assert client.cookies[security.COOKIE] != old_cookie and client.csrf() != old_csrf
    assert client.get("/models")[0].status == 200  # the new cookie works
    stale = Client(env[1])
    stale.cookies[security.COOKIE] = old_cookie
    assert stale.get("/models")[0].status == 303  # the old one does not


# --- real end-to-end: the actual `python -m web` process, a real socket, redirects followed by Location ---
REPO = WEB_DIR.parent


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def real_server(tmp_path):
    import os
    import subprocess
    import time
    port = _free_port()
    cfg = tmp_path / "cfg"
    envv = {**os.environ, "KIKO_CONFIG_DIR": str(cfg), "KIKO_DB_PATH": str(tmp_path / "e2e.db"),
            "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "web", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=REPO, env=envv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if _listening(port):
                break
            assert proc.poll() is None, "server exited early"
            time.sleep(0.1)
        else:
            raise AssertionError("server did not start")
        yield port, "admin", proc, cfg
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        proc.stdout.close()


def _listening(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Browser(Client):
    """Client that follows redirects by their Location header and records where each one pointed.

    It sends Origin and Referer on a form POST the way a browser does (Fetch spec, "serializing a request
    origin"): a non-CORS request that is not GET/HEAD carries Origin ``null`` when the referrer policy of the
    page holding the form is ``no-referrer``, else the page's origin. The Referer follows the same policy."""

    def __init__(self, port, host=None):
        super().__init__(port)
        self.host = host
        self.hops = []
        self.page = None  # (url, Referrer-Policy header) of the last document the browser rendered

    def _site(self):
        return f"http://{self.host or f'127.0.0.1:{self.port}'}"

    def _browser_headers(self, method, headers):
        headers = dict(headers)
        if self.host:
            headers["Host"] = self.host
        if method == "POST" and self.page:
            url, policy = self.page
            if policy == "no-referrer":
                headers["Origin"] = "null"
            else:
                headers["Origin"] = self._site()
                if policy in ("same-origin", "origin-when-cross-origin", "strict-origin-when-cross-origin",
                              "unsafe-url", "no-referrer-when-downgrade", "origin"):
                    headers["Referer"] = url if policy != "origin" else self._site() + "/"
        return headers

    def request(self, method, path, form=None, headers=None):
        for _ in range(6):
            sent = self._browser_headers(method, headers or {})
            resp, data = super().request(method, path, form=form, headers=sent)
            if resp.status not in (301, 302, 303, 307):
                if method == "GET":
                    self.page = (self._site() + path, resp.headers.get("Referrer-Policy", ""))
                return resp, data
            loc = resp.headers["Location"]
            self.hops.append(loc)
            assert loc.startswith("/") and not loc.startswith("//"), loc  # same host and port: relative only
            method, path, form = "GET", loc, None
        raise AssertionError("redirect loop")


def test_browser_form_post_needs_a_policy_that_keeps_the_origin(env, monkeypatch):
    """Regression (TASK #121): under Referrer-Policy no-referrer a real browser sends ``Origin: null`` on a form
    POST, so every login was refused. The e2e Browser models that; the old header must fail it."""
    def login():
        b = Browser(env[1])
        _, html = b.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        return b.request("POST", "/login", form={"csrf": token, "password": PASSWORD}), b

    (resp, _), b = login()
    assert resp.status == 200 and b.hops == ["/"] and security.SEC_HEADERS["Referrer-Policy"] == "same-origin"
    monkeypatch.setitem(security.SEC_HEADERS, "Referrer-Policy", "no-referrer")  # the old, broken header
    (resp, html), _ = login()
    assert resp.status == 403 and "cross-origin request refused" in html


@pytest.mark.parametrize("host_header", [None, "localhost:{port}"])
def test_e2e_full_flow(real_server, host_header):
    port, password, proc, cfg = real_server
    b = Browser(port, host_header.format(port=port) if host_header else None)
    resp, html = b.get("/login")
    assert resp.status == 200
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    resp, html = b.request("POST", "/login", form={"csrf": token, "password": password})
    assert b.hops == ["/", "/password"] and resp.status == 200 and "default password" in html  # forced change
    assert (b.post("/settings/save", {"logs.retention_days": "3"})[0].status, b.hops[-1]) == (200, "/password")
    assert not (cfg / "kiko.toml").exists()  # a forced-password session cannot save settings
    assert proc.poll() is None  # still alive after the login redirect
    assert not (cfg / "web-initial-password").exists()
    new = "brand-new-pass1"
    resp, html = b.post("/password", {"current": password, "new": new, "again": new})
    assert resp.status == 200 and b.hops[-1] == "/password" and "Password changed" in html
    assert "Dashboard" in b.get("/")[1]  # no longer forced

    for path in ("/", "/models", "/models/new", "/providers", "/providers/new", "/classes", "/aliases", "/billing", "/logs", "/responses",
                 "/settings", "/password"):
        resp, _ = b.get(path)
        assert resp.status == 200, path

    resp, html = b.get("/models/new")  # the form offers the seeded providers as a list box
    assert '<select name="provider"' in html and '<option value="nvidia">' in html and 'name="status"' not in html
    resp, html = b.post("/providers/save", {"name": "e2eprov", "api_base": "https://e2e.example.com/v1",
                                            "api_key_env": "E2E_KEY"})
    assert resp.status == 200 and "e2eprov" in html and b.hops[-1] == "/providers"
    resp, html = b.post("/models/save", {"alias": "e2e", "provider": "e2eprov", "model_id": "big/model-1",
                                         "model_class": "SONNET", "enabled": "on"})
    assert resp.status == 200 and "e2e" in html and b.hops[-1] == "/models" and "e2eprov/big/model-1" in html
    resp, html = b.post("/providers/delete", {"name": "e2eprov"})  # in use: refused
    assert resp.status == 200 and "still used" in html and "e2eprov" in html
    resp, html = b.post("/aliases/set", {"glob": "e2e-*", "target": "e2e"})
    assert resp.status == 200 and b.hops[-1] == "/aliases"
    resp, html = b.post("/classes/add", {"name": "E2ECLS", "keyword": "e2ecls", "role": "r"})
    assert resp.status == 200 and "E2ECLS" in html and b.hops[-1] == "/classes"
    resp, html = b.post("/settings/save", {"billing.monthly_cap_usd": "5", "logs.retention_days": "3"})
    assert resp.status == 200 and "monthly_cap_usd" in html and b.hops[-1] == "/settings" and "Settings saved" in html
    saved = (cfg / "kiko.toml").read_text(encoding="utf-8")
    assert "monthly_cap_usd = 5.0" in saved and "retention_days = 3" in saved
    assert real_server[2].poll() is None

    resp, _ = b.post("/logout", {})
    assert resp.status == 200 and b.hops[-1] == "/login"
    assert b.get("/models")[1].count("Admin password") == 1  # back at the login form
    assert proc.poll() is None


# --- providers (schema v4) ---
SEEDED = {"gemini": ("https://generativelanguage.googleapis.com", "GEMINI_API_KEY"),
          "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
          "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
          "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
          "dashscope": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
          "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY")}


def test_model_form_renders_list_boxes_and_no_status(client):
    _, html = client.get("/models/new")
    assert '<select name="provider" id="f-provider">' in html
    for name in SEEDED:
        assert f'<option value="{name}">' in html
    assert '<select name="model_class" id="f-model_class">' in html and '<option value="VOICE">' in html
    assert 'name="model_id"' in html and 'list="model-ids"' in html and '<datalist id="model-ids">' in html
    assert '<option value="gemini-3.8-flash">' in html and '<select name="api_key_env"' in html
    assert 'name="status"' not in html and ">Status<" not in html
    assert "https://integrate.api.nvidia.com/v1" in html and "NVIDIA_API_KEY" in html  # defaults are shown


def test_model_save_stores_provider_slash_model_base_and_key(client, env):
    db = env[0]
    f = {"alias": "nem", "provider": "nvidia", "model_id": "nvidia/nemotron-3-ultra-550b-a55b",
         "model_class": "SONNET"}
    assert client.post("/models/save", f)[0].status == 303
    row = db.get_model("nem")
    assert (row["provider"], row["api_base"], row["api_key_env"]) == (
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b", None, "NVIDIA_API_KEY")
    f = {"alias": "gem", "provider": "gemini", "model_id": "gemini-3.8-flash", "model_class": "HAIKU",
         "api_key_env": "GOOGLE_API_KEY"}
    assert client.post("/models/save", f)[0].status == 303
    row = db.get_model("gem")
    assert (row["provider"], row["api_key_env"], row["model_class"]) == ("gemini/gemini-3.8-flash", "GOOGLE_API_KEY",
                                                                         "HAIKU")
    o = {"alias": "ov", "provider": "openai", "model_id": "gpt-4o", "api_base": "https://proxy.example.com/v1"}
    assert client.post("/models/save", o)[0].status == 303
    assert db.get_model("ov")["api_base"] == "https://proxy.example.com/v1"
    assert db.get_model("ov")["api_key_env"] == "OPENAI_API_KEY"


def test_model_save_refuses_unknown_provider_and_bad_values(client, env):
    base = {"alias": "x1", "provider": "openai", "model_id": "gpt-4o"}
    for bad in ({"provider": "nope"}, {"provider": ""}, {"model_id": ""}, {"model_id": "bad id!"},
                {"api_base": "http://evil.example.com/v1"}, {"api_key_env": "has space"}):
        resp, _ = client.post("/models/save", {**base, **bad})
        assert resp.status == 400 and env[0].get_model("x1") is None, bad


def test_custom_provider_model_gets_its_base_and_resolves(client, env):
    db = env[0]
    assert client.post("/providers/save", {"name": "acme", "api_base": "https://api.acme.example/v1",
                                           "api_key_env": "ACME_KEY"})[0].status == 303
    assert client.post("/models/save", {"alias": "am", "provider": "acme", "model_id": "m1"})[0].status == 303
    row = db.get_model("am")
    assert (row["provider"], row["api_base"], row["api_key_env"]) == ("acme/m1", "https://api.acme.example/v1",
                                                                       "ACME_KEY")
    from kiko_lite.resolver import custom_provider_names, map_row
    mapped = map_row(row, custom_provider_names(db))
    assert mapped.provider == "custom" and mapped.model_id == "m1"  # the bare id goes upstream, not "acme/m1"


def test_model_edit_round_trips(client, env):
    db = env[0]
    db.upsert_model("nemotron-ultra", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "NVIDIA_API_KEY")
    db.upsert_model("gemini-flash", "gemini/gemini-3.8-flash", "GEMINI_API_KEY")
    _, html = client.get("/models/edit?alias=nemotron-ultra")
    assert '<option value="nvidia" selected>' in html
    assert 'name="model_id" value="nvidia/nemotron-3-ultra-550b-a55b"' in html
    assert "NVIDIA_API_KEY" in html and 'name="api_base" value=""' in html
    for alias in ("nemotron-ultra", "gemini-flash"):
        before = db.get_model(alias)
        _, page = client.get("/models/edit?alias=" + alias)
        prov = re.search(r'<option value="([a-z]+)" selected>', page).group(1)
        mid = re.search(r'name="model_id" value="([^"]*)"', page).group(1)
        assert client.post("/models/save", {"alias": alias, "provider": prov, "model_id": mid,
                                            "model_class": before["model_class"], "enabled": "on"})[0].status == 303
        after = db.get_model(alias)
        assert (after["provider"], after["api_base"], after["api_key_env"]) == (
            before["provider"], before["api_base"], before["api_key_env"])


def test_edit_of_row_with_unlisted_provider_asks_to_choose(client, env):
    env[0].upsert_model("legacy", "nvidia_nim/some-model", "NVIDIA_API_KEY")
    _, html = client.get("/models/edit?alias=legacy")
    assert "not in the providers list" in html and '(choose a provider)' in html
    assert 'name="model_id" value="some-model"' in html


def test_providers_page_crud_and_in_use_refused(client, env):
    db = env[0]
    _, html = client.get("/providers")
    assert all(n in html for n in SEEDED)
    assert client.post("/providers/save", {"name": "Acme", "api_base": "https://a.example/v1",
                                           "api_key_env": "ACME_KEY"})[0].status == 303
    assert db.get_provider("acme")["api_base"] == "https://a.example/v1"
    assert "ACME_KEY" in client.get("/providers/edit?name=acme")[1]
    assert client.post("/providers/save", {"name": "acme", "api_base": "https://b.example/v1",
                                           "api_key_env": "ACME_KEY"})[0].status == 303
    assert db.get_provider("acme")["api_base"] == "https://b.example/v1"
    for bad in ({"api_base": "http://a.example/v1"}, {"api_base": "not a url"}, {"api_key_env": SECRET},
                {"api_key_env": ""}, {"name": "Bad Name"}, {"name": ""}):
        resp, html = client.post("/providers/save", {"name": "zz", "api_base": "https://z.example/v1",
                                                     "api_key_env": "ZZ_KEY", **bad})
        assert resp.status == 400 and db.get_provider("zz") is None and SECRET not in html, bad
    db.upsert_model("m", "acme/x", "ACME_KEY")
    resp, html = client.post("/providers/delete", {"name": "acme"})
    assert resp.status == 303 and db.get_provider("acme") is not None
    assert "still used" in client.get("/providers")[1]
    db.delete_model("m")
    client.post("/providers/delete", {"name": "acme"})
    assert db.get_provider("acme") is None
    assert client.get("/providers/edit?name=acme")[0].status == 404


def test_providers_page_escapes_hostile_values(client, env):
    with sqlite3.connect(env[0].path) as con:  # set_provider refuses these values, so write them raw
        con.execute("INSERT INTO providers(name, api_base, api_key_env, sort) VALUES('evil', ?, 'SOME_KEY', 900)", (QUOTE,))
        con.execute("INSERT INTO providers(name, api_base, api_key_env, sort) VALUES('evil2', 'https://x.example/v1', ?, 901)",
                    (HOSTILE,))
    for path in ("/providers", "/providers/edit?name=evil", "/providers/edit?name=evil2", "/models/new"):
        resp, html = client.get(path)
        assert resp.status == 200, path
        assert "<script>x</script>" not in html and QUOTE not in html and 'onfocus="alert' not in html, path
    _, html = client.get("/providers")
    assert "x&quot; onfocus=&quot;alert(1)" in html
    assert HOSTILE not in html and "invalid key name (hidden)" in html
    resp, html = client.post("/providers/save", {"name": "n", "api_base": QUOTE, "api_key_env": "K"})
    assert resp.status == 400 and QUOTE not in html and 'onfocus="alert' not in html
    resp, html = client.post("/providers/delete", {"name": HOSTILE})
    assert resp.status == 303 and "<script>x</script>" not in client.get("/providers")[1]


def test_api_base_override_is_validated_for_every_provider(client, env):
    base = {"alias": "ob", "model_id": "some-model"}
    bad = ("http://evil.example/v1", "https://user:pass@host.example/v1", "ftp://x.example/v1",
           "https://x.example/v1?a=b", "https://x.example/v1#f", "https://x .example/v1")
    for prov in ("nvidia", "gemini", "openai", "groq"):
        for url in bad:
            resp, _ = client.post("/models/save", {**base, "provider": prov, "api_base": url})
            assert resp.status == 400 and env[0].get_model("ob") is None, (prov, url)
    # a good override is refused for the providers whose base kiko fixes, accepted for openai
    resp, html = client.post("/models/save", {**base, "provider": "nvidia", "api_base": "https://ok.example/v1"})
    assert resp.status == 400 and "fixes the API base" in html and env[0].get_model("ob") is None
    assert client.post("/models/save", {**base, "provider": "openai", "api_base": "https://ok.example/v1"})[0].status == 303
    assert env[0].get_model("ob")["api_base"] == "https://ok.example/v1"


def test_builtin_provider_base_is_read_only_on_providers_page(client, env):
    _, html = client.get("/providers")
    assert "fixed by kiko for this provider" in html
    _, html = client.get("/providers/edit?name=nvidia")
    assert re.search(r'name="api_base"[^>]*readonly', html) and "fixed by kiko for this provider" in html
    client.post("/providers/save", {"name": "nvidia", "api_base": "https://other.example/v1", "api_key_env": "MY_NV_KEY"})
    p = env[0].get_provider("nvidia")
    assert (p["api_base"], p["api_key_env"]) == ("https://integrate.api.nvidia.com/v1", "MY_NV_KEY")
    _, html = client.get("/models/new")
    from kiko_lite import providers as ptable
    assert f"fixed by kiko for {ptable.fixed_base_hint()}" in html


def test_pasted_key_value_in_provider_is_hidden_everywhere(client, env):
    with sqlite3.connect(env[0].path) as con:
        con.execute("INSERT INTO providers(name, api_base, api_key_env, sort) VALUES('leaky','https://l.example/v1',?,950)",
                    (SECRET,))
    for path in ("/providers", "/providers/edit?name=leaky", "/models/new"):
        assert SECRET not in client.get(path)[1], path
    resp, html = client.post("/models/save", {"alias": "lk", "provider": "leaky", "model_id": "m"})
    assert resp.status == 400 and SECRET not in html and env[0].get_model("lk") is None


def test_models_page_thinking_column_and_edit_form(client, env):
    db = env[0]
    form = {"alias": "gem", "provider": "gemini", "model_id": "x", "model_class": "SONNET", "priority": "7",
            "enabled": "on"}
    assert client.post("/models/save", form)[0].status == 303
    _, html = client.get("/models")
    assert "Thinking" in html and "default" in html
    assert client.post("/models/save", {**form, "thinking": "off-prompt"})[0].status == 303
    assert db.get_model("gem")["thinking"] == "off-prompt"
    assert "off-prompt" in client.get("/models")[1]
    _, edit = client.get("/models/edit?alias=gem")
    assert 'name="thinking"' in edit and "off-prompt" in edit
    resp, html = client.post("/models/save", {**form, "thinking": "maybe"})
    assert resp.status == 400 and "thinking must be one of" in html
    assert db.get_model("gem")["thinking"] == "off-prompt"  # the bad value saved nothing


def test_builtin_provider_edit_keeps_its_stored_api_base(client, env):
    db = env[0]
    db.set_provider("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY")
    _, html = client.get("/providers/edit?name=dashscope")
    assert "https://dashscope.aliyuncs.com/compatible-mode/v1" in html
    for posted in ("https://other.example/v1", ""):
        client.post("/providers/save", {"name": "dashscope", "api_base": posted, "api_key_env": "MY_DS_KEY"})
        p = db.get_provider("dashscope")
        assert (p["api_base"], p["api_key_env"]) == ("https://dashscope.aliyuncs.com/compatible-mode/v1", "MY_DS_KEY")


def test_model_form_hint_lists_the_fixed_providers_from_the_table(client):
    from kiko_lite import providers as ptable
    _, html = client.get("/models/new")
    assert f"fixed by kiko for {ptable.fixed_base_hint()};" in html
