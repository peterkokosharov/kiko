# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""web-ui hardening (task 118 fix round 1): CSP, XSS-safe templates, initial password file, PBKDF2, resolvers."""
import hashlib
import re

import pytest

from kiko_lite import db as kdb
from kiko_lite import keys
from kiko_lite.db import Database
from kikolib.server import Server

from .test_webui import MAIN, Client, env, webui_mod  # noqa: F401  (fixtures)

CSP_WANTED = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; "
              "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
WEB = MAIN.parent


def test_security_headers_on_every_response_kind(env):
    ui, c, _ = env
    c.login(ui.initial_password)
    for path in ("/login", "/", "/static/style.css", "/api/models", "/static/nope.js"):
        st, _, r = c.call("GET", path)
        assert r.getheader("Content-Security-Policy") == CSP_WANTED, path
        assert r.getheader("X-Content-Type-Options") == "nosniff", path
        assert r.getheader("Referrer-Policy") == "no-referrer", path


def test_templates_and_js_are_csp_clean_and_escaped():
    """No inline script/style/handlers (CSP would block them); no unescaped template expression inside quotes."""
    for t in (WEB / "templates").glob("*.html"):
        s = t.read_text(encoding="utf-8")
        assert "SPDX-License-Identifier: MIT" in s, t.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", s), t.name
        assert "<style" not in s and " style=" not in s, t.name
        assert not re.search(r"\son[a-z]+\s*=", s), t.name
    safe = ("escapeHtml(", "Number(", "CSS.escape(", "sizes[", "types[", "sortable", "found ?")  # constants
    for f in (WEB / "static").rglob("*.js"):
        s = f.read_text(encoding="utf-8")
        assert "SPDX-License-Identifier: MIT" in s, f.name
        assert "createElement('style')" not in s, f.name
        for m in re.finditer(r'="([^"\n]*)"', s):  # attribute values inside template literals
            for expr in re.findall(r"\$\{([^}]*)\}", m.group(1)):
                assert expr.startswith(safe), f"{f.name}: unescaped expression in attribute: {expr}"
    assert "SPDX-License-Identifier: MIT" in (WEB / "static" / "style.css").read_text(encoding="utf-8")


def test_escape_html_escapes_quotes():
    s = (WEB / "static" / "core" / "utils.js").read_text(encoding="utf-8")
    body = s.split("export function escapeHtml")[1].split("\n}")[0]
    assert "createElement" not in body
    for ent in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ent in s


def test_initial_password_file_not_stdout(webui_mod, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("KIKO_DB_PATH", raising=False)

    class Stop(Exception):
        pass

    def stop(*a, **kw):
        raise Stop

    monkeypatch.setattr(webui_mod.kserver, "serve", stop)
    with pytest.raises(Stop):
        webui_mod.main(["--port", "0"])
    out = capsys.readouterr().out
    pw_file = tmp_path / "cfg" / "web-ui-initial-password"
    pw = pw_file.read_text(encoding="utf-8").strip()
    assert len(pw) >= 12 and pw not in out and str(pw_file) in out
    if webui_mod.sys.platform != "win32":
        assert (pw_file.stat().st_mode & 0o777) == 0o600
    assert (tmp_path / "cfg" / "kiko.db").exists()  # one resolver: KIKO_CONFIG_DIR is honoured


def test_initial_password_file_removed_after_change(webui_mod, tmp_path):
    f = tmp_path / "pw"
    ui = webui_mod.WebUI(Database(tmp_path / "kiko.db"), password_file=f)
    assert f.read_text(encoding="utf-8").strip() == ui.initial_password
    with Server(ui.build_app(), port=0) as srv:
        c = Client(srv)
        c.login(ui.initial_password)
        st, _ = c.json("POST", "/api/auth/change-password",
                       {"current_password": ui.initial_password, "new_password": "longenough1"})
        assert st == 200
    assert not f.exists()


def test_pbkdf2_600k_new_and_old_hash_verifies(webui_mod):
    assert webui_mod.hash_password("pw-pw-pw").split("$")[1] == "600000"
    salt = b"s" * 16
    dk = hashlib.pbkdf2_hmac("sha256", b"oldpass1", salt, 200000).hex()
    old = "pbkdf2_sha256$200000$" + salt.hex() + "$" + dk
    assert webui_mod.verify_password("oldpass1", old) and not webui_mod.verify_password("nope", old)


def test_key_source_reports_name_only(monkeypatch, tmp_path):
    monkeypatch.setenv("T118_KEY", "sekret-value")
    assert keys.key_source("T118_KEY", {}) == "env"
    (tmp_path / ".env").write_text("T118_DOT=dotval\n", encoding="utf-8")
    vals = keys.dotenv_values(tmp_path / "kiko.db")
    assert keys.key_source("T118_DOT", vals) == "dotenv"
    assert keys.key_source("T118_NONE_X", vals) is None


def test_default_path_honours_config_dir(tmp_path, monkeypatch):
    assert kdb.default_path(None, {"KIKO_CONFIG_DIR": str(tmp_path)}) == tmp_path / "kiko.db"
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("KIKO_DB_PATH", raising=False)
    assert kdb.default_path() == tmp_path / "kiko.db"


def test_db_query_public(tmp_path):
    assert Database(tmp_path / "kiko.db").query("SELECT COUNT(*) AS n FROM billing") == [{"n": 0}]
