# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko_lite failover, cooldown and health prober, fully offline: fake upstreams are kikolib.server Apps on
127.0.0.1:0 that play scripted OpenAI-compatible responses."""
import json
import logging
import sqlite3
import threading
import time

import pytest

from kiko_lite import app as lite
from kiko_lite import health
from kiko_lite.db import Database
from kiko_lite.resolver import Catalog, DBResolver, build_db_app
from kikolib import http as khttp
from kikolib import retry, server, sse

MASTER = "master-key"
SECRET = "UPSTREAM-SECRET-KEY-9876"


class Fake:
    """One scripted upstream; every request body is recorded."""

    def __init__(self):
        self.script = []
        self.requests = []
        self.lock = threading.Lock()
        app = server.App()
        app.route("POST", "/v1/chat/completions")(self._play)
        self.server = server.Server(app, port=0).start()

    @property
    def base(self):
        return self.server.url + "/v1"

    def add(self, **item):
        self.script.append(item)

    def _play(self, req):
        with self.lock:
            self.requests.append(req.json())
            item = self.script.pop(0) if self.script else {"status": 500, "json": {"error": "empty script"}}
        if "sse" in item:
            frames = [f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n" for f in item["sse"]]
            return server.StreamResponse(iter(frames), item.get("status", 200))
        return server.JsonResponse(item.get("json", {}), item.get("status", 200), item.get("headers"))


def ok(text="hello"):
    return {"json": {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 2}}}


def chunk(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def body(**kw):
    return {"model": "claude-haiku-9", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}], **kw}


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    a, b, c, d = Fake(), Fake(), Fake(), Fake()
    db = Database(tmp_path / "k.db")
    for alias, fake, prio in (("a", a, 1), ("b", b, 2), ("c", c, 3), ("d", d, 4)):
        db.upsert_model(alias, f"acme/{alias}-model", "KEY_" + alias.upper(), api_base=fake.base,
                        model_class="HAIKU", priority=prio)
    policy = retry.RetryPolicy(attempts=2, base_delay=0.01, max_delay=0.02, budget=5.0, jitter=False)
    secrets = {f"KEY_{n}": SECRET for n in "ABCD"}
    app = build_db_app(db, MASTER, secret=secrets.get, env={}, ttl=0.0, policy=policy, ping_interval=0.2,
                       upstream_timeout=2.0)
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()

    class W:
        pass

    w = W()
    w.a, w.b, w.c, w.d, w.db, w.app, w.url = a, b, c, d, db, app, srv.url

    def post(payload, stream=False):
        h = {"x-api-key": MASTER, "content-type": "application/json"}
        return client.request("POST", srv.url + "/v1/messages", h, json.dumps(payload).encode(), stream=stream)

    def billing():
        con = sqlite3.connect(db.path)
        try:
            for _ in range(50):  # a stream writes its row when the generator ends
                rows = con.execute("SELECT upstream_model, attempts, error FROM billing ORDER BY id").fetchall()
                if rows:
                    return rows
                time.sleep(0.05)
            return []
        finally:
            con.close()

    w.post, w.billing = post, billing
    yield w
    client.close()
    srv.stop()
    for f in (a, b, c, d):
        f.server.stop()


def status(w, alias):
    return w.db.get_model(alias)["status"]


# ---- failover ----

@pytest.mark.parametrize("code", [429, 503, 500, 408])
def test_failover_to_next_candidate_and_billing_records_it(world, code):
    world.a.add(status=code, json={"error": {"message": "busy"}})
    world.b.add(**ok("from b"))
    r = world.post(body())
    assert r.status == 200
    assert r.json()["content"][0]["text"] == "from b"
    assert len(world.a.requests) == 1 and len(world.b.requests) == 1
    rows = world.billing()
    assert rows[0][0] == "custom/acme/b-model"  # the model actually used
    assert rows[0][1] == 2 and rows[0][2] is None  # two upstream calls, no error
    assert status(world, "a") == ("unknown" if code == 429 else "error") and status(world, "b") == "ok"


@pytest.mark.parametrize("code", [401, 403, 404])
@pytest.mark.parametrize("stream", [False, True])
def test_failover_on_broken_model_marks_it_error(world, code, stream):
    world.a.add(status=code, json={"error": {"message": "nope " + SECRET}})
    if stream:
        world.b.add(sse=[chunk({"content": "from b"}), chunk({}, "stop"), "[DONE]"])
    else:
        world.b.add(**ok("from b"))
    r = world.post(body(stream=stream), stream=stream)
    assert r.status == 200
    if stream:
        with r:
            evs = [(e.event, json.loads(e.data)) for e in sse.iter_events(r.iter_lines())]
        assert "error" not in [n for n, _ in evs]
    else:
        assert r.json()["content"][0]["text"] == "from b"
    assert status(world, "a") == "error" and status(world, "b") == "ok"
    world.b.add(**ok("again"))
    if not stream:
        assert world.post(body()).status == 200
        assert len(world.a.requests) == 1  # cooling down: not asked again
    assert len(world.billing()) >= 1


def test_explicit_model_with_bad_key_gives_clean_error(world):
    world.a.add(status=401, json={"error": {"message": "bad " + SECRET}})
    r = world.post(body(model="a"))
    assert r.status == 502
    assert "rejected the configured API key" in r.json()["error"]["message"]
    assert SECRET not in json.dumps(r.json())
    assert world.b.requests == []


def test_all_candidates_failing_prefers_the_transient_error(world):
    world.a.add(status=503, json={"error": "down"}, headers={"Retry-After": "7"})
    world.b.add(status=401, json={"error": "bad key"})
    world.c.add(status=401, json={"error": "bad key"})
    r = world.post(body())
    assert r.status == 529


def test_failover_on_connection_error(world):
    world.a.server.stop()  # nothing listens there any more
    world.b.add(**ok("from b"))
    r = world.post(body())
    assert r.status == 200 and r.json()["content"][0]["text"] == "from b"


def test_no_failover_on_400(world):
    world.a.add(status=400, json={"error": {"message": "bad field"}})
    world.b.add(**ok())
    r = world.post(body())
    assert r.status == 400
    assert world.b.requests == []
    assert status(world, "a") == "unknown"  # a client error says nothing about the model


def test_attempts_are_capped(world):
    for f in (world.a, world.b, world.c, world.d):
        for _ in range(3):
            f.add(status=503, json={"error": "down"})
    r = world.post(body())
    assert r.status == 529
    assert world.d.requests == []  # MAX_ATTEMPTS = 3 candidates
    assert len(world.a.requests) == 1 and len(world.b.requests) == 1  # earlier ones fail over at once


def test_error_row_is_tried_last_and_recovers_after_success(world):
    world.db.set_status("a", "error")
    world.db.set_enabled("d", False)  # keeps a inside the 3 candidates
    world.b.add(status=503, json={"error": "down"})
    world.c.add(status=503, json={"error": "down"})
    world.a.add(**ok("from a"))
    r = world.post(body())
    assert r.status == 200 and r.json()["content"][0]["text"] == "from a"
    assert len(world.b.requests) == 1 and len(world.c.requests) == 1  # a was last in line
    assert status(world, "a") == "ok"
    assert status(world, "b") == "error"


def test_ranking_ok_then_unknown_then_error(world):
    world.db.set_status("c", "ok")
    world.db.set_status("a", "error")
    world.app.health.catalog.invalidate()
    snap = world.app.health.catalog.snapshot()
    assert [m.row["alias"] for m in snap.ranked("HAIKU")] == ["c", "b", "d", "a"]


def test_all_failing_gives_overloaded_with_retry_after(world):
    for f in (world.a, world.b, world.c):
        for _ in range(3):
            f.add(status=503, json={"error": "down"}, headers={"Retry-After": "7"})
    r = world.post(body())
    assert r.status == 529 and r.headers.get("Retry-After") == "7"


# ---- streaming ----

def test_stream_fails_over_before_first_byte(world):
    world.a.add(status=503, json={"error": "down"})
    world.b.add(sse=[chunk({"content": "hi b"}), chunk({}, "stop"), "[DONE]"])
    r = world.post(body(stream=True), stream=True)
    assert r.status == 200
    with r:
        evs = [(e.event, json.loads(e.data)) for e in sse.iter_events(r.iter_lines())]
    text = "".join(d["delta"].get("text", "") for n, d in evs if n == "content_block_delta")
    assert text == "hi b" and "error" not in [n for n, _ in evs]
    assert len(world.a.requests) == 1 and len(world.b.requests) == 1
    assert world.billing()[0][0] == "custom/acme/b-model"


def test_stream_does_not_fail_over_after_first_byte(world):
    world.a.add(sse=[chunk({"content": "partial"})])  # head 200, one chunk, then the stream just ends
    world.b.add(**ok("never"))
    r = world.post(body(stream=True), stream=True)
    with r:
        names = [e.event for e in sse.iter_events(r.iter_lines())]
    assert "error" in names
    assert world.b.requests == []


# ---- cooldown (injected clock, fixed candidate list) ----

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def pair(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    a, b = Fake(), Fake()
    table = {n: {"base_url": f.base, "kind": "openai", "key_names": ["K"], "auth": "bearer", "flags": {}}
             for n, f in (("pa", a), ("pb", b))}

    class R:
        def __call__(self, model):
            return ("pa", "ma")

        def candidates(self, model):
            return [("pa", "ma"), ("pb", "mb")]

    healths = []
    app = lite.build_app(R(), MASTER, providers=table, secret=lambda n: SECRET, on_health=lambda p, o: healths.append((p, o)),
                         policy=retry.RetryPolicy(attempts=1, jitter=False), upstream_timeout=2.0)
    clock = Clock()
    app.gateway.cooldowns.clock = clock
    srv = server.Server(app, port=0).start()
    client = khttp.StdlibBackend()

    class P:
        pass

    p = P()
    p.a, p.b, p.clock, p.app, p.healths = a, b, clock, app, healths
    p.post = lambda: client.request("POST", srv.url + "/v1/messages", {"x-api-key": MASTER, "content-type": "application/json"}, json.dumps(body()).encode())
    yield p
    client.close()
    srv.stop()
    a.server.stop()
    b.server.stop()


def test_cooldown_skips_model_until_it_expires(pair):
    pair.a.add(status=429, json={"error": "slow down"}, headers={"Retry-After": "30"})
    pair.b.add(**ok("b1"))
    assert pair.post().json()["content"][0]["text"] == "b1"
    assert len(pair.a.requests) == 1
    pair.b.add(**ok("b2"))
    assert pair.post().json()["content"][0]["text"] == "b2"
    assert len(pair.a.requests) == 1  # still cooling: not even asked
    pair.clock.t += 31
    pair.a.add(**ok("a again"))
    assert pair.post().json()["content"][0]["text"] == "a again"
    assert len(pair.a.requests) == 2
    assert pair.healths[-1] == ("pa", True)  # a success reports the model as working


def test_all_cooling_still_tries_the_one_that_recovers_first(pair):
    pair.app.gateway.cooldowns.mark(("pa", "ma"), 50)
    pair.app.gateway.cooldowns.mark(("pb", "mb"), 10)
    pair.b.add(**ok("b"))
    assert pair.post().status == 200
    assert pair.a.requests == []


def test_cooldown_seconds_sources():
    assert lite.cooldown_seconds("30") == 30
    assert lite.cooldown_seconds(None) == lite.DEFAULT_COOLDOWN
    assert lite.cooldown_seconds("garbage") == lite.DEFAULT_COOLDOWN
    assert lite.cooldown_seconds("99999") == lite.MAX_COOLDOWN
    info = json.dumps({"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}).encode()
    assert lite.cooldown_seconds(None, info) == 37
    assert lite.cooldown_seconds("5", info) == 5  # the header wins


def test_cooldowns_table_uses_the_clock():
    clock = Clock()
    cd = lite.Cooldowns(clock)
    cd.mark(("p", "m"), 10)
    assert cd.remaining(("p", "m")) == 10
    clock.t += 11
    assert cd.remaining(("p", "m")) == 0
    cd.mark(("p", "m"), 10)
    cd.clear(("p", "m"))
    assert cd.remaining(("p", "m")) == 0


# ---- health prober ----

def test_prober_writes_status_and_last_check_and_bills_the_probes_as_route_probe(world):
    world.a.add(**ok("pong"))
    world.b.add(status=500, json={"error": "down"})
    world.c.add(**ok("pong"))
    world.d.add(status=401, json={"error": "bad key"})
    res = world.app.health.probe_once()
    assert res == {"a": True, "b": False, "c": True, "d": False}
    assert [status(world, x) for x in "abcd"] == ["ok", "error", "ok", "error"]
    assert world.db.get_model("a")["last_check"]
    assert world.a.requests[0]["max_tokens"] == 1
    con = sqlite3.connect(world.db.path)
    rows = con.execute("SELECT route, tokens_in, tokens_out, cost_usd, error, attempts FROM billing ORDER BY id").fetchall()
    con.close()
    assert [r[0] for r in rows] == ["probe"] * 4  # every probe that reached upstream is billed as route "probe"
    assert rows[0][1:4] == (5, 2, 0.0)  # tokens the answer reported; the model has no price: cost 0
    assert rows[1][4] == "api_error" and rows[3][4] == "api_error" and rows[1][1] == 0  # failed probes: tokens 0
    assert {r[5] for r in rows} == {1}


def test_prober_recovers_an_error_row_and_skips_cooling_and_disabled(world):
    world.db.set_status("a", "error")
    world.app.gateway.cooldowns.mark(("db/b", "acme/b-model"), 100)
    world.db.set_enabled("c", False)
    world.a.add(**ok("pong"))
    world.d.add(**ok("pong"))
    res = world.app.health.probe_once()
    assert res == {"a": True, "d": True}
    assert status(world, "a") == "ok"
    assert world.b.requests == [] and world.c.requests == []


def test_prober_thread_starts_and_stops(world, monkeypatch):
    assert health.parse_interval(None) == 300
    assert health.parse_interval("0") == 0
    assert health.parse_interval("abc") == 300
    assert health.parse_interval("1") == health.MIN_INTERVAL
    world.db.set_setting("health_check_interval", "0")
    p = health.HealthProber(world.app.gateway, world.app.health.catalog, lambda *a: None)
    assert p.start() is False  # 0 = off
    world.db.set_setting("health_check_interval", "3600")
    world.app.health.catalog.invalidate()
    for f in (world.a, world.b, world.c, world.d):
        f.add(**ok("pong"))
    p = health.HealthProber(world.app.gateway, world.app.health.catalog, lambda *a: None)
    assert p.start() is True
    t0 = time.monotonic()
    p.stop()
    assert time.monotonic() - t0 < 5
    assert not p._thread.is_alive()


# ---- no key leaks ----

def test_key_never_in_logs_or_error_bodies(world, caplog):
    caplog.set_level(logging.DEBUG)
    for f in (world.a, world.b, world.c):
        for _ in range(3):
            f.add(status=503, json={"error": {"message": f"echoed {SECRET}"}})
    r = world.post(body())
    assert SECRET not in json.dumps(r.json())
    for f in (world.a, world.b, world.c, world.d):
        f.add(status=500, json={"error": f"echoed {SECRET}"})
    world.app.health.probe_once()
    assert SECRET not in caplog.text
    assert SECRET not in repr(world.app.gateway._plan(body()))


# ---- probe economy: probes cost quota or money, so they are rare ----

def test_gemini_retryinfo_cooldown_end_to_end(pair):
    info = {"error": {"code": 429, "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}
    pair.a.add(status=429, json=info)  # no Retry-After header: the delay is in the body
    pair.b.add(**ok("b1"))
    assert pair.post().json()["content"][0]["text"] == "b1"
    assert pair.app.gateway.cooldowns.remaining(("pa", "ma")) == 37
    pair.clock.t += 30
    pair.b.add(**ok("b2"))
    assert pair.post().status == 200 and len(pair.a.requests) == 1  # still cooling
    pair.clock.t += 8
    pair.a.add(**ok("a again"))
    assert pair.post().json()["content"][0]["text"] == "a again"


def test_live_429_sets_cooldown_only_not_error_status(world):
    world.a.add(status=429, json={"error": "slow down"})
    world.b.add(**ok("from b"))
    assert world.post(body()).status == 200
    assert world.app.gateway.cooldowns.remaining(("db/a", "acme/a-model")) > 0
    assert status(world, "a") == "unknown"


def test_probe_429_sets_cooldown_but_no_verdict(world):
    world.a.add(status=429, json={"error": "slow"})
    res = world.app.health.probe_once()
    assert "a" not in res and status(world, "a") == "unknown"
    assert world.app.gateway.cooldowns.remaining(("db/a", "acme/a-model")) > 0


def test_prober_skips_ok_and_recent_live_success(world):
    world.db.set_status("a", "ok")
    world.db.set_status("b", "ok")
    world.db.set_status("c", "ok")
    world.db.set_status("d", "ok")
    world.a.add(**ok("live"))
    assert world.post(body()).status == 200  # live success on a (status ok)
    world.db.set_status("a", "error")  # e.g. a stale status: still no probe within the interval
    world.app.health.catalog.invalidate()
    assert world.app.health.probe_once() == {}
    assert world.app.health.last_sent == 0
    assert world.b.requests == [] and len(world.a.requests) == 1
    world.app.gateway.cooldowns.clock = lambda t=time.monotonic() + 400: t  # the success is now old
    world.a.add(**ok("pong"))
    assert world.app.health.probe_once() == {"a": True}
    assert world.app.health.sent == 1


def test_prober_backoff_doubles_and_resets(world):
    for x in "bcd":
        world.db.set_enabled(x, False)
    clock = Clock()
    world.app.gateway.cooldowns.clock = clock
    p = world.app.health
    key = ("db/a", "acme/a-model")

    def tick(dt, **item):
        clock.t += dt
        world.a.add(**item)
        before = len(world.a.requests)
        p.probe_once(300)
        return len(world.a.requests) - before

    down = {"status": 503, "json": {"error": "down"}, "headers": {"Retry-After": "1"}}
    assert tick(0, **down) == 1 and p._delay[key] == 300
    assert tick(100, **down) == 0  # backing off
    assert tick(201, **down) == 1 and p._delay[key] == 600
    assert tick(300, **down) == 0
    assert tick(301, **down) == 1 and p._delay[key] == 1200
    for _ in range(10):
        p._next[key] = 0.0
        tick(2, **down)
    assert p._delay[key] == health.MAX_BACKOFF
    p._next[key] = 0.0
    world.a.script.clear()  # leftovers of probes that were skipped
    assert tick(2, **ok("pong")) == 1
    assert key not in p._delay and key not in p._next and status(world, "a") == "ok"


def test_paid_model_is_probed_only_after_it_failed(world):
    for x in "bcd":
        world.db.set_enabled(x, False)
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, price_in=3.0, price_out=15.0)
    world.app.health.catalog.invalidate()
    world.a.add(**ok("pong"))
    assert world.app.health.probe_once() == {} and world.a.requests == []  # unknown + paid: no probe
    world.db.set_status("a", "error")
    world.app.health.catalog.invalidate()
    assert world.app.health.probe_once() == {"a": True}
    assert len(world.a.requests) == 1


def test_no_probe_setting(world):
    world.db.set_setting("health_no_probe", "a, c")
    world.app.health.catalog.invalidate()
    for f in (world.a, world.b, world.c, world.d):
        f.add(**ok("pong"))
    assert set(world.app.health.probe_once()) == {"b", "d"}
    assert world.a.requests == [] and world.c.requests == []


def test_probe_400_leaves_status_unchanged(world):
    world.a.add(status=400, json={"error": {"message": "max_tokens too small"}})
    world.b.add(**ok("pong"))
    world.c.add(status=404, json={"error": "gone"})
    world.d.add(status=422, json={"error": "odd"})
    res = world.app.health.probe_once()
    assert res == {"b": True, "c": False}
    assert [status(world, x) for x in "abcd"] == ["unknown", "ok", "error", "unknown"]
    assert world.app.health.last_sent == 4


# ---- class fallback ----

def test_class_fallback_when_all_class_models_are_error(world):
    for x in "abcd":
        world.db.set_status(x, "error")
    cat = Catalog(world.db, 0.0)
    pn = [(f"db/{x}", f"acme/{x}-model") for x in "abcd"]
    assert DBResolver(cat, {}).candidates("claude-haiku-9") == pn  # nothing else to offer
    r = DBResolver(cat, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "openai/gpt-x"})
    assert r("claude-haiku-9") == ("openai", "gpt-x")
    assert r.candidates("claude-haiku-9") == [("openai", "gpt-x")] + pn  # before the error rows
    world.db.set_status("c", "ok")  # one usable class model: the normal ranking
    assert r.candidates("claude-haiku-9")[0] == ("db/c", "acme/c-model")
    assert ("openai", "gpt-x") not in r.candidates("claude-haiku-9")
    world.db.set_status("c", "error")
    r2 = DBResolver(cat, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "a"})  # env names a class model: no duplicate
    assert r2.candidates("claude-haiku-9") == pn


# ---- background probes: same monthly-cap gate for paid models, billed as route "probe" ----

def probe_rows(world):
    con = sqlite3.connect(world.db.path)
    try:
        return con.execute("SELECT model, tokens_in, tokens_out, cost_usd, route FROM billing"
                           " WHERE route='probe' ORDER BY id").fetchall()
    finally:
        con.close()


def paid_a_only(world):
    for x in "bcd":
        world.db.set_enabled(x, False)
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, price_in=3.0, price_out=15.0)
    world.db.set_status("a", "error")
    world.app.health.catalog.invalidate()


def test_paid_probe_is_billed_with_tokens_and_cost(world):
    paid_a_only(world)
    world.a.add(**ok("pong"))  # usage 5 in / 2 out
    assert world.app.health.probe_once() == {"a": True}
    rows = probe_rows(world)
    assert len(rows) == 1 and rows[0][0] == "acme/a-model" and rows[0][4] == "probe"
    assert rows[0][1:3] == (5, 2) and rows[0][3] == pytest.approx((5 * 3.0 + 2 * 15.0) / 1e6)


def test_paid_probe_respects_the_monthly_cap_and_logs_once(world, caplog):
    caplog.set_level(logging.INFO, logger="kiko_lite.health")
    paid_a_only(world)
    world.db.set_setting("monthly_cap_usd", "1.0")
    world.db.add_billing("earlier", 1, 1, 1.5)  # this month's spend is already over the cap
    for _ in range(3):
        world.a.add(**ok("pong"))
    p = world.app.health
    assert p.probe_once() == {} and p.probe_once() == {} and p.probe_once() == {}
    assert world.a.requests == [] and p.sent == 0 and probe_rows(world) == []  # nothing sent, nothing billed
    assert len([r for r in caplog.records if "paused" in r.getMessage()]) == 1  # once per cap period, not per probe
    world.db.set_setting("monthly_cap_usd", "100")  # cap lifted: the paid probe goes out again
    world.app.health.catalog.invalidate()
    assert p.probe_once() == {"a": True} and len(world.a.requests) == 1
    assert len(probe_rows(world)) == 1
    world.db.set_setting("monthly_cap_usd", "1.0")  # a new cap period logs again
    world.db.set_status("a", "error")
    world.app.health.catalog.invalidate()
    p._next.clear()
    assert p.probe_once() == {}
    assert len([r for r in caplog.records if "paused" in r.getMessage()]) == 2


def test_free_probe_is_not_stopped_by_the_cap(world):
    world.db.set_setting("monthly_cap_usd", "1.0")
    world.db.add_billing("earlier", 1, 1, 1.5)
    world.a.add(**ok("pong"))
    world.b.add(**ok("pong"))
    world.c.add(**ok("pong"))
    world.d.add(**ok("pong"))
    assert world.app.health.probe_once() == {"a": True, "b": True, "c": True, "d": True}  # free models: no gate
    assert len(probe_rows(world)) == 4


def test_probe_cost_counts_toward_the_cap(world):
    paid_a_only(world)
    world.db.set_setting("monthly_cap_usd", "0.00001")
    world.a.add(**ok("pong"))
    assert world.app.health.probe_once() == {"a": True}  # spent 0.000045 > cap: now closed
    world.db.set_status("a", "error")
    world.app.health.catalog.invalidate()
    world.app.health._next.clear()
    world.a.add(**ok("pong"))
    assert world.app.health.probe_once() == {} and len(world.a.requests) == 1


# ---- failures before the upstream are billed too (one row, tokens 0, cost 0) ----

def refused_rows(world):
    con = sqlite3.connect(world.db.path)
    try:
        return con.execute("SELECT model, tokens_in, tokens_out, cost_usd, route, error, attempts, upstream_model"
                           " FROM billing ORDER BY id").fetchall()
    finally:
        con.close()


def test_validation_error_is_billed_once_without_body(world):
    r = world.post(body(messages="not a list"))
    assert r.status == 400
    with r:
        r.read()
    rows = refused_rows(world)
    assert len(rows) == 1
    model, t_in, t_out, cost, route, error, attempts, upstream = rows[0]
    assert (model, t_in, t_out, cost, route, attempts, upstream) == ("claude-haiku-9", 0, 0, 0.0, "messages", 0, None)
    assert error.startswith("invalid_request_error")
    assert "not a list" not in error


def test_unknown_model_is_billed_with_the_requested_name(world):
    r = world.post(body(model="no-such-model"))
    with r:
        r.read()
    assert r.status == 404
    rows = refused_rows(world)
    assert len(rows) == 1 and rows[0][0] == "no-such-model" and rows[0][5].startswith("not_found_error")


def test_missing_key_is_billed(world):
    world.app.gateway._secret = lambda name: None
    r = world.post(body())
    with r:
        r.read()
    assert r.status == 503
    rows = refused_rows(world)
    assert len(rows) == 1 and rows[0][5].startswith("api_error") and rows[0][1:4] == (0, 0, 0.0)
    assert SECRET not in rows[0][5]


def test_cap_rejection_is_billed(world):
    world.db.set_setting("monthly_cap_usd", "1.0")
    world.db.add_billing("earlier", 1, 1, 1.5)
    r = world.post(body())
    with r:
        r.read()
    assert r.status == 429
    rows = refused_rows(world)
    assert len(rows) == 2 and rows[1][0] == "claude-haiku-9" and rows[1][5].startswith("rate_limit_error")
    assert rows[1][3] == 0.0 and world.a.requests == []


def test_each_refused_request_is_one_row_and_costs_shows_the_errors(world):
    for _ in range(2):
        with world.post(body(messages="x")) as r:
            r.read()
    assert len(refused_rows(world)) == 2
    summary = world.db.billing_summary(1)
    assert summary[0]["model"] == "claude-haiku-9" and summary[0]["calls"] == 2 and summary[0]["errors"] == 2


# ---- TASK #129 part 2: Gemini bad key, error choice, thinking switch ----

GEMINI_BAD_KEY = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key. " + SECRET,
                            "status": "INVALID_ARGUMENT",
                            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                         "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]}}


def test_gemini_400_api_key_invalid_counts_as_a_rejected_key():
    raw = json.dumps(GEMINI_BAD_KEY).encode()
    assert lite.upstream_status(400, raw) == 401
    assert lite.upstream_status(400, b'{"error": {"message": "bad field", "details": []}}') == 400
    assert lite.upstream_status(400, b"not json") == 400
    assert lite.upstream_status(429, raw) == 429
    st, typ, msg, _ = lite.map_upstream_error(400, raw, None, "gemini", SECRET)
    assert st == 502 and "rejected the configured API key" in msg and SECRET not in msg


@pytest.mark.parametrize("stream", [False, True])
def test_gemini_bad_key_400_fails_over_and_marks_error(world, stream):
    world.a.add(status=400, json=GEMINI_BAD_KEY)
    if stream:
        world.b.add(sse=[chunk({"content": "from b"}), chunk({}, "stop"), "[DONE]"])
    else:
        world.b.add(**ok("from b"))
    r = world.post(body(stream=stream), stream=stream)
    assert r.status == 200
    if stream:
        with r:
            names = [e.event for e in sse.iter_events(r.iter_lines())]
        assert "error" not in names
    else:
        assert r.json()["content"][0]["text"] == "from b"
    assert status(world, "a") == "error" and status(world, "b") == "ok"


def test_gemini_bad_key_400_on_an_explicit_model_is_a_clean_502(world):
    world.a.add(status=400, json=GEMINI_BAD_KEY)
    r = world.post(body(model="a"))
    assert r.status == 502 and "rejected the configured API key" in r.json()["error"]["message"]
    assert SECRET not in json.dumps(r.json())
    assert status(world, "a") == "error"


def test_all_fail_message_retry_after_and_billing_attempts(world):
    for f in (world.a, world.b, world.c):
        f.add(status=503, json={"error": {"message": "down for " + SECRET}}, headers={"Retry-After": "9"})
    r = world.post(body())
    assert r.status == 529 and r.headers.get("Retry-After") == "9"
    assert "down for" in r.json()["error"]["message"] and SECRET not in json.dumps(r.json())
    rows = world.billing()
    assert len(rows) == 1 and rows[0][1] == 3 and rows[0][2] == "overloaded_error"  # one call per candidate


def test_connection_error_outranks_a_bad_key_non_stream(world):
    world.a.server.stop()  # unreachable
    world.b.add(status=401, json={"error": "bad key"})
    world.c.add(status=401, json={"error": "bad key"})
    r = world.post(body())
    assert r.status == 529 and "unreachable" in r.json()["error"]["message"]
    assert r.headers.get("Retry-After")


def test_stream_all_candidates_fail_shows_the_transient_error(world):
    world.a.add(status=503, json={"error": {"message": "down " + SECRET}})
    world.b.add(status=401, json={"error": "bad key " + SECRET})
    world.c.add(status=401, json={"error": "bad key"})
    r = world.post(body(stream=True), stream=True)
    with r:
        lines = list(r.iter_lines())
    raw = "\n".join(lines)
    evs = [(e.event, e.data) for e in sse.iter_events(iter(lines))]
    errs = [json.loads(d) for n, d in evs if n == "error"]
    assert len(errs) == 1 and errs[0]["error"]["type"] == "overloaded_error"
    assert SECRET not in raw
    assert world.billing()[0][1] == 3


def test_stream_connection_error_outranks_a_bad_key(world):
    world.a.server.stop()
    world.b.add(status=401, json={"error": "bad key"})
    world.c.add(status=401, json={"error": "bad key"})
    r = world.post(body(stream=True), stream=True)
    with r:
        raw = "".join(r.iter_lines())
    assert "overloaded_error" in raw and "unreachable" in raw


def test_stream_only_bad_keys_gives_the_key_message_without_the_key(world):
    for f in (world.a, world.b, world.c):
        f.add(status=401, json={"error": "bad key " + SECRET})
    r = world.post(body(stream=True), stream=True)
    with r:
        raw = "".join(r.iter_lines())
    assert "rejected the configured API key" in raw and SECRET not in raw


@pytest.mark.parametrize("mode,expect", [("off", False), ("on", True)])
def test_thinking_switch_sends_chat_template_kwargs(world, mode, expect):
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, model_class="HAIKU", priority=1,
                          thinking=mode)
    world.a.add(**ok("x"))
    assert world.post(body()).status == 200
    assert world.a.requests[0]["chat_template_kwargs"] == {"enable_thinking": expect}
    assert all(m["content"] != "/no_think" for m in world.a.requests[0]["messages"])


def test_thinking_off_prompt_adds_a_system_line(world):
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, model_class="HAIKU", priority=1,
                          thinking="off-prompt")
    world.a.add(**ok("x"))
    world.a.add(**ok("y"))
    world.post(body())
    world.post(body(system="be brief"))
    first, second = world.a.requests
    assert "chat_template_kwargs" not in first
    assert first["messages"][0] == {"role": "system", "content": "/no_think"}
    assert second["messages"][0]["content"] == "be brief\n/no_think"


def test_thinking_default_sends_nothing_and_is_kept_or_cleared(world):
    world.a.add(**ok("x"))
    world.post(body())
    assert "chat_template_kwargs" not in world.a.requests[0]
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, model_class="HAIKU", thinking="off")
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, model_class="HAIKU")  # None: keep
    assert world.db.get_model("a")["thinking"] == "off"
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, model_class="HAIKU",
                          thinking="default")
    assert world.db.get_model("a")["thinking"] is None
    with pytest.raises(ValueError):
        world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, thinking="maybe")


def test_gemini_bad_key_400_on_the_probe_path_is_a_failed_verdict(world):
    world.a.add(status=400, json=GEMINI_BAD_KEY)
    res = world.app.health.probe_once()
    assert res.get("a") is False and status(world, "a") == "error"


def test_off_prompt_with_a_block_list_system_prompt(world):
    world.db.upsert_model("a", "acme/a-model", "KEY_A", api_base=world.a.base, model_class="HAIKU", priority=1,
                          thinking="off-prompt")
    world.a.add(**ok("x"))
    sysblocks = [{"type": "text", "text": "be brief"}, {"type": "text", "text": "be kind"}]
    assert world.post(body(system=sysblocks)).status == 200
    msgs = world.a.requests[0]["messages"]
    assert msgs[0]["role"] == "system" and "/no_think" in msgs[0]["content"]
    assert "be brief" in msgs[0]["content"] and "be kind" in msgs[0]["content"]


# ---- the log line shows the real outcome of a stream that failed after the 200 headers (#150) ----

FRONTS = [
    ("/v1/messages", body),
    ("/v1/chat/completions", lambda **kw: {"model": "claude-haiku-9", "messages": [{"role": "user", "content": "hi"}], **kw}),
    ("/v1/responses", lambda **kw: {"model": "claude-haiku-9", "input": "hi", **kw}),
]


def _post_path(world, path, payload, stream):
    h = {"x-api-key": MASTER, "authorization": "Bearer " + MASTER, "content-type": "application/json"}
    client = khttp.StdlibBackend()
    try:
        r = client.request("POST", world.url + path, h, json.dumps(payload).encode(), stream=stream)
        if stream:
            raw = "".join(r.iter_lines())
            r.close()
            return r.status, raw
        return r.status, r.read()
    finally:
        client.close()


def _request_lines(caplog, path):
    for _ in range(50):  # the line is logged when the stream generator ends
        lines = [m for m in (rec.getMessage() for rec in caplog.records) if m.startswith("request ") and path in m]
        if lines:
            return lines
        time.sleep(0.05)
    return []


@pytest.mark.parametrize("path,make", FRONTS)
@pytest.mark.parametrize("code,typ", [(429, "rate_limit_error"), (529, "overloaded_error"), (500, "api_error")])
def test_failed_stream_logs_upstream_outcome(world, caplog, path, make, code, typ):
    caplog.set_level(logging.INFO)
    for f in (world.a, world.b, world.c, world.d):
        for _ in range(3):
            f.add(status=code, json={"error": {"message": "busy " + SECRET}})
    status_, raw = _post_path(world, path, make(stream=True), True)
    assert status_ == 200
    line = _request_lines(caplog, path)[0]
    assert "status=200" in line and f"upstream={code}" in line and f"error={typ}" in line and "outcome=error" in line
    assert f"path={path}" in line
    assert SECRET not in caplog.text and "hi" not in line.split("path=")[1]


@pytest.mark.parametrize("path,make", FRONTS)
def test_normal_stream_log_unchanged_plus_path(world, caplog, path, make):
    caplog.set_level(logging.INFO)
    world.a.add(sse=[chunk({"content": "ok"}), chunk({}, "stop"), "[DONE]"])
    status_, _ = _post_path(world, path, make(stream=True), True)
    assert status_ == 200
    line = _request_lines(caplog, path)[0]
    assert "status=200" in line and f"path={path}" in line
    assert "upstream=" not in line and "outcome=" not in line and "error=" not in line


def test_midstream_cut_logs_error_outcome(world, caplog):
    caplog.set_level(logging.INFO)
    world.a.add(sse=[chunk({"content": "part"})])  # EOF without finish reason
    _post_path(world, "/v1/messages", body(stream=True), True)
    line = _request_lines(caplog, "/v1/messages")[0]
    assert "status=200" in line and "upstream=502" in line and "error=api_error" in line and "outcome=error" in line


def test_nonstream_error_log_has_no_outcome_fields(world, caplog):
    caplog.set_level(logging.INFO)
    for f in (world.a, world.b, world.c, world.d):
        for _ in range(3):
            f.add(status=429, json={"error": {"message": "slow"}})
    status_, _ = _post_path(world, "/v1/messages", body(), False)
    assert status_ == 429
    line = _request_lines(caplog, "/v1/messages")[0]
    assert "status=429" in line and "path=/v1/messages" in line
    assert "upstream=" not in line and "outcome=" not in line
