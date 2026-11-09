# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite.cli: offline; temp DB (--db) and a fake keystore only, never ~/.kiko or the real OS store."""
import subprocess
import sys
from pathlib import Path

import pytest

from kiko_lite import cli
from kiko_lite import db as kdb
from kikolib import factory, keystore
from kikolib import server as kserver

ROOT = Path(__file__).resolve().parents[2]
SECRET = "SUPER-SECRET-VALUE-9f3a"


class FakeStore:
    def __init__(self):
        self.d = {}

    def get(self, name):
        return self.d.get(name.upper())

    def set(self, name, value):
        self.d[name.upper()] = value
        return True

    def delete(self, name):
        return self.d.pop(name.upper(), None) is not None


@pytest.fixture
def store(monkeypatch, tmp_path):
    s = FakeStore()
    factory.register("keystore", "fake", probe=lambda: (True, "test backend"), load=lambda: s, priority=1)
    monkeypatch.setenv("KIKO_KEYSTORE_IMPL", "fake")
    # isolation: the default DB path (env, and ~ via HOME / USERPROFILE) points into tmp, never ~/.kiko
    monkeypatch.setenv("KIKO_DB_PATH", str(tmp_path / "isolated-default" / "kiko.db"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("MASTER_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY", "MY_KEY", "CWD_ONLY_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env
    yield s
    factory.register("keystore", "fake", probe=lambda: (False, "test is over"), load=lambda: s, priority=1)


@pytest.fixture
def run(store, tmp_path, capsys):
    path = tmp_path / "data" / "kiko.db"

    def go(*argv, db=True):
        args = list(argv) + (["--db", str(path)] if db else [])
        code = cli.main(args)
        out = capsys.readouterr()
        return code, out.out, out.err

    go.path = path
    go.store = store
    return go


def copy_yaml(name, tmp_path):
    if not (ROOT / name).is_file():
        pytest.skip(f"{name} is a repo-root sample file, not shipped in the kiko-lite zip")
    dst = tmp_path / f"{name}.copy"
    dst.write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def db_bytes(path):
    return b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*"))


# ---- import ----

def test_import_kiko_config_yaml(run, tmp_path):
    code, out, _ = run("import", str(copy_yaml("kiko-config.yaml", tmp_path)))
    assert code == 0, out
    d = kdb.Database(run.path)
    models = {m["alias"]: m for m in d.models()}
    assert set(models) == {"gemini-flash", "gemini-flash-lite", "nemotron-super", "nemotron-ultra"}
    assert models["gemini-flash"]["provider"] == "gemini/gemini-3.8-flash"
    assert models["gemini-flash"]["api_key_env"] == "GEMINI_API_KEY"
    assert models["gemini-flash"]["price_in_per_mtok"] == 0.75 and models["gemini-flash"]["price_out_per_mtok"] == 3.75
    assert models["nemotron-super"]["api_base"] == "https://integrate.api.nvidia.com/v1"
    assert models["nemotron-super"]["price_in_per_mtok"] is None  # no NVIDIA NIM price in the JSON
    assert "ignored: litellm_settings" in out and "can be deleted" in out
    assert run.store.d == {}  # os.environ/ references: nothing to store


def test_import_kiko_sample_yaml_and_rerun_is_idempotent(run, tmp_path):
    y = copy_yaml("kiko-sample.yaml", tmp_path)
    code, out, _ = run("import", str(y))
    assert code == 0, out
    d = kdb.Database(run.path)
    assert len(d.models()) == 5 and d.settings() == {"disable_billing": "false"}
    d.set_enabled("gemini-pro", False)
    d.upsert_model("gemini-pro", "gemini/gemini-1.5-pro", "GEMINI_API_KEY")  # status/enabled survive a re-import
    code, out2, _ = run("import", str(y))
    assert code == 0 and "updated model gemini-pro" in out2 and "added" not in out2
    assert len(d.models()) == 5


def test_import_literal_key_goes_to_keystore_not_db(run, tmp_path):
    y = tmp_path / "lit.yaml"
    y.write_text(
        "model_list:\n"
        "  - model_name: my-model.1\n"
        "    litellm_params:\n"
        "      model: gemini/gemini-3.8-flash\n"
        f"      api_key: {SECRET}\n"
        "model_aliases:\n"
        "  'claude-sonnet-*': my-model.1\n"
        "general_settings:\n"
        "  monthly_cap_usd: 12.5\n"
        "  billing_db: C:/x/y.db\n"
        "  weird: 1\n"
        "other_top: 1\n", encoding="utf-8")
    code, out, err = run("import", str(y))
    assert code == 0, out
    assert run.store.d == {"KIKO_MY_MODEL_1_1_API_KEY": SECRET}
    m = kdb.Database(run.path).get_model("my-model.1")
    assert m["api_key_env"] == "KIKO_MY_MODEL_1_1_API_KEY"
    assert SECRET not in out + err
    assert SECRET.encode() not in db_bytes(run.path)
    assert "stored key for my-model.1 in fake" in out
    d = kdb.Database(run.path)
    assert d.aliases() == [("claude-sonnet-*", "my-model.1")]
    assert d.settings() == {"monthly_cap_usd": "12.5"}
    for item in ("general_settings.billing_db", "general_settings.weird", "other_top"):
        assert f"ignored: {item}" in out


def _literal_yaml(tmp_path):
    y = tmp_path / "orph.yaml"
    y.write_text("model_list:\n  - model_name: orph\n    litellm_params:\n      model: gemini/gemini-3.8-flash\n"
                 f"      api_key: {SECRET}\n", encoding="utf-8")
    return y


def _boom(self, *a, **k):
    raise ValueError("boom")


def test_import_failed_upsert_removes_orphan_key(run, tmp_path, monkeypatch):
    monkeypatch.setattr(kdb.Database, "upsert_model", _boom)
    code, out, err = run("import", str(_literal_yaml(tmp_path)))
    assert code == 1 and "boom" in out
    assert run.store.d == {}
    assert SECRET not in out + err


def test_import_failed_upsert_reports_orphan_name_when_delete_fails(run, tmp_path, monkeypatch):
    monkeypatch.setattr(kdb.Database, "upsert_model", _boom)
    monkeypatch.setattr(run.store, "delete", lambda n: False)
    code, out, err = run("import", str(_literal_yaml(tmp_path)))
    assert code == 1 and "orphaned keystore entry KIKO_ORPH_1_API_KEY" in out
    assert SECRET not in out + err


def test_import_failed_upsert_keeps_entry_that_existed(run, tmp_path, monkeypatch):
    monkeypatch.setattr(kdb.Database, "upsert_model", _boom)
    run.store.d["KIKO_ORPH_1_API_KEY"] = "older"
    code, out, err = run("import", str(_literal_yaml(tmp_path)))
    assert code == 1 and "KIKO_ORPH_1_API_KEY" in run.store.d and "orphaned" not in out


def test_import_dry_run_writes_nothing(run, tmp_path):
    y = tmp_path / "lit.yaml"
    y.write_text(f"model_list:\n  - model_name: a\n    litellm_params:\n      model: groq/x\n"
                 f"      api_key: {SECRET}\n", encoding="utf-8")
    code, out, err = run("import", str(y), "--dry-run")
    assert code == 0 and "would import model a" in out
    assert not run.path.exists() and run.store.d == {}
    assert SECRET not in out + err


def test_import_reports_problems(run, tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text("model_list:\n  - model_name: a\n    litellm_params:\n      model: openai/m\n"
                 "      api_base: http://evil.example.org/v1\n      api_key: os.environ/K\n"
                 "  - model_name: b\n    litellm_params:\n      model: openai/m\n"
                 "      api_base: https://llm.example.org/v1\n", encoding="utf-8")
    code, out, _ = run("import", str(y))
    assert code == 1 and "problem" in out
    assert kdb.Database(run.path).models() == []
    assert run("import", str(tmp_path / "missing.yaml"))[0] == 2


# ---- model commands ----

def test_add_list_enable_disable_delete(run):
    code, out, _ = run("add-model", "flash", "--provider", "gemini/gemini-3.8-flash", "--api-key-env", "MY_KEY",
                       "--class", "haiku", "--priority", "3")
    assert code == 0 and "added model 'flash'" in out and "warning" not in out
    m = kdb.Database(run.path).get_model("flash")
    assert (m["model_class"], m["priority"], m["price_in_per_mtok"]) == ("HAIKU", 3, 0.75)
    assert "updated" in run("add-model", "flash", "--provider", "gemini/gemini-3.8-flash", "--api-key-env", "MY_KEY",
                            "--price-in", "1.5", "--price-out", "2")[1]
    assert kdb.Database(run.path).get_model("flash")["price_in_per_mtok"] == 1.5
    code, out, _ = run("add-model", "nim", "--provider", "openai/nvidia/x", "--api-base",
                       "https://integrate.api.nvidia.com/v1", "--api-key-env", "NVIDIA_API_KEY")
    assert code == 0 and "no price known" in out
    out = run("list-models")[1]
    assert "flash" in out and "gemini/gemini-3.8-flash" in out and "nvidia/nvidia/x" in out and "key=MY_KEY" in out
    assert run("disable-model", "flash")[1].strip() == "kiko: disabled 'flash'"
    assert "OFF" in run("list-models")[1]
    assert "enabled 'flash'" in run("enable-model", "flash")[1]
    assert run("delete-model", "flash")[0] == 0
    code, out, _ = run("delete-model", "flash")
    assert code == 1 and "not found" in out
    assert run("enable-model", "nope")[0] == 1


def test_add_model_rejects_key_value_and_bad_base(run):
    code, out, err = run("add-model", "x", "--provider", "gemini/g", "--api-key-env", SECRET)
    assert code == 2 and SECRET not in out + err
    code, _, err = run("add-model", "x", "--provider", "openai/m", "--api-base", "http://evil.example.org",
                       "--api-key-env", "K")
    assert code == 2 and "https" in err
    assert not run.path.exists()


def test_alias_and_setting_commands(run):
    assert run("add-alias", "claude-*", "flash")[0] == 0
    assert run("set-setting", "monthly_cap_usd", "20")[0] == 0
    assert run("set-setting", "monthly_cap_usd", "lots")[0] == 2
    assert run("set-setting", "nope", "1")[0] == 2
    out = run("list-models")[1]
    assert "alias claude-* -> flash" in out and "setting monthly_cap_usd = 20" in out
    assert run("delete-alias", "claude-*")[0] == 0 and run("unset-setting", "monthly_cap_usd")[0] == 0
    assert run("delete-alias", "claude-*")[0] == 1 and run("unset-setting", "monthly_cap_usd")[0] == 1


def test_read_commands_do_not_create_the_db(run):
    assert "no database" in run("list-models")[1]
    assert "no database" in run("costs")[1]
    assert run("disable-model", "x")[0] == 1
    assert not run.path.exists()


def test_costs(run):
    d = kdb.Database(run.path)
    d.add_billing("m1", 100, 50, 0.5)
    d.add_billing("m1", 10, 5, 0.0, error="api_error")
    code, out, _ = run("costs", "--days", "7")
    assert code == 0 and "m1" in out and "0.500000" in out and "TOTAL" in out


# ---- doctor / serve ----

def test_doctor_reports_without_secrets(run, monkeypatch, tmp_path):
    monkeypatch.setenv("MY_KEY", SECRET)
    run.store.d["GEMINI_API_KEY"] = SECRET + "-store"
    run.store.d["MASTER_KEY"] = SECRET + "-master"
    run.path.parent.mkdir(parents=True, exist_ok=True)
    (run.path.parent / ".env").write_text(f"NVIDIA_API_KEY={SECRET}-dotenv\n", encoding="utf-8")  # next to the DB
    (tmp_path / ".env").write_text(f"CWD_ONLY_KEY={SECRET}-cwd\n", encoding="utf-8")  # cwd: must be ignored
    run("add-model", "cwd-model", "--provider", "groq/y", "--api-key-env", "CWD_ONLY_KEY")
    run("add-model", "env-model", "--provider", "gemini/gemini-3.8-flash", "--api-key-env", "MY_KEY")
    run("add-model", "store-model", "--provider", "gemini/gemini-3.5-flash-lite", "--api-key-env", "GEMINI_API_KEY")
    run("add-model", "dot-model", "--provider", "nvidia_nim/x", "--api-key-env", "NVIDIA_API_KEY")
    run("add-model", "missing", "--provider", "groq/x", "--api-key-env", "NOPE_KEY")
    kdb.Database(run.path).upsert_model("leaky", "gemini/g", "sk-" + SECRET)  # a key value in the name column
    code, out, err = run("doctor")
    assert code == 0
    assert SECRET not in out + err
    assert "Python 3." in out and "keystore" in out and "fake" in out
    assert "Master key: found in store" in out
    assert "key MY_KEY: found in env" in out and "key GEMINI_API_KEY: found in store" in out
    assert "key NVIDIA_API_KEY: found in dotenv" in out and "key NOPE_KEY: NOT FOUND" in out
    assert "<invalid name, value hidden>" in out and "kiko fix-key leaky" in out
    assert "key CWD_ONLY_KEY: NOT FOUND" in out  # .env is read only next to the DB
    assert f"schema version {kdb.SCHEMA_VERSION}" in out and "6 model(s)" in out
    assert "price: known" in out and "UNKNOWN" in out


def test_doctor_without_db_or_master_key(run):
    code, out, _ = run("doctor")
    assert code == 0 and "does not exist yet" in out and "Master key: NOT FOUND" in out
    assert not run.path.exists()


def test_serve_refuses_without_master_key(run, monkeypatch):
    called = []
    monkeypatch.setattr(kserver, "serve", lambda *a, **k: called.append(a))
    code, out, err = run("serve")
    assert code == 2 and "master key" in err and called == []
    assert not run.path.exists()


def test_serve_uses_master_key_from_keystore_then_env(run, monkeypatch):
    seen = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: seen.append((app, host, port)))
    run.store.d["MASTER_KEY"] = "from-store"
    assert run("serve", "--port", "4999")[0] == 0
    app, host, port = seen[0]
    assert (host, port) == ("127.0.0.1", 4999) and app.gateway.master == b"from-store"
    monkeypatch.setenv("MASTER_KEY", "from-env")
    run("serve")
    assert seen[1][0].gateway.master == b"from-env"
    assert run.path.exists()


GROQ_KEY = ("gsk_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fG3hJ5kL7nP9rS1tU3vX5y")[:56]  # gsk_ + 52 alnum
GEMINI_KEY = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"[:35]  # AIza + 35 alnum, no '-'
NVIDIA_KEY = "nvapi-" + "x1Y2z3W4v5U6" * 6
KEY_SHAPES = [GROQ_KEY, GEMINI_KEY, NVIDIA_KEY]
ALL_SHAPES_PROVIDER = {GROQ_KEY: "groq/x", GEMINI_KEY: "gemini/x", NVIDIA_KEY: "nvidia_nim/x"}


def assert_no_value(value, *texts):
    for t in texts:
        assert value not in t


@pytest.mark.parametrize("value", KEY_SHAPES)
def test_legacy_key_value_in_column_is_never_printed(run, caplog, value):
    caplog.set_level("DEBUG")
    kdb.Database(run.path).upsert_model("legacy", ALL_SHAPES_PROVIDER[value], value)
    outs = []
    for cmd in (["list-models"], ["doctor"], ["costs"], ["disable-model", "legacy"], ["enable-model", "legacy"]):
        code, out, err = run(*cmd)
        assert code == 0, (cmd, out, err)
        outs += [out, err]
    assert_no_value(value, *outs, caplog.text)
    assert "<invalid name, value hidden>" in run("list-models")[1]
    assert "run `kiko fix-key legacy`" in run("doctor")[1]
    # the app does not look the value up as a key name either
    from kiko_lite.resolver import Catalog, DBProviders
    prov = DBProviders(Catalog(kdb.Database(run.path), 0.0))
    assert prov["db/legacy"]["key_names"] == []


@pytest.mark.parametrize("value", KEY_SHAPES)
def test_import_flat_literal_goes_to_keystore(run, tmp_path, caplog, value):
    caplog.set_level("DEBUG")
    y = tmp_path / "flat.yaml"
    y.write_text(f"model_list:\n  - model_name: flat\n    provider: {ALL_SHAPES_PROVIDER[value]}\n"
                 f"    api_key_env: {value}\n", encoding="utf-8")
    code, out, err = run("import", str(y), "--dry-run")
    assert code == 0 and not run.path.exists() and run.store.d == {}
    assert_no_value(value, out, err)
    code, out, err = run("import", str(y))
    assert code == 0, out
    assert run.store.d == {"KIKO_FLAT_1_API_KEY": value}
    assert kdb.Database(run.path).get_model("flat")["api_key_env"] == "KIKO_FLAT_1_API_KEY"
    assert_no_value(value, out, err, caplog.text)
    assert value.encode() not in db_bytes(run.path)


def test_import_flat_real_name_is_kept_as_name(run, tmp_path):
    y = tmp_path / "flat.yaml"
    y.write_text("model_list:\n  - model_name: flat\n    provider: groq/x\n    api_key_env: MY_GROQ_KEY\n",
                 encoding="utf-8")
    assert run("import", str(y))[0] == 0
    assert kdb.Database(run.path).get_model("flat")["api_key_env"] == "MY_GROQ_KEY" and run.store.d == {}


@pytest.mark.parametrize("value", KEY_SHAPES)
def test_fix_key_moves_value_to_keystore(run, caplog, value):
    caplog.set_level("DEBUG")
    d = kdb.Database(run.path)
    d.upsert_model("my.legacy", ALL_SHAPES_PROVIDER[value], value)
    code, out, err = run("fix-key", "my.legacy")
    assert code == 0, (out, err)
    assert run.store.d == {"KIKO_MY_LEGACY_1_API_KEY": value}
    assert d.get_model("my.legacy")["api_key_env"] == "KIKO_MY_LEGACY_1_API_KEY"
    assert "KIKO_MY_LEGACY_1_API_KEY" in out and "fake" in out
    assert_no_value(value, out, err, caplog.text)
    assert value.encode() not in db_bytes(run.path)  # wiped from the file, not just from the row
    code, out, err = run("doctor")
    assert "fix-key" not in out and "key KIKO_MY_LEGACY_1_API_KEY: found in store" in out
    assert "already uses the key name" in run("fix-key", "my.legacy")[1]
    assert run("fix-key", "nope")[0] == 1


def test_fix_key_keeps_row_when_keystore_fails(run, monkeypatch):
    kdb.Database(run.path).upsert_model("x", "groq/x", GROQ_KEY)
    monkeypatch.setattr(keystore, "set_secret", lambda n, v: False)
    code, out, err = run("fix-key", "x")
    assert code == 2 and GROQ_KEY not in out + err
    assert kdb.Database(run.path).get_model("x")["api_key_env"] == GROQ_KEY


def test_fix_key_long_alias_twice_never_stores_a_name_as_the_key(run):
    d = kdb.Database(run.path)
    d.upsert_model("nemotronultra253bv1xyz", "groq/x", GROQ_KEY)
    code, out, err = run("fix-key", "nemotronultra253bv1xyz")
    assert code == 0, (out, err)
    # KIKO_NEMOTRONULTRA253BV1XYZ_1_API_KEY would look like a token: the short fallback name is used
    assert run.store.d == {"KIKO_MODEL_1_API_KEY": GROQ_KEY}
    assert d.get_model("nemotronultra253bv1xyz")["api_key_env"] == "KIKO_MODEL_1_API_KEY"
    code, out, err = run("fix-key", "nemotronultra253bv1xyz")
    assert code == 0 and "nothing to fix" in out
    assert run.store.d == {"KIKO_MODEL_1_API_KEY": GROQ_KEY}  # unchanged: not overwritten with its own name
    assert_no_value(GROQ_KEY, out, err)


def test_generated_key_names_are_valid_unique_and_avoid_taken():
    from kiko_lite.cli import generated_key_name
    from kiko_lite.resolver import valid_key_name as ok
    assert generated_key_name("a.b", 1) == "KIKO_A_B_1_API_KEY" != generated_key_name("a-b", 2)
    assert generated_key_name("a.b", 1, {"KIKO_A_B_1_API_KEY"}) == "KIKO_MODEL_1_API_KEY"
    assert generated_key_name("a.b", 1, {"KIKO_A_B_1_API_KEY", "KIKO_MODEL_1_API_KEY"}) == "KIKO_MODEL_1_1_API_KEY"
    for alias in ("---", "x" * 200, "nemotronultra253bv1xyz", "é.ü", "9"):
        assert ok(generated_key_name(alias, 77))


def test_import_literal_keys_of_a_dot_b_and_a_dash_b_do_not_collide(run, tmp_path):
    y = tmp_path / "ab.yaml"
    y.write_text("model_list:\n"
                 "  - model_name: a.b\n    litellm_params:\n      model: groq/x\n      api_key: FIRST-LITERAL-1\n"
                 "  - model_name: a-b\n    litellm_params:\n      model: groq/x\n      api_key: SECOND-LITERAL-2\n",
                 encoding="utf-8")
    code, out, _ = run("import", str(y))
    assert code == 0, out
    assert run.store.d == {"KIKO_A_B_1_API_KEY": "FIRST-LITERAL-1", "KIKO_A_B_2_API_KEY": "SECOND-LITERAL-2"}
    run("import", str(y))  # idempotent: same names, same values
    assert run.store.d == {"KIKO_A_B_1_API_KEY": "FIRST-LITERAL-1", "KIKO_A_B_2_API_KEY": "SECOND-LITERAL-2"}


def test_lower_case_key_name_is_a_name_end_to_end(run, monkeypatch):
    from kiko_lite.resolver import Catalog, DBProviders, DBResolver, build_db_app
    d = kdb.Database(run.path)
    d.upsert_model("lc", "gemini/gemini-3.8-flash", "gemini_api_key")
    cat = Catalog(d, 0.0)
    assert DBResolver(cat, {})("lc") == ("db/lc", "gemini-3.8-flash")
    entry = DBProviders(cat)["db/lc"]
    assert entry["key_names"] == ["gemini_api_key"]
    app = build_db_app(d, "mk", secret={"gemini_api_key": "the-key-value"}.get, env={})
    assert app.gateway._api_key(entry) == "the-key-value"
    run.store.d["GEMINI_API_KEY"] = "stored-value"
    code, out, _ = run("doctor")
    assert "key gemini_api_key: found in store" in out and "fix-key" not in out and HIDDEN_TXT not in out
    code, out, _ = run("fix-key", "lc")
    assert code == 0 and "nothing to fix" in out and run.store.d == {"GEMINI_API_KEY": "stored-value"}
    assert d.get_model("lc")["api_key_env"] == "gemini_api_key"
    assert "key=gemini_api_key" in run("list-models")[1]


HIDDEN_TXT = "<invalid name, value hidden>"


def test_doctor_words_value_and_not_a_name_differently(run):
    d = kdb.Database(run.path)
    d.upsert_model("val", "groq/x", GROQ_KEY)
    d.upsert_model("nam", "groq/x", "bad name!")
    code, out, err = run("doctor")
    assert "row val holds a key value in api_key_env; run `kiko fix-key val`" in out
    assert "row nam has no valid key NAME" in out and "fix-key does not apply" in out
    assert "add-model nam --provider PROVIDER/ID --api-key-env NAME" in out
    assert "fix-key nam" not in out and out.count(HIDDEN_TXT) == 2
    assert_no_value(GROQ_KEY, out, err)
    assert "bad name!" not in out + err
    code, out, err = run("fix-key", "nam")
    assert code == 2 and "does not apply" in err and "bad name!" not in out + err
    assert d.get_model("nam")["api_key_env"] == "bad name!" and run.store.d == {}


def test_fix_key_blocked_by_reader_is_reported_and_finished_later(run, monkeypatch):
    import sqlite3
    monkeypatch.setattr(kdb, "BUSY_TIMEOUT", 0.2)
    d = kdb.Database(run.path)
    d.upsert_model("old", "groq/x", GROQ_KEY)
    reader = sqlite3.connect(run.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM models").fetchall()
    try:
        code, out, err = run("fix-key", "old")
        assert code == 1 and "Stop kiko" in err and "may stay in the DB file" in err
        assert_no_value(GROQ_KEY, out, err)
        assert d.settings().get("fix_key_pending") == "1"
        assert "may still be in the DB file" in run("doctor")[1]
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    assert run.store.d == {"KIKO_OLD_1_API_KEY": GROQ_KEY}
    code, out, err = run("fix-key", "old")  # idempotent: finishes the wipe
    assert code == 0 and "wiped" in out
    assert "fix_key_pending" not in d.settings()
    assert GROQ_KEY.encode() not in db_bytes(run.path)
    assert run.store.d == {"KIKO_OLD_1_API_KEY": GROQ_KEY}


@pytest.mark.parametrize("wal", [False, True])
def test_look_commands_leave_a_kiko_full_db_untouched(run, wal):
    import os
    import sqlite3
    p = run.path
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        "CREATE TABLE billing (id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, tokens_in INTEGER NOT NULL"
        " DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0, ts TEXT NOT NULL,"
        " route TEXT);"
        "CREATE TABLE models (id INTEGER PRIMARY KEY AUTOINCREMENT, alias TEXT UNIQUE NOT NULL, provider TEXT NOT NULL,"
        " api_base TEXT, api_key_env TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT"
        " 'unknown', last_check TEXT, model_class TEXT NOT NULL DEFAULT 'SONNET', priority INTEGER NOT NULL DEFAULT"
        " 100);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY, input_json TEXT NOT NULL, output_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "INSERT INTO models(alias, provider, api_key_env) VALUES('old','gemini/g','GEMINI_API_KEY');"
        "INSERT INTO billing(model, cost_usd, ts) VALUES('m', 1.0, datetime('now'));")
    con.commit()
    con.close()
    before = (p.read_bytes(), os.stat(p).st_mtime_ns)
    for cmd in (["list-models"], ["doctor"], ["costs"]):
        code, out, err = run(*cmd)
        assert code == 0, (cmd, out, err)
        assert "not migrated" in out, (cmd, out)
    assert "old" in run("list-models")[1] and "m" in run("costs")[1]
    assert (p.read_bytes(), os.stat(p).st_mtime_ns) == before
    con = sqlite3.connect(p)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()


def test_case_only_duplicate_alias_rejected_by_add_model(run):
    assert run("add-model", "Flash", "--provider", "gemini/g", "--api-key-env", "K")[0] == 0
    code, out, err = run("add-model", "flash", "--provider", "gemini/g", "--api-key-env", "K")
    assert code == 2 and "only in case" in err


def test_python_dash_m_smoke(tmp_path):
    p = tmp_path / "k.db"
    r = subprocess.run([sys.executable, "-m", "kiko_lite", "list-models", "--db", str(p)], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "no database" in r.stdout and not p.exists()


# ---- costs: probes on their own line ----

def test_costs_lists_probes_separately(run):
    d = kdb.Database(run.path)
    d.add_billing("m1", 100, 50, 0.5)
    d.add_billing("m1", 5, 1, 0.25, route="probe")
    d.add_billing("m2", 5, 1, 0.0, route="probe")
    code, out, _ = run("costs", "--days", "7")
    lines = out.splitlines()
    assert code == 0
    assert any(x.strip().startswith("m1") and " 1 " in x and "0.500000" in x for x in lines)  # the live call only
    assert not any(x.strip().startswith("m2") for x in lines)  # probe-only models are not table rows
    total = next(x for x in lines if x.strip().startswith("TOTAL"))
    assert "0.500000" in total
    probe = next(x for x in lines if "Health probes" in x)
    assert " 2 " in probe and "0.250000" in probe
    assert "0.750000" in next(x for x in lines if "incl. probes" in x)


def test_costs_with_only_probes(run):
    kdb.Database(run.path).add_billing("m1", 5, 1, 0.25, route="probe")
    code, out, _ = run("costs")
    assert code == 0 and "Health probes" in out and "No billing data" not in out


# ---- keystore did not answer ----

def _stuck_keystore(run, monkeypatch):
    def stuck(name):
        monkeypatch.setattr(keystore.subprocess, "run", _timeout)
        keystore._run(["secret-tool", "lookup", name])  # counts a timeout, like a locked wallet would
        return None

    def _timeout(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(run.store, "get", stuck)


def test_doctor_says_keystore_did_not_answer_instead_of_not_found(run, monkeypatch):
    _stuck_keystore(run, monkeypatch)
    run("add-model", "g", "--provider", "gemini/gemini-3.8-flash", "--api-key-env", "GEMINI_API_KEY")
    code, out, _ = run("doctor")
    assert code == 0
    assert "key GEMINI_API_KEY: keystore did not answer (locked? unlock prompt open?)" in out
    assert "NOT FOUND" not in out.split("GEMINI_API_KEY")[1].splitlines()[0]
    assert "Master key: keystore did not answer (locked? unlock prompt open?)" in out


def test_serve_without_master_key_mentions_the_keystore_timeout(run, monkeypatch):
    _stuck_keystore(run, monkeypatch)
    monkeypatch.setattr(kserver, "serve", lambda *a, **k: None)
    code, out, err = run("serve")
    assert code == 2 and "keystore did not answer (locked? unlock prompt open?)" in err


def test_serve_logs_keystore_timeout_for_a_key_and_falls_back_to_dotenv(run, monkeypatch, caplog):
    run.store.d["MASTER_KEY"] = "m"
    seen = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: seen.append(app))
    assert run("serve")[0] == 0
    _stuck_keystore(run, monkeypatch)
    run.path.parent.mkdir(parents=True, exist_ok=True)
    (run.path.parent / ".env").write_text("SLOW_KEY=from-file\n", encoding="utf-8")
    caplog.set_level("WARNING", logger="kiko_lite.cli")
    assert seen[0].gateway._secret("SLOW_KEY") == "from-file"
    assert "key SLOW_KEY: keystore did not answer" in caplog.text


# ---- serve: rss limit flag, .env reload ----

def test_serve_passes_rss_limit(run, monkeypatch):
    kw = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: kw.append(k))
    run.store.d["MASTER_KEY"] = "m"
    assert run("serve")[0] == 0 and kw[-1]["rss_limit_mb"] is None
    assert run("serve", "--rss-limit-mb", "512")[0] == 0 and kw[-1]["rss_limit_mb"] == 512.0


def _bump(path, text):
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    import os
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))  # a coarse clock must not hide the change


def test_serve_reloads_dotenv_but_environment_wins(run, monkeypatch):
    run.store.d["MASTER_KEY"] = "m"
    seen = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: seen.append(app))
    run.path.parent.mkdir(parents=True, exist_ok=True)
    env = run.path.parent / ".env"
    env.write_text("RL_KEY=one\nRL_ENVWINS=file\n", encoding="utf-8")
    assert run("serve")[0] == 0
    gw = seen[0].gateway
    watched = gw._secret.version.__self__
    watched.ttl = 0.0  # re-check at every call (production: every 2 s, like the model snapshot)
    monkeypatch.setenv("RL_ENVWINS", "real")
    entry = {"key_names": ["RL_KEY"]}
    assert gw._secret("RL_KEY") == "one" and gw._api_key(entry) == "one"
    _bump(env, "RL_KEY=two\nRL_ENVWINS=file2\nRL_NEW=added\n")  # replaced, added
    assert gw._secret("RL_KEY") == "two" and gw._secret("RL_NEW") == "added"
    assert gw._api_key(entry) == "two"  # the 5 minute key cache does not hide the change
    assert gw._secret("RL_ENVWINS") == "real"  # a real environment variable always wins
    _bump(env, "OTHER=1\n")  # removed
    assert gw._secret("RL_KEY") is None and gw._secret("RL_NEW") is None
    assert gw._api_key(entry) is None
    assert gw._secret("RL_ENVWINS") == "real"  # still the environment's
    env.unlink()
    assert gw._secret("OTHER") is None


def test_thinking_option_add_model_set_thinking_and_list(run):
    base = ("add-model", "nim", "--provider", "openai/nvidia/x", "--api-base", "https://integrate.api.nvidia.com/v1",
            "--api-key-env", "NVIDIA_API_KEY")
    assert run(*base)[0] == 0 and kdb.Database(run.path).get_model("nim")["thinking"] is None
    assert "thinking=" not in run("list-models")[1]
    assert run(*base, "--thinking", "off")[0] == 0
    assert kdb.Database(run.path).get_model("nim")["thinking"] == "off"
    assert "thinking=off" in run("list-models")[1]
    assert run(*base)[0] == 0 and kdb.Database(run.path).get_model("nim")["thinking"] == "off"  # kept
    assert run("set-thinking", "nim", "on")[0] == 0 and kdb.Database(run.path).get_model("nim")["thinking"] == "on"
    assert run("set-thinking", "nim", "default")[0] == 0 and kdb.Database(run.path).get_model("nim")["thinking"] is None
    assert run("set-thinking", "nope", "off")[0] == 1
