# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite home mode (serve --no-auth) and model classes as DB data (schema v2). Offline, temp DB only."""
import json
import logging
import sqlite3

import pytest

from kiko_lite import app as lite
from kiko_lite import db as kdb
from kiko_lite.resolver import Catalog, DBResolver
from kikolib import http as khttp
from kikolib import server as kserver
from .test_kiko_lite_cli import run, store  # noqa: F401  (fixtures: temp DB + fake keystore)

BODY = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()


def _post(app, headers=None):
    srv = kserver.Server(app, port=0).start()
    try:
        return khttp.StdlibBackend().request("POST", srv.url + "/v1/messages/count_tokens",
                                             {"content-type": "application/json", **(headers or {})}, BODY)
    finally:
        srv.stop()


# ---- home mode ----

def test_no_auth_accepts_unauthenticated_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="kiko_lite.app"):
        app = lite.build_app(lambda m: None, None, auth=False)
    assert [r.getMessage() for r in caplog.records if "home mode" in r.getMessage()]
    assert _post(app).status == 200


def test_default_still_401_without_key():
    app = lite.build_app(lambda m: None, "mk")
    assert _post(app).status == 401
    assert _post(app, {"x-api-key": "mk"}).status == 200


def test_build_app_refuses_empty_key_unless_auth_disabled():
    for bad in ("", None):
        with pytest.raises(ValueError):
            lite.build_app(lambda m: None, bad)
    lite.build_app(lambda m: None, "", auth=False)


def test_serve_no_auth_flag_and_env(run, monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: seen.append(app))
    assert run("serve")[0] == 2 and seen == []  # still fail closed without the opt-in
    assert run("serve", "--no-auth")[0] == 0 and seen[0].gateway.auth is False
    monkeypatch.setenv("KIKO_AUTH", "none")
    assert run("serve")[0] == 0 and seen[1].gateway.auth is False
    monkeypatch.delenv("KIKO_AUTH")
    run.store.d["MASTER_KEY"] = "mk"
    assert run("serve")[0] == 0 and seen[2].gateway.auth is True
    with caplog.at_level(logging.WARNING):
        assert run("serve", "--no-auth")[0] == 0
    assert seen[3].gateway.auth is False and "MASTER_KEY is ignored" in caplog.text


def test_doctor_reports_auth_mode(run, monkeypatch):
    out = run("doctor")[1]
    assert "Auth mode: client key required" in out
    monkeypatch.setenv("KIKO_AUTH", "none")
    assert "Auth mode: OFF" in run("doctor")[1]


# ---- classes as data ----

