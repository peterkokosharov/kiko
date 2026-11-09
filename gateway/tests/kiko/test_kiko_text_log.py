# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #128: every /v1/messages exchange is stored in ``responses`` (linked to its billing row) and the web Text log
shows it. Real path through the proxy server with a fake upstream; temp DB and config dir only."""
import io
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from kiko_lite import app as lite
from kiko_lite import db as kdb
from kiko_lite.resolver import AsyncWriter, build_db_app
from kikolib import config
from kikolib import http as khttp
from kikolib import retry, server, tomlwrite
from web import security
from web.ui import WebUI

from .test_web_ui import PASSWORD, Client

MASTER = "master-key"
UPSTREAM_KEY = "UPKEY-VALUE-9876"
HOSTILE = '"><script>alert(1)</script>'


def sse(*chunks):
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def chunk(delta=None, finish=None, usage=None):
    c = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


class FakeHttp:
    """Scripted upstream: items are (status, json-able) or (status, bytes, 'sse')."""

    def __init__(self):
        self.calls, self.script = [], []

    def request(self, method, url, headers=None, body=None, timeout=0, stream=False):
        self.calls.append({"url": url, "headers": dict(headers or {}), "body": json.loads(body or b"{}")})
        item = self.script.pop(0)
        if len(item) == 3:
            raw, ctype = item[1], "text/event-stream"
        else:
            raw, ctype = json.dumps(item[1]).encode(), "application/json"
        return khttp.Response(item[0], "x", khttp.Headers([("content-type", ctype)]), io.BytesIO(raw))

    def close(self):
        pass


OK = (200, {"choices": [{"message": {"content": "the answer <b>42</b>"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
BODY = {"model": "claude-sonnet-4", "max_tokens": 10, "system": "be brief", "temperature": 0.5,
        "messages": [{"role": "user", "content": "what is six times seven?"}],
        "tools": [{"name": "calc", "description": "d", "input_schema": {"type": "object"}}]}


@pytest.fixture
def db(tmp_path):
    return kdb.Database(tmp_path / "t" / "kiko.db")


@pytest.fixture
def wired(db, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    fake = FakeHttp()
    db.upsert_model("cheap", "openai/some-model", "MY_UP_KEY", "https://llm.example.org/v1", "SONNET", 1,
                    2.0, 8.0, status="ok")
    policy = retry.RetryPolicy(attempts=1, base_delay=0.01, max_delay=0.02, budget=1.0, jitter=False)
    app = build_db_app(db, MASTER, secret={"MY_UP_KEY": UPSTREAM_KEY}.get, env={}, ttl=0.0, http=fake,
                       policy=policy, ping_interval=0.2, upstream_timeout=2.0)
    srv = server.Server(app, port=0).start()  # port 0: an ephemeral port, never 4000/4001
    client = khttp.StdlibBackend()

    def post(body=None, headers=None):
        h = {"x-api-key": MASTER, "content-type": "application/json", **(headers or {})}
        r = client.request("POST", srv.url + "/v1/messages", h, json.dumps(body or BODY).encode())
        with r:
            r.read()
        return r

    yield type("W", (), {"db": db, "fake": fake, "app": app, "post": staticmethod(post)})
    app.results.stop(0.2)
    client.close()
    srv.stop()


def table(db, name):
    con = sqlite3.connect(db.path)
    con.row_factory = sqlite3.Row
    try:
        for _ in range(50):  # a stream writes its rows when the generator ends
            rows = [dict(r) for r in con.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            if rows:
                return rows
            time.sleep(0.05)
        return rows
    finally:
        con.close()


def test_nonstream_stored_and_linked_to_billing(wired):
    wired.fake.script.append(OK)
    assert wired.post().status == 200
    (resp,) = table(wired.db, "responses")
    (bill,) = table(wired.db, "billing")
    assert bill["request_id"] == resp["id"] and len(resp["id"]) == 16
    sent = json.loads(resp["input_json"])
    assert sent["model"] == "claude-sonnet-4" and sent["system"] == "be brief" and sent["temperature"] == 0.5
    assert sent["messages"][0]["content"] == "what is six times seven?" and sent["tools"][0]["name"] == "calc"
    out = json.loads(resp["output_json"])
    assert out["content"] == [{"type": "text", "text": "the answer <b>42</b>"}] and out["stop_reason"] == "end_turn"


def test_stream_assembled_text_and_tool_use(wired):
    tool = {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                            "function": {"name": "calc", "arguments": '{"a":'}}]}
    tool2 = {"tool_calls": [{"index": 0, "function": {"arguments": "6}"}}]}
    wired.fake.script.append((200, sse(chunk({"content": "Hel"}), chunk({"content": "lo"}), chunk(tool), chunk(tool2),
                                       chunk(finish="tool_calls",
                                             usage={"prompt_tokens": 4, "completion_tokens": 3})), "sse"))
    assert wired.post({**BODY, "stream": True}).status == 200
    (resp,) = table(wired.db, "responses")
    (bill,) = table(wired.db, "billing")
    assert bill["request_id"] == resp["id"]
    out = json.loads(resp["output_json"])
    texts = [b["text"] for b in out["content"] if b["type"] == "text"]
    uses = [b for b in out["content"] if b["type"] == "tool_use"]
    assert texts == ["Hello"] and uses[0]["name"] == "calc" and uses[0]["input"] == {"a": 6}
    assert out["stop_reason"] == "tool_use"
    assert json.loads(resp["input_json"])["stream"] is True


def test_upstream_error_stored_with_error_body(wired):
    wired.fake.script.append((400, {"error": {"message": "bad thing happened"}}))
    assert wired.post().status == 400
    (resp,) = table(wired.db, "responses")
    out = json.loads(resp["output_json"])
    assert out["type"] == "error" and "bad thing happened" in out["error"]["message"]
    (bill,) = table(wired.db, "billing")
    assert bill["request_id"] == resp["id"] and bill["error"]


def test_refused_before_upstream_is_stored(wired):
    assert wired.post({"model": "claude-sonnet-4", "messages": "nope"}).status == 400
    (resp,) = table(wired.db, "responses")
    assert json.loads(resp["output_json"])["type"] == "error"


def test_store_text_false_stores_nothing(wired):
    tomlwrite.set_value(config.toml_path(), "logs", "store_text", False)
    wired.fake.script.append(OK)
    assert wired.post().status == 200
    assert len(table(wired.db, "billing")) == 1
    assert table(wired.db, "responses") == []


def test_no_secret_in_stored_rows(wired):
    secret = "sk-ant-CLIENT-SECRET-555"
    wired.fake.script.append(OK)
    assert wired.post(headers={"authorization": "Bearer " + secret, "x-api-key": MASTER}).status == 200
    wired.fake.script.append(OK)
    assert wired.post(headers={"authorization": "Bearer " + MASTER, "x-api-key": secret}).status == 401
    rows = table(wired.db, "responses")
    assert rows
    for name in ("responses", "billing"):
        for row in table(wired.db, name):
            blob = json.dumps(row).lower()
            for s in (secret, MASTER, UPSTREAM_KEY):
                assert s.lower() not in blob
    assert not any("authorization" in r["input_json"].lower() or "x-api-key" in r["input_json"].lower()
                   for r in rows)


def test_storage_failure_does_not_break_the_request(wired, monkeypatch, caplog):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk is full")
    monkeypatch.setattr(wired.db, "add_response", boom)
    wired.fake.script.append(OK)
    assert wired.post().status == 200
    assert len(table(wired.db, "billing")) == 1
    assert sum("could not store the request text" in m for m in caplog.messages) == 1


def test_retention_prunes_responses(db):
    db.add_response("old", "{}", "{}")
    db.add_response("new", "{}", "{}")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db.path) as con:
        con.execute("UPDATE responses SET created_at=? WHERE id='old'", (old,))
    db.prune_logs(30)
    assert [r["id"] for r in db.query("SELECT id FROM responses")] == ["new"]


def test_existing_v4_db_gets_the_request_id_column(tmp_path):
    p = tmp_path / "kiko.db"
    kdb.Database(p)
    con = sqlite3.connect(p)
    con.execute("DROP INDEX billing_request_id")
    con.execute("ALTER TABLE billing DROP COLUMN request_id")
    con.execute("PRAGMA user_version=4")
    con.commit()
    con.close()
    d = kdb.Database(p)
    d.add_billing("m", 1, 1, 0.0, request_id="abc")
    assert d.query("SELECT request_id FROM billing")[0]["request_id"] == "abc" and d.schema_version() == kdb.SCHEMA_VERSION


# ---- the web Text log ----

@pytest.fixture
def web(wired, tmp_path):
    wired.db.set_setting(security.HASH_KEY, security.hash_password(PASSWORD, iterations=1000))
    ui = WebUI(wired.db, password_file=tmp_path / "pw")
    srv = server.Server(ui.build_app(), "127.0.0.1", 0)
    srv.start()
    c = Client(srv.port)
    assert c.login()[0].status == 303
    yield wired, c
    srv.request_stop()
    srv.close()


def test_text_log_list_and_detail_through_the_proxy(web):
    wired, c = web
    wired.fake.script.append(OK)
    wired.post()
    (resp,) = table(wired.db, "responses")
    status, html = c.get("/responses")
    assert status.status == 200
    for want in ("claude-sonnet-4", "custom/some-model", "what is six times seven?", "the answer &lt;b&gt;42&lt;/b&gt;",
                 "ok", f'href="/responses/{resp["id"]}"'):
        assert want in html
    status, html = c.get("/responses/" + resp["id"])
    assert status.status == 200
    for want in ("be brief", "calc", "what is six times seven?", "the answer &lt;b&gt;42&lt;/b&gt;", "temperature"):
        assert want in html
    assert "<b>42</b>" not in html
    assert c.get("/responses/nope")[0].status == 404
    assert c.get("/responses?limit=1&offset=0")[0].status == 200


def test_text_log_escapes_hostile_content(web):
    wired, c = web
    bad = {"model": HOSTILE, "max_tokens": 5, "messages": [{"role": "user", "content": HOSTILE}]}
    wired.db.add_response('a"b', json.dumps(bad), json.dumps({"content": [{"type": "text", "text": HOSTILE}]}))
    wired.db.add_response("legacy", HOSTILE, HOSTILE)  # kiko-full rows hold plain text
    for path in ("/responses", "/responses/a%22b", "/responses/legacy"):
        status, html = c.get(path)
        assert status.status == 200 and "<script>alert(1)" not in html and "&lt;script&gt;" in html


# ---- fix round 1: off the request path, partial answers, unexpected errors, probes, disable_billing ----

def test_slow_database_does_not_slow_the_request(wired, monkeypatch):
    def slow(*a, **k):
        time.sleep(3)
    monkeypatch.setattr(wired.db, "add_billing", slow)
    monkeypatch.setattr(wired.db, "add_response", slow)
    wired.app.results.start()
    wired.fake.script.append(OK)
    t0 = time.monotonic()
    assert wired.post().status == 200
    assert time.monotonic() - t0 < 1.5
    wired.fake.script.append((200, sse(chunk({"content": "x"}), chunk(finish="stop")), "sse"))
    t0 = time.monotonic()
    assert wired.post({**BODY, "stream": True}).status == 200  # post() reads the stream to its end
    assert time.monotonic() - t0 < 1.5


def test_full_queue_never_blocks_and_keeps_every_result():
    gate, seen = [], []

    def slow(res):  # only the writer thread is stuck; an inline write from the request thread goes through
        if threading.current_thread().name == "kiko-log-writer":
            while not gate:
                time.sleep(0.01)
        seen.append(res)
    w = AsyncWriter(slow, maxsize=1)
    w.start()
    try:
        t0 = time.monotonic()
        for i in range(5):
            w(lite.RequestResult(request_id=str(i), client_model="m"))
        assert time.monotonic() - t0 < 0.5 and w.dropped >= 3
        assert len(seen) >= 3  # the overflow was written inline, none lost
    finally:
        gate.append(1)
        w.stop(2.0)
    assert sorted(r.request_id for r in seen) == [str(i) for i in range(5)]


def test_async_writer_flushes_on_stop(db):
    seen = []
    w = AsyncWriter(seen.append)
    w.start()
    for i in range(20):
        w(lite.RequestResult(request_id=str(i), client_model="m"))
    w.stop(5.0)
    assert [r.request_id for r in seen] == [str(i) for i in range(20)]


def test_midstream_error_keeps_the_partial_answer(wired):
    raw = ("data: " + json.dumps(chunk({"content": "partial words"})) + "\n\n").encode()  # then EOF: cut
    wired.fake.script.append((200, raw, "sse"))
    wired.post({**BODY, "stream": True})
    (resp,) = table(wired.db, "responses")
    out = json.loads(resp["output_json"])
    assert out["content"][0]["text"] == "partial words" and out["error"]["type"] == "api_error"


def test_unexpected_error_in_the_nonstream_path_is_stored(wired, monkeypatch):
    def bug(*a, **k):
        raise RuntimeError("boom sk-secret")
    monkeypatch.setattr(lite._Gateway, "_parsed", bug)
    wired.fake.script.append(OK)
    assert wired.post().status == 500
    (resp,) = table(wired.db, "responses")
    out = json.loads(resp["output_json"])
    assert out["type"] == "error" and "RuntimeError" in out["error"]["message"] and "sk-secret" not in resp["output_json"]


def test_disable_billing_still_stores_text(wired):
    wired.db.set_setting("disable_billing", "true")
    wired.fake.script.append(OK)
    assert wired.post().status == 200
    assert len(table(wired.db, "responses")) == 1
    assert wired.db.query("SELECT COUNT(*) AS n FROM billing")[0]["n"] == 0


def test_health_probe_is_not_stored(wired):
    wired.fake.script.append(OK)
    assert wired.app.gateway.probe("db/cheap", "some-model") is True
    (bill,) = table(wired.db, "billing")
    assert bill["route"] == "probe"
    assert wired.db.query("SELECT COUNT(*) AS n FROM responses")[0]["n"] == 0


def test_indexes_exist(db):
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"billing_request_id", "responses_created_at"} <= names
