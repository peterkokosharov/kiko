# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The providers table (schema v4): seeding, the v3 -> v4 migration (also two processes at once), the Database
methods and the CLI commands. Offline, temp DB only."""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kiko_lite import db as kdb
from kiko_lite import providers as ptable
from .test_kiko_lite_cli import run, store  # noqa: F401  (fixtures: temp DB + fake keystore)
from .test_kiko_lite_planned_classes import _v2_db

SEEDED = {"gemini": ("https://generativelanguage.googleapis.com", "GEMINI_API_KEY"),
          "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
          "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
          "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
          "dashscope": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
          "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY")}


def _v3_db(path):
    """A DB as schema v3 left it: classes seeded, no providers table, one model row."""
    _v2_db(path)
    con = sqlite3.connect(path)
    con.execute("DELETE FROM models")
    con.execute("INSERT INTO model_classes(name, keyword, role, sort) VALUES('VOICE','voice','tts',50)")
    con.execute("INSERT INTO models(alias, provider, api_key_env) VALUES('gf','gemini/gemini-3.8-flash','GEMINI_API_KEY')")
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()


def test_seeded_providers_match_what_the_resolver_uses(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    got = {p["name"]: (p["api_base"], p["api_key_env"]) for p in d.list_providers()}
    assert got == SEEDED and d.schema_version() == kdb.SCHEMA_VERSION
    for name, (base, key) in SEEDED.items():  # the same host and key name the gateway table has
        entry = ptable.PROVIDERS[name]
        assert entry["base_url"] == base and key in entry["key_names"]
    assert [p["name"] for p in d.list_providers()][:2] == ["gemini", "nvidia"]


def test_v3_db_migrates_to_v4_and_keeps_rows(tmp_path):
    p = tmp_path / "kiko.db"
    _v3_db(p)
    d = kdb.Database(p)
    assert d.schema_version() == kdb.SCHEMA_VERSION
    assert {x["name"] for x in d.list_providers()} == set(SEEDED)
    assert [m["alias"] for m in d.models()] == ["gf"] and d.get_provider("gemini")["models"] == 1
    d.delete_provider("mistral")
    assert "mistral" not in {x["name"] for x in kdb.Database(p).list_providers()}  # not re-added


def test_owner_provider_is_not_overwritten_by_the_seed(tmp_path):
    p = tmp_path / "kiko.db"
    _v3_db(p)
    con = sqlite3.connect(p)
    con.executescript(kdb._V4)
    con.execute("INSERT INTO providers(name, api_base, api_key_env, sort) VALUES('nvidia','https://mine/v1','MY_KEY',1)")
    con.commit()
    con.close()
    assert kdb.Database(p).get_provider("nvidia")["api_base"] == "https://mine/v1"


_OPEN = ("import sys, time\n"
         "from kiko_lite import db\n"
         "time.sleep(max(0.0, float(sys.argv[2]) - time.time()))\n"
         "d = db.Database(sys.argv[1], busy_timeout=30.0)\n"
         "print(d.schema_version())\n")


def test_two_processes_open_v3_db_at_once(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for i in range(5):
        p = tmp_path / f"race{i}.db"
        _v3_db(p)
        at = str(time.time() + 2.0)
        procs = [subprocess.Popen([sys.executable, "-c", _OPEN, str(p), at], cwd=root, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for pr in procs:
            out, err = pr.communicate(timeout=120)
            assert pr.returncode == 0, err
            assert out.strip() == str(kdb.SCHEMA_VERSION)
        con = sqlite3.connect(p)
        names = [r[0] for r in con.execute("SELECT name FROM providers")]
        version = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()
        assert version == kdb.SCHEMA_VERSION and sorted(names) == sorted(SEEDED)


def test_readonly_v3_db_reads_the_defaults(tmp_path):
    p = tmp_path / "kiko.db"
    _v3_db(p)
    ro = kdb.Database(p, readonly=True)
    assert ro.schema_version() == 3 and {x["name"] for x in ro.list_providers()} == set(SEEDED)


def test_set_get_delete_provider(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    assert d.set_provider("acme", "https://a.example/v1", "ACME_KEY") is True
    assert d.set_provider("acme", "https://b.example/v1", "ACME_KEY2") is False
    p = d.get_provider("acme")
    assert (p["api_base"], p["api_key_env"]) == ("https://b.example/v1", "ACME_KEY2")
    assert d.list_providers()[-1]["name"] == "acme"
    for bad in ("", "Acme", "1x", "a b", "x" * 40):
        with pytest.raises(ValueError):
            d.set_provider(bad, "https://a.example/v1", "K")
    with pytest.raises(ValueError):
        d.set_provider("ok", "", "K")
    with pytest.raises(ValueError):
        d.set_provider("ok", "https://a.example/v1", " ")
    for base in ("http://a.example/v1", "https://u:p@a.example/v1", "a.example", "https://a.example/v1?x=1"):
        with pytest.raises(ValueError):
            d.set_provider("ok", base, "K")
    with pytest.raises(ValueError):
        d.set_provider("ok", "https://a.example/v1", "sk-" + "a1B2" * 10)  # a key value, not a name
    assert d.get_provider("ok") is None
    assert d.delete_provider("acme") is True and d.delete_provider("acme") is False


def test_delete_provider_in_use_is_refused(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("n", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "NVIDIA_API_KEY")
    with pytest.raises(ValueError, match="still used"):
        d.delete_provider("nvidia")
    assert d.get_provider("nvidia")["models"] == 1
    d.delete_model("n")
    assert d.delete_provider("nvidia") is True


def test_cli_provider_commands(run):
    kdb.Database(run.path)
    code, out, _ = run("list-providers")
    assert code == 0 and all(n in out for n in SEEDED) and "NVIDIA_API_KEY" in out
    code, out, _ = run("add-provider", "acme", "--api-base", "https://a.example/v1", "--api-key-env", "ACME_KEY")
    assert code == 0 and "added provider 'acme'" in out
    assert "acme" in run("list-providers")[1]
    for args in (("acme", "--api-base", "http://a.example/v1", "--api-key-env", "K"),
                 ("acme", "--api-base", "https://a.example/v1", "--api-key-env", "has space"),
                 ("Bad", "--api-base", "https://a.example/v1", "--api-key-env", "K")):
        assert run("add-provider", *args)[0] != 0, args
    run("add-model", "m", "--provider", "acme/x", "--api-base", "https://a.example/v1", "--api-key-env", "ACME_KEY")
    code, _, err = run("delete-provider", "acme")
    assert code != 0 and "still used" in err
    run("delete-model", "m")
    assert run("delete-provider", "acme")[0] == 0
    assert run("delete-provider", "acme")[0] == 1


def test_cli_add_model_validates_api_base_for_every_provider(run):
    kdb.Database(run.path)
    for prov in ("nvidia/x", "gemini/gemini-3.8-flash", "openai/gpt-4o"):
        for url in ("http://evil.example/v1", "https://user:pass@host.example/v1"):
            code, _, _ = run("add-model", "m", "--provider", prov, "--api-key-env", "K", "--api-base", url)
            assert code == 2, (prov, url)
    assert kdb.Database(run.path).get_model("m") is None


def test_custom_provider_row_sends_the_bare_model_id(tmp_path, monkeypatch):
    """acme/m1 with acme in the providers table: the upstream gets "m1" (end to end through the proxy)."""
    import json
    from kiko_lite.resolver import Catalog, DBResolver, build_db_app, map_row, custom_provider_names
    from kikolib import http as khttp
    from kikolib import retry, server
    from .test_kiko_lite_failover import Fake, ok
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    fake = Fake()
    d = kdb.Database(tmp_path / "k.db")
    d.set_provider("acme", fake.base, "ACME_KEY")
    d.upsert_model("am", "acme/m1", "ACME_KEY", api_base=fake.base, model_class="HAIKU")
    d.upsert_model("legacy", "other/x", "ACME_KEY", api_base=fake.base, model_class="OPUS")  # not in the table
    row = d.get_model("am")
    assert map_row(row, custom_provider_names(d)).model_id == "m1"
    assert map_row(d.get_model("legacy"), custom_provider_names(d)).model_id == "other/x"  # old rule kept
    fake.add(**ok("hi"))
    policy = retry.RetryPolicy(attempts=1, base_delay=0.01, max_delay=0.02, budget=5.0, jitter=False)
    app = build_db_app(d, "mk", secret={"ACME_KEY": "UP-SECRET"}.get, env={}, ttl=0.0, policy=policy,
                       ping_interval=0.2, upstream_timeout=2.0)
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()
    try:
        payload = {"model": "claude-haiku-9", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
        r = client.request("POST", srv.url + "/v1/messages", {"x-api-key": "mk", "content-type": "application/json"},
                           json.dumps(payload).encode())
        assert r.status == 200 and fake.requests[0]["model"] == "m1"
    finally:
        client.close()
        srv.stop()
        fake.server.stop()


# --- TASK #125: one fixed-provider list ---

def test_resolver_prefixes_come_from_the_provider_table():
    from kiko_lite import resolver
    assert resolver._PREFIX == ptable.prefix_table()
    assert set(resolver._PREFIX.values()) == set(ptable.PROVIDERS)  # every alias points at a real provider
    assert set(ptable.PROVIDERS) <= set(resolver._PREFIX) and set(ptable.ALIASES) <= set(resolver._PREFIX)
    assert {"google", "qwen", "nvidia_nim"} <= set(resolver._PREFIX)
    assert ptable.lookup("nvidia_nim")[0] == "nvidia"
    assert set(ptable.fixed_base_names()) == set(ptable.PROVIDERS) - {"openai"}
    hint = ptable.fixed_base_hint()
    assert all(n in hint for n in ptable.fixed_base_names()) and "openai" not in hint


def test_cli_add_model_refuses_base_for_fixed_providers(run):
    for prov in ("gemini/g", "google/g", "nvidia_nim/m", "QWEN/m", "mistral/m"):
        code, out, err = run("add-model", "x", "--provider", prov, "--api-base", "https://ok.example/v1",
                             "--api-key-env", "K")
        assert code == 2 and "fixes the API base" in err and "Traceback" not in err, prov
    assert not run.path.exists()
    code, _, err = run("add-model", "o", "--provider", "openai/m", "--api-base", "https://ok.example/v1",
                       "--api-key-env", "K")
    assert code == 0, err
    assert run("add-model", "g", "--provider", "gemini/g", "--api-key-env", "K")[0] == 0  # no override: fine