def _v1_db(path):
    """A DB as schema v1 left it: no model_classes table, user_version 1, one model row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(kdb._SCHEMA + kdb._V1)
    for t, c, d in kdb._COLUMNS:
        if c not in {r[1] for r in con.execute(f"PRAGMA table_info({t})")}:
            con.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
    con.execute("INSERT INTO models(alias, provider, api_key_env, model_class) VALUES('old','gemini/x','K','HAIKU')")
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()


def test_migration_v1_to_v2_keeps_data(tmp_path):
    p = tmp_path / "kiko.db"
    _v1_db(p)
    d = kdb.Database(p)
    assert d.schema_version() == kdb.SCHEMA_VERSION
    assert [m["alias"] for m in d.models()] == ["old"]
    cl = d.classes()
    assert [c["name"] for c in cl] == [n for n, *_ in kdb.DEFAULT_CLASSES]
    assert [c["keyword"] for c in cl][:4] == ["opus", "sonnet", "haiku", "fable"]
    assert {c["name"]: c["role"] for c in cl}["HAIKU"].startswith("laborer")
    assert {c["name"]: c["models"] for c in cl}["HAIKU"] == 1
    kdb.Database(p)  # second open: idempotent
    assert len(d.classes()) == len(kdb.DEFAULT_CLASSES)


def test_readonly_on_v1_db_uses_defaults(run, tmp_path):
    _v1_db(run.path)
    before = run.path.read_bytes()
    ro = kdb.Database(run.path, readonly=True)
    assert ro.class_names() == list(kdb.CLASSES) and ro.schema_version() == 1
    code, out, _ = run("list-classes")
    assert code == 0 and "HAIKU" in out and "not migrated" in out
    assert run("list-models")[0] == 0
    assert run.path.read_bytes() == before and ro.schema_version() == 1


def test_add_class_and_model_and_resolver(run):
    code, out, _ = run("add-class", "VOCAL", "--keyword", "vocal", "--role", "speech")
    assert code == 0 and "ANTHROPIC_DEFAULT_VOCAL_MODEL" in out
    out = run("list-classes")[1]
    assert "VOCAL" in out and "speech" in out and out.index("FABLE") < out.index("VOCAL")
    assert run("add-model", "v1", "--provider", "gemini/gemini-3.8-flash", "--api-key-env", "GEMINI_API_KEY",
               "--class", "vocal")[0] == 0
    assert run("add-model", "x", "--provider", "gemini/g", "--api-key-env", "K", "--class", "NOPE")[0] == 2
    d = kdb.Database(run.path)
    assert [c["models"] for c in d.classes() if c["name"] == "VOCAL"] == [1]
    assert DBResolver(Catalog(d, 0.0), {})("my-Vocal-model-3") == ("db/v1", "gemini-3.8-flash")
    # old classes still work
    assert DBResolver(Catalog(d, 0.0), {})("claude-sonnet-x") is None  # no sonnet model, no env: unknown name


def test_add_class_validation(run):
    for argv in (["vocal", "--keyword", "vocal"], ["1X", "--keyword", "x"], ["A" * 33, "--keyword", "x"],
                 ["VOCAL", "--keyword", "Vocal"], ["VOCAL", "--keyword", ""], ["OPUS", "--keyword", "zzz"],
                 ["NEW", "--keyword", "sonnet"]):
        assert run("add-class", *argv)[0] == 2, argv
    assert run("add-class", "VOCAL", "--keyword", "vocal")[0] == 0
    assert run("add-class", "VOCAL", "--keyword", "vocal2")[0] == 2


def test_env_fallback_for_new_class(run):
    run("add-class", "GAZE", "--keyword", "gaze")
    d = kdb.Database(run.path)
    r = DBResolver(Catalog(d, 0.0), {"ANTHROPIC_DEFAULT_GAZE_MODEL": "gemini/vision-1"})
    assert r("some-gaze-model") == ("gemini", "vision-1")


def test_import_accepts_new_class(run, tmp_path):
    run("add-class", "VOCAL", "--keyword", "vocal")
    y = tmp_path / "c.yaml"
    y.write_text("model_list:\n  - model_name: v\n    model_class: VOCAL\n    litellm_params:\n"
                 "      model: gemini/g\n      api_key: os.environ/GEMINI_API_KEY\n", encoding="utf-8")
    assert run("import", str(y))[0] == 0
    assert kdb.Database(run.path).get_model("v")["model_class"] == "VOCAL"


def test_simple_resolver_default_keywords_and_custom():
    r = lite.SimpleResolver(env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "gemini/a"})
    assert r("claude-opus-4") == ("gemini", "a")
    r = lite.SimpleResolver(classes={"vocal": "gemini/v"}, class_defs=(("VOCAL", "vocal"),), env={})
    assert r("x-vocal") == ("gemini", "v")


def test_simple_resolver_env_uses_class_name_not_keyword():
    r = lite.SimpleResolver(class_defs=(("VOCAL", "speech"),), env={"ANTHROPIC_DEFAULT_VOCAL_MODEL": "gemini/v"})
    assert r("my-speech-1") == ("gemini", "v")
    assert lite.SimpleResolver(class_defs=(("VOCAL", "speech"),),
                               env={"ANTHROPIC_DEFAULT_SPEECH_MODEL": "gemini/v"})("my-speech-1") is None


def test_add_class_rejects_overlapping_and_short_keywords(run):
    for kw in ("opus-max", "son", "pus", "o", "a", "ab", "SONNET"):
        code, _, err = run("add-class", "NEWC", "--keyword", kw)
        assert code == 2, kw
    code, _, err = run("add-class", "NEWC", "--keyword", "opus-max")
    assert "OPUS" in err
    assert run("add-class", "VOCAL", "--keyword", "vocal")[0] == 0
    assert run("add-class", "OTHER", "--keyword", "vocal-hd")[0] == 2  # contains 'vocal'
    d = kdb.Database(run.path)
    assert DBResolver(Catalog(d, 0.0), {"ANTHROPIC_DEFAULT_VOCAL_MODEL": "gemini/v"})("a-vocal-b") == ("gemini", "v")


def test_add_class_integrity_error_is_a_normal_error(run, monkeypatch, capsys):
    def boom(self, *a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: model_classes.name")
    monkeypatch.setattr(kdb.Database, "add_class", boom)
    code, _, err = run("add-class", "VOCAL", "--keyword", "vocal")
    assert code == 2 and "UNIQUE" in err


def test_v2_db_opens_without_writing(tmp_path, monkeypatch):
    import os
    import stat
    p = tmp_path / "kiko.db"
    kdb.Database(p).upsert_model("m", "gemini/x", "K")
    # no write statement at all on an already migrated DB
    stmts = []
    real = sqlite3.connect

    def spy(*a, **k):
        con = real(*a, **k)
        con.set_trace_callback(stmts.append)
        return con
    monkeypatch.setattr(sqlite3, "connect", spy)
    kdb.Database(p)
    assert not [s for s in stmts if s.lstrip().upper().startswith(("INSERT", "CREATE", "ALTER", "UPDATE"))]
    monkeypatch.setattr(sqlite3, "connect", real)
    for f in tmp_path.glob("kiko.db*"):
        os.chmod(f, stat.S_IREAD)
    try:
        sqlite3.connect(p).execute("CREATE TABLE zz(a)")
        pytest.skip("this OS/user can write a read-only file")
    except sqlite3.OperationalError:
        pass
    try:
        d = kdb.Database(p)
        assert [m["alias"] for m in d.models()] == ["m"]
    finally:
        for f in tmp_path.glob("kiko.db*"):
            os.chmod(f, stat.S_IREAD | stat.S_IWRITE)
