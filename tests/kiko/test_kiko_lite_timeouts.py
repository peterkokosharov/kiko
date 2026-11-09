# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #131 (the web-page tests are in test_web_timeouts.py: web/ is not in the lite zip): configurable upstream and probe timeouts and the per-model override: kiko.toml / flag precedence,
schema migration, CLI, web form, live requests, streaming (idle, not total), probes. Fully offline: the fake upstreams
are kikolib.server Apps on 127.0.0.1:0; the config dir and the DB are in tmp."""
import json
import sqlite3
import threading
import time

import pytest

from kiko_lite import app as lite
from kiko_lite import cli
from kiko_lite import db as kdb
from kiko_lite.db import Database
from kiko_lite.resolver import build_db_app
from kikolib import config
from kikolib import http as khttp
from kikolib import retry, server
from kikolib import server as kserver

from .test_kiko_lite_cli import run, store  # noqa: F401  (the cli fixtures)

MASTER = "master-key"
SECRET = "UPSTREAM-SECRET-KEY-9876"


# ---- config: kiko.toml and the serve flag ----

def toml(text):
    config.toml_path().write_text(text, encoding="utf-8")


def test_defaults_and_toml_values():
    assert config.value("proxy.upstream_timeout") == 300.0 and config.value("proxy.probe_timeout") == 20.0
    toml("[proxy]\nupstream_timeout = 650\nprobe_timeout = 45.5\n")
    assert config.value("proxy.upstream_timeout") == 650.0 and config.value("proxy.probe_timeout") == 45.5
    assert config.effective("proxy.upstream_timeout")[1] == "file"
    settings = config.resolved()["settings"]
    assert settings["proxy.upstream_timeout"] == 650.0 and settings["proxy.probe_timeout"] == 45.5


@pytest.mark.parametrize("bad", ["0", "-5", "86401", '"abc"', "false"])
def test_bad_toml_value_reads_as_default(bad):
    toml(f"[proxy]\nupstream_timeout = {bad}\nprobe_timeout = {bad}\n")
    assert config.value("proxy.upstream_timeout") == 300.0 and config.value("proxy.probe_timeout") == 20.0


def serve_gateway(monkeypatch, monkeypatch_env=None, *argv):
    seen = []
    monkeypatch.setattr(kserver, "serve", lambda app, host, port, **k: seen.append(app.gateway))
    monkeypatch.setenv("MASTER_KEY", MASTER)
    assert cli.main(["serve", *argv]) == 0
    return seen[-1]


def test_serve_reads_toml_and_the_flag_overrides_it(monkeypatch):
    gw = serve_gateway(monkeypatch)
    assert (gw.timeout, gw.probe_timeout) == (300.0, 20.0)  # default
    toml("[proxy]\nupstream_timeout = 700\nprobe_timeout = 33\n")
    gw = serve_gateway(monkeypatch)
    assert (gw.timeout, gw.probe_timeout) == (700.0, 33.0)  # toml
    gw = serve_gateway(monkeypatch, None, "--upstream-timeout", "900")
    assert (gw.timeout, gw.probe_timeout) == (900.0, 33.0)  # flag > toml; the probe timeout has no flag


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "inf", "99999"])
def test_serve_flag_is_validated(monkeypatch, capsys, bad):
    called = []
    monkeypatch.setattr(kserver, "serve", lambda *a, **k: called.append(a))
    monkeypatch.setenv("MASTER_KEY", MASTER)
    assert cli.main(["serve", "--upstream-timeout", bad]) == 2 and not called
    assert "upstream_timeout" in capsys.readouterr().err


def test_config_json_shows_the_settings(run):
    code, out, _ = run("config", "--json", db=False)
    assert code == 0
    s = json.loads(out)["settings"]
    assert s["proxy.upstream_timeout"] == 300.0 and s["proxy.probe_timeout"] == 20.0


# ---- DB: column, migration, CLI ----

def test_schema_is_at_least_7_and_has_the_column(tmp_path):
    d = Database(tmp_path / "k.db")
    assert kdb.SCHEMA_VERSION >= 7 and d.schema_version() == kdb.SCHEMA_VERSION
    d.upsert_model("m", "acme/x", "KEY_X", api_base="http://127.0.0.1:1/v1")
    assert d.get_model("m")["timeout"] is None


def test_v6_db_with_rows_migrates_and_keeps_them(tmp_path):
    p = tmp_path / "v6.db"
    d = Database(p)
    d.upsert_model("keep", "acme/x", "KEY_X", api_base="http://127.0.0.1:1/v1", model_class="HAIKU", priority=3,
                   thinking="off", price_in=1.5)
    d.upsert_model("two", "acme/y", "KEY_Y")
    con = sqlite3.connect(p)
    con.execute("ALTER TABLE models DROP COLUMN timeout")  # the v6 shape
    con.execute("PRAGMA user_version=6")
    con.commit()
    con.close()

    def columns():
        c = sqlite3.connect(p)
        try:
            return {r[1] for r in c.execute("PRAGMA table_info(models)")}
        finally:
            c.close()

    assert "timeout" not in columns()
    ro = Database(p, readonly=True)  # a read-only open of a v6 file still lists the rows (no timeout key)
    assert {m["alias"] for m in ro.models()} == {"keep", "two"}
    d2 = Database(p)
    assert d2.schema_version() == kdb.SCHEMA_VERSION and "timeout" in columns()
    rows = {m["alias"]: m for m in d2.models()}
    assert rows["keep"]["priority"] == 3 and rows["keep"]["thinking"] == "off" and rows["keep"]["price_in_per_mtok"] == 1.5
    assert rows["keep"]["timeout"] is None and rows["two"]["timeout"] is None


def test_two_threads_opening_a_v6_db_at_once(tmp_path):
    p = tmp_path / "race.db"
    d = Database(p)
    d.upsert_model("m", "acme/x", "KEY_X")
    con = sqlite3.connect(p)
    con.execute("ALTER TABLE models DROP COLUMN timeout")
    con.execute("PRAGMA user_version=6")
    con.commit()
    con.close()
    errors, barrier = [], threading.Barrier(4)

    def opener():
        try:
            barrier.wait()
            assert Database(p).schema_version() == kdb.SCHEMA_VERSION
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=opener) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and Database(p).get_model("m")["timeout"] is None


def test_upsert_keeps_sets_and_clears_the_model_timeout(tmp_path):
    d = Database(tmp_path / "k.db")
    kw = dict(api_base="http://127.0.0.1:1/v1")
    d.upsert_model("m", "acme/x", "KEY_X", timeout=600, **kw)
    assert d.get_model("m")["timeout"] == 600.0
    d.upsert_model("m", "acme/x", "KEY_X", **kw)  # omitted: kept
    assert d.get_model("m")["timeout"] == 600.0
    d.upsert_model("m", "acme/x", "KEY_X", timeout="90.5", **kw)
    assert d.get_model("m")["timeout"] == 90.5
    d.upsert_model("m", "acme/x", "KEY_X", timeout=0, **kw)  # 0 clears
    assert d.get_model("m")["timeout"] is None
    d.upsert_model("m", "acme/x", "KEY_X", timeout=5, **kw)
    d.upsert_model("m", "acme/x", "KEY_X", timeout="default", **kw)  # "default" clears
    assert d.get_model("m")["timeout"] is None
    for bad in (-1, "abc", float("inf"), float("nan"), 86401, True):
        with pytest.raises(ValueError):
            d.upsert_model("m", "acme/x", "KEY_X", timeout=bad, **kw)
        with pytest.raises(ValueError):
            d.set_timeout("m", bad)


def test_cli_add_model_set_timeout_and_list(run):
    base = ("add-model", "nim", "--provider", "nvidia/nvidia/nemotron-x", "--api-key-env", "NVIDIA_API_KEY")
    assert run(*base)[0] == 0 and Database(run.path).get_model("nim")["timeout"] is None
    assert "timeout=" not in run("list-models")[1]
    assert run(*base, "--timeout", "600")[0] == 0 and Database(run.path).get_model("nim")["timeout"] == 600.0
    assert "timeout=600s" in run("list-models")[1]
    assert run(*base)[0] == 0 and Database(run.path).get_model("nim")["timeout"] == 600.0  # kept
    assert run("set-timeout", "nim", "900")[0] == 0 and Database(run.path).get_model("nim")["timeout"] == 900.0
    assert run("set-timeout", "nim", "default")[0] == 0 and Database(run.path).get_model("nim")["timeout"] is None
    assert run("set-timeout", "nim", "120")[0] == 0 and run("set-timeout", "nim", "0")[0] == 0
    assert Database(run.path).get_model("nim")["timeout"] is None
    assert run(*base, "--timeout", "500")[0] == 0 and run(*base, "--timeout", "default")[0] == 0
    assert Database(run.path).get_model("nim")["timeout"] is None
    assert run("set-timeout", "nope", "60")[0] == 2
    for bad in ("abc", "-5", "inf", "999999"):
        assert run("set-timeout", "nim", bad)[0] == 2
        assert run(*base, "--timeout", bad)[0] == 2
    assert Database(run.path).get_model("nim")["timeout"] is None


# ---- the gateway: a stub upstream that delays ----

class Slow:
    """OpenAI-compatible stub: waits ``delay`` before the response head; ``frames`` > 0 streams that many SSE frames
    ``gap`` seconds apart (the head is sent at once)."""

    def __init__(self, delay=0.0, text="hi"):
        self.delay, self.text, self.gap, self.frames, self.hits = delay, text, 0.0, 0, 0
        app = server.App()
        app.route("POST", "/v1/chat/completions")(self._play)
        self.server = server.Server(app, port=0).start()

    @property
    def base(self):
        return self.server.url + "/v1"

    def _play(self, req):
        self.hits += 1
        body = req.json()
        if body.get("stream") and self.frames:
            def gen():
                for i in range(self.frames):
                    time.sleep(self.gap)
                    yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": f"w{i} "}}]}) + "\n\n"
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
                yield "data: [DONE]\n\n"
            return server.StreamResponse(gen())
        time.sleep(self.delay)
        return server.JsonResponse({"choices": [{"message": {"content": self.text}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    db = Database(tmp_path / "k.db")
    stubs = []
    clients = []

    class W:
        pass

    w = W()
    w.db, w.stubs = db, stubs

    def stub(**kw):
        s = Slow(**kw)
        stubs.append(s)
        return s

    def start(upstream_timeout=0.5, probe_timeout=0.5):
        policy = retry.RetryPolicy(attempts=1, base_delay=0.01, max_delay=0.02, budget=5.0, jitter=False)
        app = build_db_app(db, MASTER, secret={"KEY_A": SECRET, "KEY_B": SECRET}.get, env={}, ttl=0.0, policy=policy,
                           ping_interval=0.2, upstream_timeout=upstream_timeout, probe_timeout=probe_timeout)
        srv = server.Server(app, port=0).start()
        c = khttp.StdlibBackend()
        clients.append((srv, c))
        w.app = app

        def post(payload, stream=False):
            h = {"x-api-key": MASTER, "content-type": "application/json"}
            return c.request("POST", srv.url + "/v1/messages", h, json.dumps(payload).encode(), stream=stream)
        w.post = post
        return app

    w.stub, w.start = stub, start
    yield w
    for srv, c in clients:
        c.close()
        srv.stop()
    for s in stubs:
        s.server.stop()


def req(**kw):
    return {"model": "claude-haiku-9", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}], **kw}


def add(w, alias, stub, prio, **kw):
    w.db.upsert_model(alias, f"acme/{alias}-model", "KEY_" + alias.upper(), api_base=stub.base, model_class="HAIKU",
                      priority=prio, **kw)


def test_model_timeout_longer_than_the_global_one_lets_a_slow_head_through(world):
    a = world.stub(delay=1.0, text="from a")
    b = world.stub(text="from b")
    add(world, "a", a, 1, timeout=5)  # global 0.5 s, this model 5 s, the head comes after 1 s
    add(world, "b", b, 2)
    world.start()
    r = world.post(req())
    assert r.status == 200 and r.json()["content"][0]["text"] == "from a" and b.hits == 0


def test_model_timeout_shorter_than_the_head_delay_times_out_and_fails_over(world):
    a = world.stub(delay=1.5, text="from a")
    b = world.stub(text="from b")
    add(world, "a", a, 1, timeout=0.3)  # a longer GLOBAL timeout does not save it: the model's own wins
    add(world, "b", b, 2)
    world.start(upstream_timeout=5.0)
    r = world.post(req())
    assert r.status == 200 and r.json()["content"][0]["text"] == "from b" and a.hits == 1 and b.hits == 1


def test_global_timeout_applies_without_a_model_timeout(world):
    a = world.stub(delay=1.0, text="from a")
    b = world.stub(text="from b")
    add(world, "a", a, 1)
    add(world, "b", b, 2)
    world.start(upstream_timeout=0.4)
    assert world.post(req()).json()["content"][0]["text"] == "from b"
    world.db.set_timeout("a", 5)  # set-timeout takes effect on the next request (catalog ttl 0)
    world.app.gateway.cooldowns._until.clear()  # the failure of a started a cooldown, and marked it "error"
    world.db.set_status("a", "ok")
    assert world.post(req()).json()["content"][0]["text"] == "from a"


def test_streaming_is_cut_by_silence_not_by_total_time(world):
    a = world.stub()
    a.frames, a.gap = 6, 0.3  # 6 frames x 0.3 s = 1.8 s in total; the timeout is 0.8 s: every gap is shorter
    add(world, "a", a, 1)
    world.start(upstream_timeout=0.8)
    r = world.post(req(stream=True), stream=True)
    text = "".join(line for line in r.iter_lines())
    assert r.status == 200 and "w0" in text and "w5" in text and "message_stop" in text
    a.gap = 1.2  # a gap longer than the timeout: the stream is cut (an error event, not an endless wait)
    r = world.post(req(stream=True), stream=True)
    text = "".join(line for line in r.iter_lines())
    assert "w5" not in text


def test_probe_uses_probe_timeout_or_the_model_timeout(world):
    slow = world.stub(delay=1.0)
    own = world.stub(delay=1.0)
    add(world, "a", slow, 1)
    add(world, "b", own, 2, timeout=5)
    gw = world.start(probe_timeout=0.3).gateway
    assert gw.probe("db/a", "acme/a-model") is False  # probe_timeout 0.3 s < the 1 s head delay
    assert gw.probe("db/b", "acme/b-model") is True  # the model's own 5 s wins over probe_timeout
    world.db.set_timeout("a", 5)
    gw.cooldowns._until.clear()  # the failed probe started a cooldown
    assert gw.probe("db/a", "acme/a-model") is True
    assert gw.probe("db/b", "acme/b-model", 0.2) is False  # an explicit argument still wins


def test_probe_timeout_of_a_model_is_capped(world):
    gw = world.start(probe_timeout=20).gateway
    assert gw._probe_timeout(None) == 20.0
    assert gw._probe_timeout(5) == 5.0
    assert gw._probe_timeout(86400) == 120.0  # the cap
    gw.probe_timeout = 300.0
    assert gw._probe_timeout(86400) == 300.0  # a larger probe_timeout raises the cap


def test_a_bad_stored_timeout_does_not_crash_listing(run):
    base = ("add-model", "nim", "--provider", "nvidia/nvidia/nemotron-x", "--api-key-env", "NVIDIA_API_KEY")
    assert run(*base, "--timeout", "600")[0] == 0
    assert kdb.fmt_timeout(600.0) == "600" and kdb.fmt_timeout(None) == "" and kdb.fmt_timeout("junk") == ""
    con = sqlite3.connect(run.path)
    con.execute("UPDATE models SET timeout='junk'")
    con.commit()
    con.close()
    code, out, _ = run("list-models")
    assert code == 0 and "nim" in out and "timeout=" not in out


def test_defaults_of_build_app():
    app = lite.build_app(lambda *a: None, MASTER)
    assert app.gateway.timeout == 300.0 and app.gateway.probe_timeout == 20.0
