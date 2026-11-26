# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Import of a model catalog XML (import-models --xml, tools/inject.py): validation with all errors at once,
all-or-nothing write, dry run, backup, ping gate for enabled models, key NAMES only, export -> import -> export
round trip. Offline; temp DB files only; the ping is a stub."""
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kiko_lite import catalog_xml, cli
from kiko_lite import db as kdb

ROOT = Path(__file__).resolve().parents[3]


def src_db(tmp_path):
    d = kdb.Database(tmp_path / "src" / "kiko.db")
    d.upsert_model("opus", "cgw/opus", "CGW_TOKEN", "http://127.0.0.1:4100/v1", "OPUS",
                   roles=["OPUS", "CODER", "EYES"], price_in=0.0, price_out=0.0, context_window=1_000_000,
                   max_output=128_000, display_name="Anthropic Opus 5.5", status="ok")
    d.upsert_model("nemotron-ultra", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "NVIDIA_API_KEY", model_class="OPUS",
                   priority=50, context_window=1_000_000, max_output=65_536, status="ok", thinking="off",
                   timeout=600, toolshim="on")
    d.upsert_model("gemini-35-lite", "gemini/gemini-3.5-flash-lite", "GEMINI_API_KEY", model_class="HAIKU",
                   roles=["HAIKU", "TUTOR"], price_in=0.3, price_out=2.5, price_cache=0.03, context_window=1_048_576,
                   max_output=65_536, enabled=False, display_name="Gemini 3.5 Flash-Lite")
    d.upsert_model("mystery", "gemini/gemini-x", "GEMINI_API_KEY", model_class="TUTOR", enabled=False)
    return d


def collect(out):
    return lambda line: out.append(line)


def good_pinger(alias):
    return True, "stub ok"


def test_round_trip_export_import_export_is_identical(tmp_path):
    a = src_db(tmp_path)
    xml1 = catalog_xml.export_xml(a.models())
    target = tmp_path / "empty" / "kiko.db"  # does not exist yet: import creates it
    out = []
    code = catalog_xml.run_import(target, xml1, pinger=good_pinger, say=collect(out), settle=0)
    assert code == 0, out
    b = kdb.Database(target)
    assert catalog_xml.export_xml(b.models()) == xml1
    assert [r["alias"] for r in b.models() if r["enabled"]] == sorted(["nemotron-ultra", "opus"])
    # importing it again changes nothing
    out.clear()
    assert catalog_xml.run_import(target, xml1, pinger=good_pinger, say=collect(out), settle=0) == 0
    assert "0 added, 0 changed, 4 unchanged" in out[-1]


def test_dry_run_writes_nothing_and_shows_the_plan(tmp_path):
    a = src_db(tmp_path)
    xml1 = catalog_xml.export_xml(a.models())
    target = tmp_path / "t" / "kiko.db"
    out = []
    assert catalog_xml.run_import(target, xml1, dry_run=True, say=collect(out)) == 0
    assert not target.exists() and not target.parent.exists()  # not even the folder
    assert any(line.startswith("  + added") and "opus" in line for line in out)
    assert "4 added, 0 changed, 0 unchanged (dry run, nothing written)" in out[-1]
    # against an existing database: byte-identical afterwards, plan says changed / unchanged
    b = kdb.Database(target)
    b.upsert_model("opus", "cgw/opus", "CGW_TOKEN", "http://127.0.0.1:4100/v1", "OPUS", priority=7)
    before = b.models()
    out.clear()
    assert catalog_xml.run_import(target, xml1, dry_run=True, say=collect(out)) == 0
    assert any("~ changed" in line and "opus" in line and "priority" in line for line in out)
    assert "3 added, 1 changed, 0 unchanged" in out[-1]
    assert kdb.Database(target).models() == before
    assert not list(target.parent.glob("*.bak-*"))  # no backup for a dry run


def test_new_models_are_disabled_unless_enabled_and_pinged(tmp_path):
    xml = catalog_xml.export_xml(src_db(tmp_path).models())
    seen = []

    def pinger(alias):
        seen.append(alias)
        return alias == "opus", "stub"

    target = tmp_path / "t" / "kiko.db"
    out = []
    assert catalog_xml.run_import(target, xml, pinger=pinger, say=collect(out), settle=0) == 0
    rows = {r["alias"]: r for r in kdb.Database(target).models()}
    assert seen == ["nemotron-ultra", "opus"]  # only the ones the file marks enabled are pinged
    assert (rows["opus"]["enabled"], rows["opus"]["status"]) == (1, "ok")
    assert (rows["nemotron-ultra"]["enabled"], rows["nemotron-ultra"]["status"]) == (0, "error")  # ping failed
    assert (rows["gemini-35-lite"]["enabled"], rows["mystery"]["enabled"]) == (0, 0)
    assert any("nemotron-ultra: ping failed" in line and "DISABLED" in line for line in out)


def test_import_by_alias_updates_and_keeps_enabled_and_status(tmp_path):
    a = src_db(tmp_path)
    target = tmp_path / "t" / "kiko.db"
    b = kdb.Database(target)
    b.upsert_model("opus", "old/provider", "OLD_KEY", model_class="SONNET", enabled=False, status="error",
                   price_in=9, price_out=9, thinking="on")
    out = []
    assert catalog_xml.run_import(target, catalog_xml.export_xml(a.models()), pinger=good_pinger, say=collect(out),
                                  settle=0) == 0
    row = kdb.Database(target).get_model("opus")
    assert (row["provider"], row["api_key_env"], row["model_class"]) == ("cgw/opus", "CGW_TOKEN", "OPUS")
    assert row["roles"] == ["OPUS", "EYES", "CODER"] and row["display_name"] == "Anthropic Opus 5.5"
    assert (row["price_in_per_mtok"], row["context_window"], row["max_output"]) == (0.0, 1_000_000, 128_000)
    assert row["thinking"] is None  # the file has none: back to default
    assert (row["enabled"], row["status"]) == (0, "error")  # operational state is the database's own
    assert len(list(target.parent.glob("kiko.db.bak-*-import"))) == 1  # backed up before the write
    # a second import is all "unchanged": no write, no second backup
    out.clear()
    catalog_xml.run_import(target, catalog_xml.export_xml(a.models()), pinger=good_pinger, say=collect(out), settle=0)
    assert "0 added, 0 changed, 4 unchanged" in out[-1] and len(list(target.parent.glob("kiko.db.bak-*"))) == 1


BASE = """<?xml version="1.0" encoding="UTF-8"?>
<kiko-models version="1">
{models}
</kiko-models>
"""
MODEL = """  <model id="{id}" enabled="{enabled}" status="ok">
    <alias>{alias}</alias>
    <name>{name}</name>
    <key>{key}</key>
    <roles>{roles}</roles>
    <context>{context}</context>
    <max_output>{max_output}</max_output>
    <price per="1M">{price}</price>
    <priority>{priority}</priority>
  </model>"""


def model(**kw):
    d = dict(id="m1", enabled="false", alias="m1", name="gemini/gemini-3.5-flash-lite", key="GEMINI_API_KEY",
             roles="<role>HAIKU</role>", context="1048576", max_output="65536", price="free", priority="100")
    d.update(kw)
    return MODEL.format(**d)


def doc(*models):
    return BASE.format(models="\n".join(models)).encode("utf-8")


def snapshot(path):
    return kdb.Database(path).models()


def test_every_problem_is_listed_and_nothing_is_written(tmp_path):
    target = tmp_path / "t" / "kiko.db"
    base = kdb.Database(target)
    base.upsert_model("keep", "gemini/gemini-x", "GEMINI_API_KEY")
    before = snapshot(target)
    bad = doc(model(id="a", key="my-lowercase-key"),
              model(id="b", roles="<role>NOPE</role>"),
              model(id="c", context="lots", price="cheap"),
              model(id="d", name="strange/model"),
              model(id="e", roles=""),
              model(id="e", priority="x"))
    out = []
    assert catalog_xml.run_import(target, bad, say=collect(out)) == 2
    text = "\n".join(out)
    for needle in ("model 'a': <key> must be the NAME", "model 'b': role NOPE is not a class",
                   "model 'c': <context> must be a whole number", "model 'c': <price> must be 'free'",
                   "model 'd': <name>", "model 'e': <roles> needs at least one", "model 'e': <priority>",
                   "model 'e': id appears twice"):
        assert needle in text, (needle, text)
    assert "nothing was written" in text
    assert snapshot(target) == before and not list(target.parent.glob("*.bak-*"))


def test_a_key_value_anywhere_refuses_the_file(tmp_path):
    target = tmp_path / "t" / "kiko.db"
    for where in ("alias", "key", "name"):
        kw = {where: "nvapi-AbCdEf0123456789SECRET"}
        out = []
        assert catalog_xml.run_import(target, doc(model(**kw)), say=collect(out), dry_run=True) == 2
        assert any("looks like an API key value" in line for line in out)
        assert not any("SECRET" in line for line in out)  # and the value is never echoed back
    out = []
    assert catalog_xml.run_import(target, doc(model(key="AKIAABCDEFGHIJKLMNOP")), say=collect(out)) == 2
    assert not target.exists()
    assert any("<key>" in line for line in out)


def test_doctype_malformed_and_wrong_root_are_refused(tmp_path):
    t = tmp_path / "k.db"
    for data, needle in ((b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><kiko-models version="1"/>', "DOCTYPE"),
                         (b"<kiko-models", "not well-formed"),
                         (b'<other version="1"/>', "root element"),
                         (b'<kiko-models version="2"/>', 'version="1"')):
        out = []
        assert catalog_xml.run_import(t, data, say=collect(out), dry_run=True) == 2
        assert needle in "\n".join(out), (data, out)


def test_all_or_nothing_when_the_database_refuses_one_model(tmp_path):
    target = tmp_path / "t" / "kiko.db"
    base = kdb.Database(target)
    base.upsert_model("Opus", "gemini/gemini-x", "GEMINI_API_KEY")  # "opus" would differ only in case
    before_models = [r["alias"] for r in base.models()]
    out = []
    data = doc(model(id="alpha"), model(id="opus"))
    assert catalog_xml.run_import(target, data, say=collect(out), settle=0) == 2
    assert "differs only in case" in "\n".join(out)
    assert [r["alias"] for r in kdb.Database(target).models()] == before_models  # alpha was rolled back


def test_first_role_is_primary_unless_marked(tmp_path):
    target = tmp_path / "t" / "kiko.db"
    data = doc(model(id="first", roles="<role>EYES</role><role>CODER</role>"),
               model(id="marked", roles='<role>EYES</role><role primary="true">CODER</role>'))
    assert catalog_xml.run_import(target, data, say=lambda s: None) == 0
    d = kdb.Database(target)
    assert (d.get_model("first")["model_class"], d.get_model("first")["roles"]) == ("EYES", ["EYES", "CODER"])
    assert (d.get_model("marked")["model_class"], d.get_model("marked")["roles"]) == ("CODER", ["CODER", "EYES"])
    bad = doc(model(roles='<role primary="true">EYES</role><role primary="true">CODER</role>'))
    out = []
    assert catalog_xml.run_import(target, bad, say=collect(out), dry_run=True) == 2
    assert "more than one primary" in "\n".join(out)


def test_cli_import_models_and_inject_script(tmp_path, capsys):
    a = src_db(tmp_path)
    xml_file = tmp_path / "models.xml"
    assert cli.main(["export-models", "--xml", "--out", str(xml_file), "--db", str(a.path)]) == 0
    capsys.readouterr()
    target = tmp_path / "t" / "kiko.db"
    if (ROOT / "tools" / "inject.py").is_file():  # a repo-level script, not shipped in the kiko-lite zip
        r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "inject.py"), str(xml_file), "--dry-run",
                            "--db", str(target)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr  # inject.py: --dry-run writes nothing
        assert "4 added" in r.stdout and not target.exists()
    # the CLI command, every model disabled in the file (no ping is made)
    disabled = ET.fromstring(xml_file.read_bytes())
    for m in disabled.findall("model"):
        m.set("enabled", "false")
    xml_file.write_bytes(ET.tostring(disabled, encoding="utf-8", xml_declaration=True))
    assert cli.main(["import-models", "--xml", str(xml_file), "--db", str(target)]) == 0
    assert "4 added" in capsys.readouterr().out
    assert {r["alias"]: r["enabled"] for r in kdb.Database(target).models()} == \
        {"opus": 0, "nemotron-ultra": 0, "gemini-35-lite": 0, "mystery": 0}
    assert cli.main(["import-models", "--xml", str(tmp_path / "missing.xml"), "--db", str(target)]) == 2
    assert cli.main(["import-models", "--xml", str(xml_file), "--dry-run", "--db", str(target)]) == 0
    assert "4 unchanged" in capsys.readouterr().out


def test_models_import_never_stores_a_key_value(tmp_path):
    target = tmp_path / "t" / "kiko.db"
    assert catalog_xml.run_import(target, doc(model()), say=lambda s: None) == 0
    assert kdb.Database(target).get_model("m1")["api_key_env"] == "GEMINI_API_KEY"


def test_importing_ones_own_export_is_all_unchanged(tmp_path):
    a = src_db(tmp_path)  # holds a free-tier row with NULL prices (nemotron-ultra), exported as "free"
    out = []
    assert catalog_xml.run_import(a.path, catalog_xml.export_xml(a.models()), dry_run=True, say=collect(out)) == 0
    assert "0 added, 0 changed, 4 unchanged" in out[-1], out
