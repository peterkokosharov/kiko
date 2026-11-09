# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""All planned model classes are seeded (schema v3). Offline, temp DB only."""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from kiko_lite import db as kdb
from kiko_lite.resolver import Catalog, DBResolver
from .test_kiko_lite_cli import run, store  # noqa: F401  (fixtures: temp DB + fake keystore)

PLANNED = ["VOICE", "EYES", "EARS", "MEMORY", "RERANK", "GUARD", "CODER", "ROUTER", "TUTOR", "OCR", "PAINTER",
           "VIDEO", "MUSIC", "WATCH", "SENSOR", "REALTIME", "ACTION"]

TODAY = ["claude-opus-4", "claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5", "claude-fable-1",
         "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3-flash",
         "nvidia/llama-3.1-nemotron-ultra-253b-v1", "meta/llama-3.3-70b-instruct", "gpt-4o", "gpt-4.1-mini",
         "gpt-5", "gpt-6", "gpt-4o-realtime-preview", "gpt-5-codex", "o3-mini", "deepseek-chat", "deepseek-reasoner",
         "deepseek-coder", "deepseek-r1", "qwen2.5-coder-32b", "codestral-latest", "openrouter/auto",
         "llama-guard-3-8b", "mistral-large", "mistral-ocr-latest", "gemini-2.5-computer-use",
         "text-embedding-3-small", "whisper-1", "tts-1"]


NEW_IDS = {"nvidia/llama-3.2-nv-rerankqa-1b-v2": "RERANK",  # deliberate: a reranker is the RERANK class
           "veo-3.0-generate-001": None, "gemini-2.5-flash-preview-tts": None, "gemini-live-2.5-flash": None,
           "gpt-4o-audio-preview": None, "qwen2.5-vl-72b-instruct": None, "mistral-embed": None,
           "llama-4-scout": None, "gpt-image-1": None, "deepseek-v3": None}


def test_more_model_ids_route_as_expected():
    for name, cls in NEW_IDS.items():
        assert _detect(name) == cls, name


def _detect(name):
    """First class whose keyword is in the lower-cased name, as the resolver does (None when none)."""
    low = name.lower()
    return next((n for n, k, _, _ in kdb.DEFAULT_CLASSES if k in low), None)


def _v2_db(path, extra=()):
    """A DB as schema v2 left it: the four classes, user_version 2."""
    con = sqlite3.connect(path)
    con.executescript(kdb._SCHEMA + kdb._V1 + kdb._V2)
    for t, c, d in kdb._COLUMNS:
        if c not in {r[1] for r in con.execute(f"PRAGMA table_info({t})")}:
            con.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
    con.executemany("INSERT INTO model_classes(name, keyword, role, sort) VALUES(?,?,?,?)",
                    kdb._CLASSES_V2 + tuple(extra))
    con.execute("INSERT INTO models(alias, provider, api_key_env, model_class) VALUES('mine','gemini/x','K','TINY')")
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()


