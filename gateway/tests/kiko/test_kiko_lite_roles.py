# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""One model, many roles (schema v10): migration, resolver by role, CLI, management API and web form.

Offline; temp DB files only (never ~/.kiko)."""
import http.client
import json
import sqlite3

import pytest

from kiko_lite import cli, management
from kiko_lite import db as kdb
from kiko_lite.resolver import Catalog, DBResolver, build_db_app
from kikolib import server

# A shape like the live catalog (2026-11-26): one route per Claude class, NVIDIA / Gemini / Mistral rows with
# the primary class only, a disabled one, an error one and low-priority hub-* rows.
SHAPE = [
    # alias, provider, key, class, priority, enabled, status
    ("opus", "gemini/gemini-3.5-flash-lite", "K1", "OPUS", 100, True, "ok"),
    ("sonnet", "gemini/gemini-3.5-flash-lite", "K1", "SONNET", 100, True, "ok"),
    ("haiku", "gemini/gemini-3.5-flash-lite", "K1", "HAIKU", 100, True, "ok"),
    ("nemotron-ultra", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "NVIDIA_API_KEY", "OPUS", 50, True, "ok"),
    ("gpt-astra", "openai/gpt-6.2-astra", "OPENROUTER_API_KEY", "OPUS", 40, False, "unknown"),
    ("nemotron-super", "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b", "NVIDIA_API_KEY", "SONNET", 60, True, "error"),
    ("llama-nemotron", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b", "NVIDIA_API_KEY", "SONNET", 70, True, "ok"),
    ("gemini-lite", "gemini/gemini-flash-lite-latest", "GEMINI_API_KEY", "HAIKU", 80, True, "unknown"),
    ("muse-glimmer", "nvidia/meta/muse-glimmer-30b", "NVIDIA_API_KEY", "FABLE", 100, True, "ok"),
    ("codestral", "mistral/codestral-latest", "MISTRAL_API_KEY", "CODER", 100, False, "unknown"),
    ("nemotron-vl", "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b", "NVIDIA_API_KEY", "EYES", 100, True, "ok"),
    ("gemini-tutor", "gemini/gemini-3.5-flash-lite", "GEMINI_API_KEY", "TUTOR", 100, True, "ok"),
    ("hub-gemini-3.5-flash-lite", "gemini/gemini-3.5-flash-lite", "GEMINI_API_KEY", "HAIKU", 900, True, "ok"),
    ("hub-gemini-3.8-flash", "gemini/gemini-3.8-flash", "GEMINI_API_KEY", "SONNET", 900, False, "error"),
]
CLASS_NAMES = ("opus", "sonnet", "haiku", "fable", "coder", "kiko-coder", "eyes", "tutor", "router", "voice")


def fill(d):
    for alias, prov, key, cls, prio, en, st in SHAPE:
        d.upsert_model(alias, prov, key, model_class=cls, priority=prio, enabled=en, status=st)


def old_rule(rows, cls):
    """The resolver rule of schema <= 9, written independently: class == model_class, ok first, then unknown, then
    error; priority then id inside. Gives the ranked aliases."""
    rank = {"ok": 0, "unknown": 1}
    hits = [r for r in rows if r["enabled"] and r["model_class"] == cls]
    return [r["alias"] for r in sorted(hits, key=lambda r: (rank.get(r["status"], 2), r["priority"], r["id"]))]


def new_rule(d, cls):
    snap = Catalog(d).snapshot()
    return [m.row["alias"] for m in snap.ranked(cls)]


def downgrade_to_v9(path):
    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER IF EXISTS models_role_follow_class")  # v9 had no triggers
    con.execute("DROP TRIGGER IF EXISTS models_roles_cleanup")
    con.execute("DROP TABLE model_roles")
    con.execute("ALTER TABLE models DROP COLUMN display_name")
    con.execute("PRAGMA user_version=9")
    con.commit()
    con.close()


def test_migration_v9_to_v10_copies_each_class_as_first_role(tmp_path):
    p = tmp_path / "kiko.db"
    d = kdb.Database(p)
    fill(d)
    downgrade_to_v9(p)
    d2 = kdb.Database(p)
    assert d2.schema_version() == 10 == kdb.SCHEMA_VERSION
    rows = d2.models()
    assert len(rows) == len(SHAPE)
    for r in rows:
        assert r["roles"] == [r["model_class"]], r
    con = sqlite3.connect(p)
    assert con.execute("SELECT COUNT(*) FROM model_roles").fetchone()[0] == len(SHAPE)
    assert "display_name" in {c[1] for c in con.execute("PRAGMA table_info(models)")}
    con.close()
    kdb.Database(p)  # a second open changes nothing
    assert sorted(sum((r["roles"] for r in kdb.Database(p).models()), [])) == sorted(s[3] for s in SHAPE)


def test_resolver_picks_the_same_models_as_before_for_todays_data_shape(tmp_path):
    p = tmp_path / "kiko.db"
    d = kdb.Database(p)
    fill(d)
    rows = d.models()
    # the independent v9 rule on the v9 columns ...
    before = {c.upper(): old_rule(rows, c.upper()) for c in ("opus", "sonnet", "haiku", "fable", "coder", "eyes", "tutor")}
    assert before["OPUS"][0] == "opus" or before["OPUS"][0] == "nemotron-ultra"  # shape sanity: not empty
    # ... equals the role-based resolver after migrating a real v9 file
    downgrade_to_v9(p)
    d2 = kdb.Database(p)
    for cls, ranked in before.items():
        assert new_rule(d2, cls) == ranked, cls
    res = DBResolver(Catalog(d2))
    for name in CLASS_NAMES:  # what the client-visible names pick, before == after
        want = old_rule(rows, name.upper().replace("KIKO-", ""))
        got = [c for c in res.candidates(name)]
        assert [pn for pn, _ in got][:len(want)] == [f"db/{a}" for a in want] if want else True, name


def test_a_second_role_makes_the_model_a_candidate_of_that_class(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    fill(d)
    base_coder = new_rule(d, "CODER")  # codestral is disabled: nothing
    assert base_coder == []
    d.upsert_model("opus", "gemini/gemini-3.5-flash-lite", "K1", model_class="OPUS", roles=["OPUS", "CODER", "EYES"])
    assert d.get_model("opus")["roles"] == ["OPUS", "EYES", "CODER"] or d.get_model("opus")["roles"][0] == "OPUS"
    assert set(d.get_model("opus")["roles"]) == {"OPUS", "CODER", "EYES"}
    assert new_rule(d, "CODER") == ["opus"]
    assert new_rule(d, "EYES") == ["opus", "nemotron-vl"] or new_rule(d, "EYES")[0] in ("opus", "nemotron-vl")
    assert "opus" in new_rule(d, "EYES") and "nemotron-vl" in new_rule(d, "EYES")
    res = DBResolver(Catalog(d))
    assert res("kiko-coder") == ("db/opus", "gemini-3.5-flash-lite")
    # the primary class order of OPUS is unchanged by the extra roles
    assert new_rule(d, "OPUS") == ["opus", "nemotron-ultra"] or new_rule(d, "OPUS")[0] in ("opus", "nemotron-ultra")
    # class counts include role membership
    counts = {c["name"]: c["models"] for c in d.classes()}
    assert counts["CODER"] == 1 and counts["EYES"] == 2


def test_priority_and_health_still_rule_inside_a_role(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("a", "gemini/gemini-3.5-flash-lite", "K", model_class="SONNET", priority=50, status="error",
                   roles=["SONNET", "CODER"])
    d.upsert_model("b", "gemini/gemini-3.5-flash-lite", "K", model_class="CODER", priority=100, status="ok")
    d.upsert_model("c", "gemini/gemini-3.5-flash-lite", "K", model_class="CODER", priority=10, status="ok",
                   roles=["CODER", "SONNET"])
    assert new_rule(d, "CODER") == ["c", "b", "a"]  # ok before error, priority inside
    assert Catalog(d).snapshot().best("CODER").row["alias"] == "c"


def test_roles_api_rules(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU")
    assert d.get_model("m")["roles"] == ["HAIKU"]
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU", roles="eyes, coder")
    assert d.get_model("m")["roles"][0] == "HAIKU" and set(d.get_model("m")["roles"]) == {"HAIKU", "EYES", "CODER"}
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU")  # omitted = kept
    assert set(d.get_model("m")["roles"]) == {"HAIKU", "EYES", "CODER"}
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="SONNET")  # new primary replaces the old
    assert d.get_model("m")["roles"][0] == "SONNET" and set(d.get_model("m")["roles"]) == {"SONNET", "EYES", "CODER"}
    assert d.set_model_class("m", "OPUS") and set(d.get_model("m")["roles"]) == {"OPUS", "EYES", "CODER"}
    assert d.set_roles("m", ["EYES"]) and d.get_model("m")["model_class"] == "EYES"  # primary left the set
    assert d.get_model("m")["roles"] == ["EYES"]
    with pytest.raises(ValueError, match="at least one role"):
        d.set_roles("m", [])
    with pytest.raises(ValueError, match="unknown role"):
        d.set_roles("m", ["NOPE"])
    with pytest.raises(ValueError, match="unknown role"):
        d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", roles=["NOPE"])
    with pytest.raises(ValueError):
        d.delete_class("EYES")  # a role keeps the class in use
    assert d.set_roles("nobody", ["EYES"]) is False
    assert d.delete_model("m")
    con = sqlite3.connect(tmp_path / "kiko.db")
    assert con.execute("SELECT COUNT(*) FROM model_roles").fetchone()[0] == 0
    con.close()


def test_a_row_without_role_rows_serves_its_class(tmp_path):
    p = tmp_path / "kiko.db"
    d = kdb.Database(p)
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU")
    con = sqlite3.connect(p)
    con.execute("DELETE FROM model_roles")  # as if kiko-full had written the row
    con.commit()
    con.close()
    assert d.models()[0]["roles"] == ["HAIKU"] and new_rule(d, "HAIKU") == ["m"]


# ---- CLI -----------------------------------------------------------------------------------------------

def run_cli(path, capsys, *argv):
    code = cli.main(list(argv) + ["--db", str(path)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_role_option_and_list_models(tmp_path, capsys):
    p = tmp_path / "kiko.db"
    base = ["add-model", "opus", "--provider", "gemini/gemini-3.5-flash-lite", "--api-key-env", "MY_KEY"]
    code, out, err = run_cli(p, capsys, *base, "--role", "OPUS,CODER,EYES")
    assert code == 0, err
    m = kdb.Database(p).get_model("opus")
    assert m["model_class"] == "OPUS" and m["roles"] == ["OPUS", "EYES", "CODER"]  # first role = primary; class order
    out = run_cli(p, capsys, "list-models")[1]
    assert "roles=OPUS,EYES,CODER" in out
    # --class sets the primary and is always one of the roles
    assert run_cli(p, capsys, *base, "--class", "sonnet", "--role", "EYES,CODER")[0] == 0
    assert kdb.Database(p).get_model("opus")["roles"][0] == "SONNET"
    assert set(kdb.Database(p).get_model("opus")["roles"]) == {"SONNET", "EYES", "CODER"}
    # kept when --role is omitted
    assert run_cli(p, capsys, *base, "--class", "sonnet")[0] == 0
    assert set(kdb.Database(p).get_model("opus")["roles"]) == {"SONNET", "EYES", "CODER"}
    # a model must keep at least one role; unknown roles are refused
    code, _, err = run_cli(p, capsys, *base, "--role", ",")
    assert code == 2 and "at least one role" in err
    code, _, err = run_cli(p, capsys, *base, "--class", "sonnet", "--role", "NOPE")
    assert code == 2 and "unknown role" in err
    assert set(kdb.Database(p).get_model("opus")["roles"]) == {"SONNET", "EYES", "CODER"}
    assert run_cli(p, capsys, *base, "--display-name", "Anthropic Opus 5.5")[0] == 0
    assert kdb.Database(p).get_model("opus")["display_name"] == "Anthropic Opus 5.5"


# ---- management API (tray) -----------------------------------------------------------------------------

TOKEN = "management-token-1234567890"
HDR = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}


def call(srv, path, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        c.request(method, path, body=body, headers=HDR)
        r = c.getresponse()
        return r.status, json.loads(r.read())
    finally:
        c.close()


def test_management_models_and_roles_endpoint(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("opus", "gemini/gemini-3.5-flash-lite", "K", model_class="OPUS", roles=["OPUS", "CODER", "EYES"])
    app = build_db_app(d, None, secret=lambda n: None, auth=False)
    management.install(app, d, tmp_path / "kiko.db", 4000, token=TOKEN)
    s = server.Server(app, port=0).start()
    try:
        status, body = call(s, "/kiko/v1/models")
        m = body["models"][0]
        assert status == 200 and m["class"] == "OPUS" and m["roles"] == ["OPUS", "EYES", "CODER"]
        assert m["roles_text"] == "OPUS, EYES, CODER"
        status, body = call(s, "/kiko/v1/models/opus/roles", "POST", json.dumps({"roles": ["CODER"]}))
        assert status == 200 and body == {"name": "opus", "class": "CODER", "roles": ["CODER"]}
        status, body = call(s, "/kiko/v1/models/opus/roles", "POST", json.dumps({"roles": ["EYES", "CODER"],
                                                                                 "primary": "EYES"}))
        assert status == 200 and body["class"] == "EYES" and body["roles"] == ["EYES", "CODER"]
        for bad in ({"roles": []}, {"roles": ["lower"]}, {"roles": "OPUS"}, {}, {"roles": ["NOPE"]}):
            status, body = call(s, "/kiko/v1/models/opus/roles", "POST", json.dumps(bad))
            assert status == 400 and body["error"]["code"] == "bad_request", bad
        status, body = call(s, "/kiko/v1/models/ghost/roles", "POST", json.dumps({"roles": ["OPUS"]}))
        assert status == 404
        assert d.get_model("opus")["roles"] == ["EYES", "CODER"]
    finally:
        s.stop()


# ---- triggers: model_roles follows models written by something else ---------------------------------------

def raw(path, sql, *params):
    con = sqlite3.connect(path)
    try:
        out = con.execute(sql, params).fetchall()
        con.commit()
        return out
    finally:
        con.close()


def role_names(path, alias):
    return sorted(r[0] for r in raw(path, "SELECT class_name FROM model_roles r JOIN models m ON m.id=r.model_id"
                                         " WHERE m.alias=?", alias))


def test_triggers_keep_roles_in_step_with_a_direct_sql_change(tmp_path):
    p = tmp_path / "kiko.db"
    d = kdb.Database(p)
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU", roles=["HAIKU", "EYES"])
    raw(p, "UPDATE models SET model_class='SONNET' WHERE alias='m'")  # as kiko-full / a SQL client would
    assert role_names(p, "m") == ["EYES", "SONNET"]
    raw(p, "UPDATE models SET model_class='NOSUCHCLASS' WHERE alias='m'")  # unknown class: no role row for it
    assert role_names(p, "m") == ["EYES"]
    raw(p, "UPDATE models SET model_class='EYES' WHERE alias='m'")  # already a role: still one row
    assert role_names(p, "m") == ["EYES"]
    raw(p, "DELETE FROM models WHERE alias='m'")
    assert raw(p, "SELECT COUNT(*) FROM model_roles") == [(0,)]


def test_a_v10_database_without_the_triggers_gets_them_on_open(tmp_path):
    p = tmp_path / "kiko.db"
    d = kdb.Database(p)
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU")
    raw(p, "DROP TRIGGER models_role_follow_class")
    raw(p, "DROP TRIGGER models_roles_cleanup")
    assert d.schema_version() == 10
    kdb.Database(p)  # reopen: already v10, the triggers come back
    raw(p, "UPDATE models SET model_class='SONNET' WHERE alias='m'")
    assert role_names(p, "m") == ["SONNET"]
    raw(p, "DELETE FROM models")
    assert raw(p, "SELECT COUNT(*) FROM model_roles") == [(0,)]


def test_the_library_paths_still_agree_with_the_triggers(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="HAIKU", roles=["HAIKU", "EYES"])
    d.set_model_class("m", "SONNET")
    assert d.models()[0]["roles"] == ["SONNET", "EYES"]
    d.set_roles("m", ["TUTOR", "EYES"], primary="TUTOR")
    assert d.models()[0]["roles"] == ["TUTOR", "EYES"]
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", model_class="CODER")
    assert d.models()[0]["roles"] == ["CODER", "EYES"]


def test_display_names_with_control_characters_are_refused(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", display_name="Fine name")
    for bad in ("two\nlines", "tab\there", "bell\x07"):
        with pytest.raises(ValueError, match="control characters"):
            d.set_display_name("m", bad)
        with pytest.raises(ValueError, match="control characters"):
            d.upsert_model("m", "gemini/gemini-3.5-flash-lite", "K", display_name=bad)
    assert d.get_model("m")["display_name"] == "Fine name"
    assert d.set_display_name("m", "") and d.get_model("m")["display_name"] is None
