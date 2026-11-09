# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite.db / resolver / billing / monthly cap: offline, temp DB files only (never ~/.kiko)."""
import asyncio
import importlib
import io
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiko_lite import app as lite
from kiko_lite import db as kdb
from kiko_lite import prices
from kiko_lite.resolver import (BillingWriter, Catalog, DBProviders, DBResolver, MappingError, build_db_app,
                                map_row, show_name, valid_key_name)
from kikolib import http as khttp
from kikolib import retry, server

# kiko-full's own schema, copied from litellm/proxy/integrations/kiko_sqlite_logger.py (_SCHEMA).
KIKO_FULL_SCHEMA = """
CREATE TABLE IF NOT EXISTS billing (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL    NOT NULL DEFAULT 0.0,
    ts         TEXT    NOT NULL,
    route      TEXT
);
CREATE TABLE IF NOT EXISTS models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alias       TEXT UNIQUE NOT NULL,
    provider    TEXT NOT NULL,
    api_base    TEXT,
    api_key_env TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'unknown',
    last_check  TEXT,
    model_class TEXT NOT NULL DEFAULT 'SONNET',
    priority    INTEGER NOT NULL DEFAULT 100
);
CREATE TABLE IF NOT EXISTS responses (
    id          TEXT PRIMARY KEY,
    input_json  TEXT NOT NULL,
    output_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
OLD_SCHEMA = """
CREATE TABLE billing (id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, tokens_in INTEGER NOT NULL
    DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0, ts TEXT NOT NULL);