def test_new_db_has_all_classes(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    names = d.class_names()
    assert names[:4] == ["OPUS", "SONNET", "HAIKU", "FABLE"] and names[4:] == PLANNED
    assert d.schema_version() == kdb.SCHEMA_VERSION
    assert all(c["role"] and kdb.CLASS_NAME.match(c["name"]) for c in d.classes())


def test_keywords_valid_and_never_overlap():
    kws = [k for _, k, _, _ in kdb.DEFAULT_CLASSES]
    names = [n for n, _, _, _ in kdb.DEFAULT_CLASSES]
    assert len(set(names)) == len(names) and len(set(kws)) == len(kws)
    for n, k, r, o in kdb.DEFAULT_CLASSES:
        assert kdb.CLASS_NAME.match(n) and 3 <= len(k) <= 64 and k == k.lower().strip() and r and o > 0
    for i, a in enumerate(kws):
        for b in kws[i + 1:]:
            assert a not in b and b not in a, (a, b)
    assert [o for *_, o in kdb.DEFAULT_CLASSES] == sorted(o for *_, o in kdb.DEFAULT_CLASSES)


def test_today_model_names_keep_their_class():
    expect = {"claude-opus-4": "OPUS", "claude-opus-4-1": "OPUS", "claude-sonnet-4-5": "SONNET",
              "claude-haiku-4-5": "HAIKU", "claude-fable-1": "FABLE"}
    for name in TODAY:
        assert _detect(name) == expect.get(name), name


def test_resolver_regression_with_all_classes(tmp_path):
    """Same answers with all classes as with only the four old ones (planned rows removed from a copy)."""
    d = kdb.Database(tmp_path / "all.db")
    old = kdb.Database(tmp_path / "old.db")
    for x in (d, old):
        x.upsert_model("nim", "nvidia_nim/x", "K")  # default class SONNET
    with old._write() as con:
        con.executemany("DELETE FROM model_classes WHERE name=?", [(n,) for n in PLANNED])
    assert old.class_names() == ["OPUS", "SONNET", "HAIKU", "FABLE"]
    r_all, r_old = DBResolver(Catalog(d, 0.0), {}), DBResolver(Catalog(old, 0.0), {})
    assert r_all("claude-sonnet-4-5") == ("db/nim", "x")
    for name in TODAY:
        assert r_all(name) == r_old(name), name


def test_v2_db_migrates_to_v3_and_keeps_custom(tmp_path):
    p = tmp_path / "kiko.db"
    _v2_db(p, extra=[("TINY", "tinykw", "mine", 5)])
    d = kdb.Database(p)
    assert d.schema_version() == kdb.SCHEMA_VERSION
    names = d.class_names()
    assert set(names) == {"OPUS", "SONNET", "HAIKU", "FABLE", "TINY"} | set(PLANNED)
    assert names[0] == "TINY" and [m["alias"] for m in d.models()] == ["mine"]
    assert {c["name"]: c["models"] for c in d.classes()}["TINY"] == 1


def test_v3_skips_overlap_with_users_own_class(tmp_path):
    p = tmp_path / "kiko.db"
    _v2_db(p, extra=[("TINY", "videogen", "mine", 5), ("VOICE", "speech", "own role", 6)])
    d = kdb.Database(p)
    cl = {c["name"]: c for c in d.classes()}
    assert "VIDEO" not in cl and cl["VOICE"]["keyword"] == "speech" and cl["VOICE"]["role"] == "own role"
    assert "EYES" in cl and "MUSIC" in cl


def test_deleted_class_is_not_readded(tmp_path):
    """A class deleted after the upgrade stays deleted across reopenings."""
    p = tmp_path / "kiko.db"
    _v2_db(p)
    d = kdb.Database(p)
    con = sqlite3.connect(p)
    con.execute("DELETE FROM model_classes WHERE name IN ('VOICE', 'OPUS')")
    con.commit()
    con.close()
    for _ in range(3):
        d2 = kdb.Database(p)
        assert "VOICE" not in d2.class_names() and "OPUS" not in d2.class_names() and d is not d2
    assert len(d2.classes()) == len(kdb.DEFAULT_CLASSES) - 2


def test_class_deleted_on_v2_before_upgrade_is_not_added(tmp_path):
    """The owner deleted OPUS (a seeded v2 class) on a v2 DB: the v3 step must not bring it back."""
    p = tmp_path / "kiko.db"
    _v2_db(p)
    con = sqlite3.connect(p)
    con.execute("DELETE FROM model_classes WHERE name='OPUS'")
    con.commit()
    con.close()
    d = kdb.Database(p)
    names = d.class_names()
    assert "OPUS" not in names and set(PLANNED) <= set(names) and d.schema_version() == kdb.SCHEMA_VERSION


def test_v3_rows_sort_after_owner_classes(tmp_path):
    p = tmp_path / "kiko.db"
    _v2_db(p, extra=[("BIG", "bigkw", "mine", 900)])
    d = kdb.Database(p)
    cl = d.classes()
    names = [c["name"] for c in cl]
    assert names[:5] == ["OPUS", "SONNET", "HAIKU", "FABLE", "BIG"] and names[5:] == PLANNED
    assert min(c["sort"] for c in cl if c["name"] in PLANNED) > 900


_OPEN = ("import sys, time\n"
         "from kiko_lite import db\n"
         "time.sleep(max(0.0, float(sys.argv[2]) - time.time()))\n"
         "d = db.Database(sys.argv[1], busy_timeout=30.0)\n"
         "print(d.schema_version())\n")


def test_two_processes_open_v2_db_at_once(tmp_path):
    """kiko-lite and the web-ui opening the same v2 DB at the same moment: both start, each class once."""
    root = Path(__file__).resolve().parents[2]
    for i in range(5):
        p = tmp_path / f"race{i}.db"
        _v2_db(p)
        at = str(time.time() + 2.0)
        procs = [subprocess.Popen([sys.executable, "-c", _OPEN, str(p), at], cwd=root, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for pr in procs:
            out, err = pr.communicate(timeout=120)
            assert pr.returncode == 0, err
            assert out.strip() == str(kdb.SCHEMA_VERSION)
        con = sqlite3.connect(p)
        names = [r[0] for r in con.execute("SELECT name FROM model_classes")]
        version = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()
        assert version == kdb.SCHEMA_VERSION and sorted(names) == sorted(c[0] for c in kdb.DEFAULT_CLASSES)


def test_cli_list_classes_shows_all(run):
    kdb.Database(run.path)
    code, out, _ = run("list-classes")
    assert code == 0 and all(n in out for n in PLANNED)
    assert run("add-class", "VOICE", "--keyword", "talk")[0] == 2  # already seeded
