# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""XML export of the model catalog: well-formed, key NAMES only, one <role> tag per role, free vs priced,
<context> and <max_output> always present. Offline; temp DB files only."""
import http.client
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kiko_lite import catalog_xml, cli, management
from kiko_lite import db as kdb
from kiko_lite.resolver import HIDDEN, build_db_app
from kikolib import server

ROOT = Path(__file__).resolve().parents[3]
SECRET = "nvapi-SECRETVALUE1234567890abcdef"


@pytest.fixture
def d(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("opus", "cgw/opus", "ANTHROPIC_API_KEY", model_class="OPUS", roles=["OPUS", "CODER", "EYES"],
                   price_in=0.0, price_out=0.0, context_window=1_000_000, max_output=128_000,
                   display_name="Anthropic Opus 5.5", status="ok")
    d.upsert_model("nemotron-ultra", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "NVIDIA_API_KEY",
                   model_class="OPUS", context_window=1_000_000, max_output=65_536, status="ok")  # free tier
    d.upsert_model("gemini-35-lite", "gemini/gemini-3.5-flash-lite", "GEMINI_API_KEY", model_class="HAIKU",
                   price_in=0.3, price_out=2.5, price_cache=0.03, context_window=1_048_576, max_output=65_536,
                   enabled=False, thinking="off", timeout=600, toolshim="on")
    d.upsert_model("mystery", "gemini/gemini-x", "GEMINI_API_KEY", "https://example.org/v1", model_class="TUTOR")
    return d


def parse(d):
    return ET.fromstring(catalog_xml.export_xml(d.models()))


def by_id(root):
    return {m.get("id"): m for m in root.findall("model")}


def test_export_is_well_formed_utf8_and_stable(d):
    data = catalog_xml.export_xml(d.models())
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    root = ET.fromstring(data)
    assert root.tag == "kiko-models" and root.get("version") == "1"
    assert [m.get("id") for m in root.findall("model")] == ["gemini-35-lite", "mystery", "nemotron-ultra", "opus"]
    assert catalog_xml.export_xml(d.models()) == data  # stable
    data.decode("utf-8")


def test_the_sample_model_has_every_field(d):
    m = by_id(parse(d))["opus"]
    assert m.get("enabled") == "true" and m.get("status") == "ok"
    assert m.findtext("alias") == "Anthropic Opus 5.5"
    assert m.findtext("name") == "cgw/opus"
    assert m.findtext("key") == "ANTHROPIC_API_KEY"
    roles = m.findall("roles/role")  # one tag per role, the primary first and marked
    assert [r.text for r in roles] == ["OPUS", "EYES", "CODER"] and roles[0].get("primary") == "true"
    assert m.findtext("context") == "1000000" and m.findtext("max_output") == "128000"
    price = m.find("price")
    assert price.get("per") == "1M" and price.text == "free" and len(price) == 0
    assert m.findtext("priority") == "100"


def test_alias_falls_back_to_the_database_alias(d):
    assert by_id(parse(d))["mystery"].findtext("alias") == "mystery"


def test_free_versus_priced_versus_unknown(d):
    ms = by_id(parse(d))
    p = ms["gemini-35-lite"].find("price")
    assert p.get("per") == "1M" and (p.text or "").strip() == ""
    assert (p.findtext("in"), p.findtext("out"), p.findtext("cache_read")) == ("0.3", "2.5", "0.03")
    assert ms["nemotron-ultra"].find("price").text == "free"  # free-tier provider, price columns NULL
    unknown = ms["mystery"].find("price")
    assert unknown.get("per") == "1M" and not (unknown.text or "").strip() and len(unknown) == 0
    priced_zero_cache = catalog_xml.model_element({"alias": "x", "provider": "a/b", "api_key_env": "K_1",
                                                   "price_in_per_mtok": 5.0, "price_out_per_mtok": 25.0,
                                                   "model_class": "OPUS"})
    assert priced_zero_cache.findtext("price/in") == "5" and priced_zero_cache.find("price/cache_read") is None


def test_context_and_max_output_are_always_present(d):
    ms = by_id(parse(d))
    m = ms["mystery"]
    assert m.find("context") is not None and m.find("max_output") is not None
    assert (m.findtext("context") or "") == "" and (m.findtext("max_output") or "") == ""
    assert b"<context />" in catalog_xml.export_xml(d.models())
    for el in ms.values():
        assert el.find("context") is not None and el.find("max_output") is not None


def test_optional_fields_and_attributes(d):
    m = by_id(parse(d))["gemini-35-lite"]
    assert m.get("enabled") == "false" and m.get("status") == "unknown"
    assert (m.findtext("thinking"), m.findtext("timeout"), m.findtext("toolshim")) == ("off", "600", "on")
    assert by_id(parse(d))["mystery"].findtext("api_base") == "https://example.org/v1"
    assert by_id(parse(d))["opus"].find("api_base") is None and by_id(parse(d))["opus"].find("thinking") is None


def test_no_key_value_ever(tmp_path):
    d = kdb.Database(tmp_path / "kiko.db")
    d.upsert_model("ok", "nvidia/x", "NVIDIA_API_KEY")
    # a legacy row whose api_key_env holds a key VALUE (kiko-full wrote it): hidden, never exported
    d.upsert_model("legacy", "nvidia/y", SECRET)
    data = catalog_xml.export_xml(d.models())
    assert SECRET.encode() not in data and b"SECRETVALUE" not in data
    assert by_id(ET.fromstring(data))["legacy"].findtext("key") == HIDDEN
    assert b"NVIDIA_API_KEY" in data


def test_xsd_is_well_formed_and_names_every_exported_element(d):
    if not (ROOT / "docs" / "schema" / "kiko-models.xsd").is_file():
        pytest.skip("docs/schema is repo-level, not shipped in the kiko-lite zip")
    xsd = ET.parse(ROOT / "docs" / "schema" / "kiko-models.xsd").getroot()
    declared = {e.get("name") for e in xsd.iter("{http://www.w3.org/2001/XMLSchema}element")}
    exported = {el.tag for m in parse(d).findall("model") for el in m.iter()} | {"kiko-models"}
    assert exported <= declared | {"role", "in", "out", "cache_read"}, exported - declared


# ---- CLI and gateway endpoint ---------------------------------------------------------------------------

def test_cli_export_models_to_stdout_and_file(d, tmp_path, capsys):
    code = cli.main(["export-models", "--xml", "--db", str(d.path)])
    out = capsys.readouterr()
    assert code == 0 and ET.fromstring(out.out.encode("utf-8")).tag == "kiko-models"
    target = tmp_path / "out" / "models.xml"
    assert cli.main(["export-models", "--xml", "--out", str(target), "--db", str(d.path)]) == 0
    assert target.read_bytes() == catalog_xml.export_xml(d.models())
    assert cli.main(["export-models", "--db", str(d.path)]) == 2  # the format must be named


TOKEN = "management-token-1234567890"


def get(srv, path, headers):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        return r.status, r.getheader("Content-Type"), r.read()
    finally:
        c.close()


def test_gateway_endpoint_same_auth_rules_as_management(d, tmp_path):
    app = build_db_app(d, None, secret=lambda n: None, auth=False)
    management.install(app, d, d.path, 4000, token=TOKEN)
    s = server.Server(app, port=0).start()
    try:
        status, ctype, body = get(s, "/kiko/v1/models.xml", {})
        assert status == 401 and b"SECRET" not in body
        status, ctype, body = get(s, "/kiko/v1/models.xml", {"Authorization": "Bearer wrong"})
        assert status == 401
        status, ctype, body = get(s, "/kiko/v1/models.xml", {"Authorization": "Bearer " + TOKEN})
        assert status == 200 and ctype.startswith("application/xml")
        assert body == catalog_xml.export_xml(d.models())
        status, ctype, body = get(s, "/kiko/v1/models", {"Authorization": "Bearer " + TOKEN})
        assert status == 200 and json.loads(body)["models"]  # the JSON route is unchanged
    finally:
        s.stop()