CREATE TABLE models (id INTEGER PRIMARY KEY AUTOINCREMENT, alias TEXT UNIQUE NOT NULL, provider TEXT NOT NULL,
    api_base TEXT, api_key_env TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown', last_check TEXT);
"""


def cols(path, table):
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def kiko_full_db(path, schema=KIKO_FULL_SCHEMA):
    con = sqlite3.connect(path)
    con.executescript(schema)
    con.execute("INSERT INTO models(alias, provider, api_base, api_key_env) VALUES('old','gemini/g','','GEMINI_API_KEY')")
    con.execute("INSERT INTO billing(model, tokens_in, tokens_out, cost_usd, ts) VALUES('m',1,2,0.25,?)",
                (datetime.now(timezone.utc).isoformat(),))
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _isolated_default_db(monkeypatch, tmp_path):
    """Nothing here may resolve ~/.kiko/kiko.db: even the default path points into tmp."""
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "isolated-default" / "kiko.db"))


@pytest.fixture
def db(tmp_path):
    return kdb.Database(tmp_path / "t" / "kiko.db")


# ---- schema / migration ----

def test_fresh_db_created_with_all_tables_and_version(tmp_path):
    d = kdb.Database(tmp_path / "sub" / "kiko.db")
    assert d.path.exists() and d.schema_version() == 8  # the one literal pin: bump it with the schema
    con = sqlite3.connect(d.path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"billing", "models", "responses", "aliases", "settings"} <= tables
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
    assert {"price_in_per_mtok", "price_out_per_mtok", "price_cache_read_per_mtok"} <= cols(d.path, "models")
    assert {"attempts", "latency_ms", "upstream_model", "error", "route"} <= cols(d.path, "billing")


def test_migrates_kiko_full_db_keeping_rows(tmp_path):
    p = tmp_path / "kiko.db"
    kiko_full_db(p)
    d = kdb.Database(p)
    assert d.schema_version() == kdb.SCHEMA_VERSION
    assert [m["alias"] for m in d.models()] == ["old"]
    assert d.models()[0]["price_in_per_mtok"] is None
    assert d.month_spent() == pytest.approx(0.25)
    kdb.Database(p)  # idempotent
    assert d.schema_version() == kdb.SCHEMA_VERSION and len(d.models()) == 1


def test_migrates_pre_route_kiko_db(tmp_path):
    p = tmp_path / "kiko.db"
    con = sqlite3.connect(p)
    con.executescript(OLD_SCHEMA)
    con.commit()
    con.close()
    kdb.Database(p)
    assert {"route", "attempts"} <= cols(p, "billing")
    assert {"model_class", "priority", "price_in_per_mtok"} <= cols(p, "models")


def test_migration_failure_is_raised_and_version_not_set(tmp_path):
    p = tmp_path / "kiko.db"
    kiko_full_db(p)
    blocker = sqlite3.connect(p, timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # holds the write lock
    try:
        with pytest.raises(sqlite3.OperationalError):
            kdb.Database(p, busy_timeout=0.1)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    con = sqlite3.connect(p)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()
    assert "price_in_per_mtok" not in cols(p, "models")
    assert kdb.Database(p).schema_version() == kdb.SCHEMA_VERSION  # and it works once the lock is gone


def test_readonly_open_does_not_migrate(tmp_path):
    p = tmp_path / "kiko.db"
    kiko_full_db(p)
    ro = kdb.Database(p, readonly=True)
    assert ro.schema_version() == 0 and [m["alias"] for m in ro.models()] == ["old"]
    assert ro.aliases() == [] and ro.settings() == {} and ro.billing_summary(30)[0]["errors"] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.set_alias("a", "b")
    assert cols(p, "models").isdisjoint({"price_in_per_mtok"})


def test_alias_differing_only_in_case_is_rejected(db):
    db.upsert_model("Flash", "gemini/x", "K")
    with pytest.raises(ValueError, match="only in case"):
        db.upsert_model("flash", "gemini/x", "K")
    db.upsert_model("Flash", "gemini/y", "K")  # the same alias is a normal update
    # a kiko-full DB may already hold both: the first one wins, the other is unusable
    con = sqlite3.connect(db.path)
    con.execute("INSERT INTO models(alias, provider, api_key_env) VALUES('FLASH','gemini/z','K')")
    con.commit()
    con.close()
    snap = Catalog(db, 0.0).snapshot()
    assert snap.by_alias["Flash"].mapped is not None
    assert snap.by_alias["FLASH"].mapped is None and "only in case" in snap.by_alias["FLASH"].error


@pytest.mark.parametrize("name,ok", [
    ("GEMINI_API_KEY", True), ("KIKO_MY_ALIAS_API_KEY", True), ("K", True), ("_X1", True),
    ("KIKO_" + "A_" * 20 + "API_KEY", True),
    ("gemini_api_key", True), ("MixedCase_Name", True), ("gsk_" + "aB3dE5gH7j" * 5 + "xY", False), ("AIza" + "Sy" + "b" * 33, False),
    ("nvapi-" + "x1Y2z3" * 8, False), ("sk-abc", False), ("AKIA" + "ABCDEFGHIJKLMNOP", False),
    ("A" * 40 + "1", False), ("A" * 129, False), ("", False), (None, False), ("has space", False),
])
def test_valid_key_name(name, ok):
    assert valid_key_name(name) is ok
    assert (show_name(name) == name) is ok
    if not ok:
        assert show_name(name) == "<invalid name, value hidden>"


@pytest.mark.parametrize("name,why", [
    ("GEMINI_API_KEY", None), ("gemini_api_key", None), ("gsk_" + "a" * 52, "value"), ("AIza" + "b" * 35, "value"),
    ("sk-ant-" + "x" * 20, "value"), ("A" * 40 + "1", "value"), ("x" * 24 + " y", "value"),
    ("bad name!", "name"), ("", "name"), (None, "name"), ("1ABC", "name"),
])
def test_key_name_problem_reasons(name, why):
    from kiko_lite.resolver import key_name_problem
    assert key_name_problem(name) == why


@pytest.mark.parametrize("path,uri", [
    ("C:\\Users\\a b\\k.db", "file:///C:/Users/a%20b/k.db?mode=ro"),
    ("C:/x/k.db", "file:///C:/x/k.db?mode=ro"),
    ("\\\\server\\share\\x.db", "file:////server/share/x.db?mode=ro"),
    ("\\\\?\\UNC\\server\\share\\x.db", "file:////server/share/x.db?mode=ro"),
    ("\\\\?\\C:\\x\\k.db", "file:///C:/x/k.db?mode=ro"),
    ("/tmp/a?b#c%d/k.db", "file:///tmp/a%3Fb%23c%25d/k.db?mode=ro"),
    ("C:\\q?\\#\\%41\\k.db", "file:///C:/q%3F/%23/%2541/k.db?mode=ro"),
])
def test_ro_uri_string(path, uri):
    assert kdb.ro_uri(path) == uri


@pytest.mark.parametrize("dirname", ["a#b", "c%d", "e%20f", "g?h", "i j"])
def test_readonly_open_in_odd_directory_names(tmp_path, dirname):
    d = tmp_path / dirname
    try:
        d.mkdir()
    except OSError:
        pytest.skip(f"this OS does not allow {dirname!r} in a folder name")
    p = d / "kiko.db"
    kdb.Database(p).upsert_model("m", "gemini/x", "K")
    ro = kdb.Database(p, readonly=True)
    assert [m["alias"] for m in ro.models()] == ["m"] and ro.schema_version() == kdb.SCHEMA_VERSION


def test_checkpoint_reports_busy_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(kdb, "BUSY_TIMEOUT", 0.2)
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("m", "gemini/x", "K")
    reader = sqlite3.connect(d.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM models").fetchall()  # holds a read snapshot
    try:
        assert d.set_key_name("m", "NEWNAME") == (True, False)
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    assert d.checkpoint() is True


def test_two_aliases_differing_in_case_through_the_gateway(wired):
    """kiko-full DB with 'Flash' and 'FLASH': the first row works and is billed with ITS price; the other is 404."""
    wired.db.upsert_model("Flash", "openai/some-model", "MY_UP_KEY", "https://llm.example.org/v1", "OPUS", 1,
                          2.0, 8.0)
    con = sqlite3.connect(wired.db.path)
    con.execute("INSERT INTO models(alias, provider, api_base, api_key_env, model_class, priority,"
                " price_in_per_mtok, price_out_per_mtok) VALUES('FLASH','openai/some-model',"
                "'https://llm.example.org/v1','MY_UP_KEY','OPUS',1,100.0,100.0)")
    con.commit()
    con.close()
    wired.fake.script.append(OK)
    body = {"max_tokens": 5, "messages": [{"role": "user", "content": "x"}]}
    assert wired.post({"model": "Flash", **body}).status == 200
    (row,) = rows(wired.db)
    assert row["cost_usd"] == pytest.approx(0.006)  # 1000 * 2 + 500 * 8 per Mtok, not the 100/100 of 'FLASH'
    assert wired.post({"model": "FLASH", **body}).status == 404
    from kiko_lite.resolver import Catalog as C
    snap = C(wired.db, 0.0).snapshot()
    assert snap.by_pname["db/flash"].row["alias"] == "Flash"
    assert "only in case" in snap.by_alias["FLASH"].error


def test_errored_request_with_usage_is_still_billed(db):
    add(db, "m", "openai/some-model", api_base="https://llm.example.org/v1", price_in=2.0, price_out=8.0)
    w = BillingWriter(db, Catalog(db, 0.0))
    w(lite.RequestResult(request_id="r", client_model="c", provider="db/m", upstream_model="some-model",
                         stream=True, attempts=1, status=200, error="api_error",
                         usage={"input_tokens": 1000, "output_tokens": 500}))
    (row,) = db.billing_summary(1)
    assert row["errors"] == 1 and row["cost_usd"] == pytest.approx(0.006)


def test_default_path_order(tmp_path):
    assert kdb.default_path("x.db", {"KIKO_DB_PATH": "env.db"}) == Path("x.db")
    assert kdb.default_path(None, {"KIKO_DB_PATH": "env.db"}) == Path("env.db")
    assert kdb.default_path(None, {}) == Path.home() / ".kiko" / "kiko.db"


def test_kiko_full_code_reads_and_writes_our_db(tmp_path, monkeypatch):
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "oracle-default.db"))  # its module creates a DB on import
    try:
        full = importlib.import_module("litellm.proxy.integrations.kiko_sqlite_logger")
    except Exception as e:  # litellm not importable here
        pytest.skip(f"kiko-full logger not importable: {type(e).__name__}")
    p = tmp_path / "kiko.db"
    d = kdb.Database(p)
    d.upsert_model("ours", "gemini/gemini-3.8-flash", "GEMINI_API_KEY", None, "HAIKU", 1, 0.75, 3.75, 0.075,
                   status="ok")
    full._init_db(p)  # kiko-full opening our DB is a no-op
    assert full.get_models_by_class(p, "HAIKU") == ["ours"]
    assert full.load_models(p)[0]["model_name"] == "ours"
    assert full.add_model(p, "theirs", "openai/x", "OPENAI_API_KEY", None, "OPUS", 7) is True
    full.seed_models(p, [{"model_name": "seeded", "litellm_params": {"model": "groq/y", "api_key": "os.environ/GROQ_API_KEY"}}])
    full.update_model_status(p, "theirs", "ok")
    logger = full.KikoSQLiteLogger(db_path=p)
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4)
    asyncio.run(logger.async_log_success_event(
        {"model": "m", "response_cost": 0.5, "litellm_params": {"metadata": {"route": "messages"}}},
        SimpleNamespace(usage=usage), datetime.now(timezone.utc), None))
    full.store_response(p, "r1", "hi", "yo")
    assert full.load_response(p, "r1") is not None
    assert full.get_billing_summary(p, 30)[0]["cost_usd"] == pytest.approx(0.5)
    # and we read what kiko-full wrote
    assert {m["alias"] for m in d.models()} == {"ours", "theirs", "seeded"}
    assert d.month_spent() == pytest.approx(0.5)
    assert d.schema_version() == kdb.SCHEMA_VERSION
    # kiko-full's INSERT OR REPLACE must not make our upsert unusable
    d.upsert_model("theirs", "openai/x", "OPENAI_API_KEY", price_in=1.0, price_out=2.0)
    assert d.get_model("theirs")["price_in_per_mtok"] == 1.0


# ---- CRUD / concurrency ----

def test_upsert_keeps_id_status_and_prices(db):
    assert db.upsert_model("a", "gemini/x", "K", None, "haiku", 5, 1.0, 2.0, None, status="ok") is True
    first = db.get_model("a")
    assert db.upsert_model("a", "gemini/y", "K2", None, "SONNET", 9) is False
    second = db.get_model("a")
    assert second["id"] == first["id"] and second["status"] == "ok" and second["enabled"] == 1
    assert (second["provider"], second["api_key_env"], second["model_class"], second["priority"]) == \
        ("gemini/y", "K2", "SONNET", 9)
    assert second["price_in_per_mtok"] == 1.0  # not overwritten by a missing price
    with pytest.raises(ValueError):
        db.upsert_model("b", "gemini/x", "K", model_class="nope")
    assert db.set_enabled("a", False) and db.get_model("a")["enabled"] == 0
    assert db.set_enabled("zz", True) is False
    assert db.delete_model("a") and not db.delete_model("a")


def test_aliases_keep_insert_order_and_settings(db):
    db.set_alias("claude-*", "t1")
    db.set_alias("*", "t2")
    db.set_alias("claude-*", "t3")
    assert db.aliases() == [("claude-*", "t3"), ("*", "t2")]
    db.set_setting("monthly_cap_usd", "5")
    assert db.settings() == {"monthly_cap_usd": "5"}
    assert db.delete_alias("*") and db.delete_setting("monthly_cap_usd")


def test_thread_safe_writes(db):
    def work():
        for _ in range(10):
            db.add_billing("m", 1, 1, 0.01)
    threads = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert db.billing_summary(1)[0]["calls"] == 120


def test_month_spent_counts_this_month_only(db):
    db.add_billing("m", 1, 1, 1.5)
    db.add_billing("m", 1, 1, 100.0, ts=(datetime.now(timezone.utc) - timedelta(days=400)).isoformat())
    assert db.month_spent() == pytest.approx(1.5)
    assert db.billing_summary(30)[0]["cost_usd"] == pytest.approx(1.5)


# ---- row -> provider mapping ----

@pytest.mark.parametrize("provider,api_base,expect,base", [
    ("gemini/gemini-3.8-flash", None, ("gemini", "gemini-3.8-flash"), "https://generativelanguage.googleapis.com"),
    ("nvidia_nim/meta/llama-3.3-70b", None, ("nvidia", "meta/llama-3.3-70b"), "https://integrate.api.nvidia.com/v1"),
    ("groq/llama-3.3-70b-versatile", None, ("groq", "llama-3.3-70b-versatile"), "https://api.groq.com/openai/v1"),
    ("dashscope/qwen-plus", "", ("dashscope", "qwen-plus"), "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    ("openai/nvidia/nemotron-3-super-120b-a12b", "https://integrate.api.nvidia.com/v1",
     ("nvidia", "nvidia/nemotron-3-super-120b-a12b"), "https://integrate.api.nvidia.com/v1"),
    ("openai/x", "https://api.groq.com/openai/v1", ("groq", "x"), "https://api.groq.com/openai/v1"),
    ("openai/qwen-max", "https://dashscope.aliyuncs.com/compatible-mode/v1", ("dashscope", "qwen-max"),
     "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("openai/gpt-4o", "https://api.openai.com/v1", ("openai", "gpt-4o"), "https://api.openai.com/v1"),
    ("openai/gpt-4o", None, ("openai", "gpt-4o"), "https://api.openai.com/v1"),
    ("openai/m", "https://llm.example.org/v1/", ("custom", "m"), "https://llm.example.org/v1"),
    ("openai/m", "http://localhost:8000/v1", ("custom", "m"), "http://localhost:8000/v1"),
    ("openai/m", "http://127.0.0.1:8000", ("custom", "m"), "http://127.0.0.1:8000"),
])
def test_map_row(provider, api_base, expect, base):
    m = map_row({"provider": provider, "api_base": api_base, "api_key_env": "MY_KEY"})
    assert (m.provider, m.model_id) == expect
    assert m.entry["base_url"] == base and m.entry["key_names"] == ["MY_KEY"]


@pytest.mark.parametrize("provider,api_base", [
    ("openai/m", "http://evil.example.org/v1"), ("openai/m", "ftp://x.example.org"),
    ("openai/m", "https://user:pw@x.example.org/v1"), ("gemini/", None), ("", None),
])
def test_map_row_rejects(provider, api_base):
    with pytest.raises(MappingError):
        map_row({"provider": provider, "api_base": api_base, "api_key_env": "K"})


def test_map_row_bad_key_name_gives_no_key_names():
    m = map_row({"provider": "gemini/x", "api_base": None, "api_key_env": "sk-not-a-name"})
    assert m.entry["key_names"] == []


# ---- resolver ----

def add(db, alias, provider="gemini/g", cls="SONNET", prio=100, status=None, enabled=None, **kw):
    db.upsert_model(alias, provider, kw.pop("key", "K"), kw.pop("api_base", None), cls, prio, status=status,
                    enabled=enabled, **kw)


def make(db, env=None, ttl=0.0):
    cat = Catalog(db, ttl)
    return DBResolver(cat, env if env is not None else {}), DBProviders(cat), cat


def test_resolver_order_alias_then_class_then_env_then_name(db):
    add(db, "gem", "gemini/gemini-3.8-flash", "SONNET", 10, "ok")
    add(db, "nim", "nvidia_nim/m", "OPUS", 10, "ok")
    add(db, "tiny", "groq/t", "HAIKU", 10, "ok")
    db.set_alias("claude-sonnet-*", "tiny")  # alias beats class routing
    r, prov, _ = make(db, {"ANTHROPIC_DEFAULT_FABLE_MODEL": "nim", "ANTHROPIC_DEFAULT_OPUS_MODEL": "tiny"})
    assert r("claude-sonnet-4") == ("db/tiny", "t")
    assert r("claude-opus-4") == ("db/nim", "m")  # class beats env
    assert r("claude-fable-1") == ("db/nim", "m")  # no FABLE model: env
    assert r("gem") == ("db/gem", "gemini-3.8-flash")  # direct alias name
    assert r("groq/direct-id") == ("groq", "direct-id")  # direct provider/model form
    assert r("nonexistent") is None
    assert prov["db/gem"]["kind"] == "gemini" and prov["groq"]["kind"] == "openai"
    assert "db/gem" in prov and "db/nope" not in prov


def test_class_routing_priority_status_enabled(db):
    add(db, "slow", cls="HAIKU", prio=50, status="ok")
    add(db, "fast", cls="HAIKU", prio=5, status="ok")
    add(db, "broken", cls="HAIKU", prio=1, status="error")
    add(db, "fresh", cls="HAIKU", prio=2)  # unknown: only if no ok model
    r, _, _ = make(db)
    assert r("claude-3-haiku")[0] == "db/fast"
    db.set_enabled("fast", False)
    assert r("claude-3-haiku")[0] == "db/slow"
    db.set_enabled("slow", False)
    assert r("claude-3-haiku")[0] == "db/fresh"  # unknown ok-fallback, never the error one
    db.set_enabled("fresh", False)
    assert r("claude-3-haiku") is None


def test_alias_to_disabled_or_unknown_target_is_none(db):
    add(db, "gem", enabled=False)
    db.set_alias("a-*", "gem")
    db.set_alias("b-*", "nothing")
    r, _, _ = make(db)
    assert r("a-1") is None and r("b-1") is None


def test_resolver_picks_up_edits_after_ttl(db):
    clock = [0.0]
    cat = Catalog(db, ttl=2.0, clock=lambda: clock[0])
    r = DBResolver(cat, {})
    add(db, "one")
    assert r("one") is not None
    db.set_enabled("one", False)
    assert r("one") is not None  # cached
    clock[0] = 2.5
    assert r("one") is None


def test_unmappable_row_is_skipped(db):
    add(db, "bad", "openai/m", api_base="http://evil.example.org/v1", cls="HAIKU", status="ok")
    r, _, _ = make(db)
    assert r("bad") is None and r("claude-haiku") is None


# ---- prices ----

def test_prices_lookup_and_cost():
    assert prices.lookup("gemini", "gemini-3.8-flash") == (0.75, 3.75, 0.075)
    assert prices.lookup("nvidia", "nvidia/nemotron-3-super-120b-a12b") is None
    assert prices.cost(1000, 500, 0, 2.0, 8.0, None) == pytest.approx(0.006)
    assert prices.cost(100, 0, 1000, 1.0, 1.0, 0.1) == pytest.approx(0.0002)
    assert prices.cost(100, 100, 0, None, None, None) == 0.0


# ---- the app wired to the DB: billing rows, cap, key names ----

class FakeHttp:
    def __init__(self):
        self.calls = []
        self.script = []

    def request(self, method, url, headers=None, body=None, timeout=0, stream=False):
        self.calls.append({"url": url, "headers": dict(headers or {}), "body": json.loads(body or b"{}")})
        status, data = self.script.pop(0) if self.script else (500, {"error": {"message": "empty script"}})
        return khttp.Response(status, "x", khttp.Headers([("content-type", "application/json")]),
                              io.BytesIO(json.dumps(data).encode()))

    def close(self):
        pass


OK = (200, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})
MASTER = "master-key"


@pytest.fixture
def wired(db, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    fake = FakeHttp()
    add(db, "cheap", "openai/some-model", "SONNET", 1, "ok", api_base="https://llm.example.org/v1", key="MY_UP_KEY",
        price_in=2.0, price_out=8.0)
    policy = retry.RetryPolicy(attempts=1, base_delay=0.01, max_delay=0.02, budget=1.0, jitter=False)
    app = build_db_app(db, MASTER, secret={"MY_UP_KEY": "UPKEY-VALUE"}.get, env={}, ttl=0.0, http=fake,
                       policy=policy, ping_interval=0.2, upstream_timeout=2.0)
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()

    def post(body=None, stream=False):
        body = body or {"model": "claude-sonnet-4", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        return client.request("POST", srv.url + "/v1/messages",
                              {"x-api-key": MASTER, "content-type": "application/json"}, json.dumps(body).encode())

    yield SimpleNamespace(db=db, fake=fake, post=post)
    client.close()
    srv.stop()


def rows(db):
    con = sqlite3.connect(db.path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM billing ORDER BY id")]
    finally:
        con.close()


def test_success_writes_billing_row_with_cost_and_uses_row_key_name(wired):
    wired.fake.script.append(OK)
    r = wired.post()
    assert r.status == 200
    sent = {k.lower(): v for k, v in wired.fake.calls[0]["headers"].items()}
    assert sent["authorization"] == "Bearer UPKEY-VALUE"
    assert wired.fake.calls[0]["url"] == "https://llm.example.org/v1/chat/completions"
    (row,) = rows(wired.db)
    assert (row["model"], row["tokens_in"], row["tokens_out"], row["route"]) == ("some-model", 1000, 500, "messages")
    assert row["cost_usd"] == pytest.approx(0.006)
    assert row["error"] is None and row["attempts"] == 1 and row["upstream_model"] == "custom/some-model"
    assert row["latency_ms"] >= 0 and row["ts"][:4].isdigit()


def test_failed_request_is_recorded_with_error_and_zero_cost(wired):
    wired.fake.script.append((400, {"error": {"message": "bad"}}))
    assert wired.post().status == 400
    (row,) = rows(wired.db)
    assert row["error"] == "invalid_request_error" and row["cost_usd"] == 0.0 and row["route"] == "messages"


def test_unknown_price_costs_zero(wired):
    wired.db.upsert_model("nop", "openai/zzz", "MY_UP_KEY", "https://llm.example.org/v1", "OPUS", 1, status="ok")
    wired.fake.script.append(OK)
    assert wired.post({"model": "claude-opus-4", "max_tokens": 5,
                       "messages": [{"role": "user", "content": "x"}]}).status == 200
    (row,) = rows(wired.db)
    assert row["tokens_in"] == 1000 and row["cost_usd"] == 0.0


def test_price_falls_back_to_price_table(wired):
    wired.db.upsert_model("g", "openai/gpt-4o", "MY_UP_KEY", None, "OPUS", 1, status="ok")
    wired.fake.script.append(OK)
    wired.post({"model": "claude-opus-4", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]})
    (row,) = rows(wired.db)
    assert row["cost_usd"] == pytest.approx((1000 * 2.5 + 500 * 10.0) / 1e6)


def test_disable_billing_setting_skips_rows(wired):
    wired.db.set_setting("disable_billing", "true")
    wired.fake.script.append(OK)
    assert wired.post().status == 200
    assert rows(wired.db) == []


def test_monthly_cap_429_before_upstream(wired):
    wired.db.set_setting("monthly_cap_usd", "1.5")
    wired.db.add_billing("m", 1, 1, 1.5)  # exactly at the cap
    wired.fake.script.append(OK)
    r = wired.post()
    assert r.status == 429
    assert r.json() == {"type": "error", "error": {"type": "rate_limit_error",
                                                  "message": "Monthly spending cap of 1.50 USD reached"}}
    assert wired.fake.calls == []  # nothing sent upstream
    refused = rows(wired.db)[1:]  # the refusal is one billing row: tokens 0, cost 0, the error type in the text
    assert len(rows(wired.db)) == 2 and refused[0]["tokens_in"] == 0 and refused[0]["cost_usd"] == 0.0
    assert refused[0]["error"].startswith("rate_limit_error")
    wired.db.set_setting("monthly_cap_usd", "2")  # edited by the CLI: picked up at once (ttl 0)
    assert wired.post().status == 200
    assert len(wired.fake.calls) == 1


def test_no_cap_setting_means_no_limit_and_bad_value_is_ignored(wired):
    wired.db.add_billing("m", 1, 1, 9999.0)
    wired.db.set_setting("monthly_cap_usd", "not-a-number")
    wired.fake.script.append(OK)
    assert wired.post().status == 200


def test_gate_error_fails_open_and_gate_message_is_429():
    def boom():
        raise RuntimeError("db down")
    fake = FakeHttp()
    fake.script.append(OK)
    app = lite.build_app(lambda m: ("fake", "x"), "mk", http=fake, secret=lambda n: "k", gate=boom,
                         providers={"fake": {"base_url": "https://llm.example.org/v1", "kind": "openai",
                                             "key_names": ["K"]}})
    assert app.gateway.gate is boom
    app.gateway._check_gate()  # does not raise
    app.gateway.gate = lambda: "stop"
    with pytest.raises(server.HTTPError) as ei:
        app.gateway._check_gate()
    assert ei.value.status == 429 and ei.value.error_type == "rate_limit_error"


@pytest.mark.parametrize("name", [
    "gsk_" + "aB3dE5gH7j" * 5 + "xY", "AIza" + "Sy" + "b" * 33, "nvapi-" + "x1Y2z3" * 8, "sk-proj-" + "aB3_dE5-gH7j" * 3,
    "hf_" + "aB3dE5gH7j" * 3 + "xYzW", "ghp_" + "aB3dE5gH7j" * 3 + "xYzWqR", "AKIA" + "ABCDEFGHIJKLMNOP",
])
def test_real_key_formats_are_values(name):
    from kiko_lite.resolver import key_name_problem
    assert key_name_problem(name) == "value"


@pytest.mark.parametrize("name", [
    "hf_token", "sk_api_key", "gsk_name", "ghp_x", "GROQ_API_KEY", "gemini_api_key", "sk_my_long_api_key_name",
])
def test_prefix_like_names_are_names(name):
    from kiko_lite.resolver import key_name_problem
    assert key_name_problem(name) is None


# ---- private folder and file (POSIX) ----

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_new_db_folder_is_0700_and_files_are_0600(tmp_path):
    folder = tmp_path / "kiko-data"
    d = kdb.Database(folder / "kiko.db")
    d.upsert_model("m", "gemini/g", "K")
    d.add_billing("m", 1, 1, 0.0)  # WAL / SHM exist now
    assert (folder.stat().st_mode & 0o777) == 0o700
    files = sorted(folder.iterdir())
    assert [f.name for f in files if f.name == "kiko.db"] == ["kiko.db"]
    for f in files:
        assert (f.stat().st_mode & 0o777) == 0o600, f.name


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_existing_folder_is_left_alone_and_an_old_db_file_is_tightened(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    path = folder / "kiko.db"
    path.write_bytes(b"")
    path.chmod(0o644)
    kdb.Database(path)
    assert (folder.stat().st_mode & 0o777) == 0o755  # not ours to change
    assert (path.stat().st_mode & 0o777) == 0o600


def test_db_creation_works_on_every_platform(tmp_path):
    d = kdb.Database(tmp_path / "a" / "b" / "kiko.db")  # permission handling must never break creation
    assert d.schema_version() == kdb.SCHEMA_VERSION


def test_v5_db_with_model_rows_migrates_to_current_keeping_rows(tmp_path):
    p = tmp_path / "v5.db"
    d = kdb.Database(p)
    d.upsert_model("keep", "acme/x", "KEY_X", api_base="http://127.0.0.1:1/v1", model_class="HAIKU", priority=3,
                   thinking="off")
    d.upsert_model("two", "acme/y", "KEY_Y")
    con = sqlite3.connect(p)
    con.execute("ALTER TABLE models DROP COLUMN thinking")  # the v5 shape: no thinking column
    con.execute("PRAGMA user_version=5")
    con.commit()
    con.close()
    assert "thinking" not in cols(p, "models")
    d2 = kdb.Database(p)
    assert d2.schema_version() == kdb.SCHEMA_VERSION and "thinking" in cols(p, "models")
    rows = {m["alias"]: m for m in d2.models()}
    assert set(rows) == {"keep", "two"} and rows["keep"]["priority"] == 3 and rows["keep"]["model_class"] == "HAIKU"
    assert rows["keep"]["thinking"] is None and rows["two"]["thinking"] is None


def test_indexes_are_recreated_on_every_open(tmp_path):
    p = tmp_path / "idx.db"
    kdb.Database(p)
    con = sqlite3.connect(p)
    con.execute("DROP INDEX billing_request_id")
    con.execute("DROP INDEX responses_created_at")
    con.commit()
    con.close()
    kdb.Database(p)  # already at the current version: the indexes still come back
    con = sqlite3.connect(p)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    con.close()
    assert {"billing_request_id", "responses_created_at"} <= names


def test_list_models_only_resolvable_ids(db):
    add(db, "gem", "gemini/gemini-3.8-flash", "SONNET", 10, "ok")
    add(db, "off", "groq/o", "HAIKU", 10, "ok", enabled=False)
    add(db, "bad", "openai/m", api_base="http://evil.example.org/v1", cls="OPUS", status="ok")
    db.set_alias("my-*", "gem")  # wildcard alias: not listed
    db.set_alias("exact", "gem")
    db.set_alias("dead", "off")  # alias to a disabled model: not listed
    r, _, _ = make(db)
    ids = r.list_models()
    assert "gem" in ids and "exact" in ids and "sonnet" in ids
    assert not {"off", "bad", "my-*", "dead", "haiku", "opus"} & set(ids)
    assert len(ids) == len(set(ids)) and all(r(i) is not None for i in ids)
