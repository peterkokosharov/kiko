# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #129 part 3 fix 1: the log writer never drops a billing row, its stop is bounded and race-free, deeply
nested upstream bodies do not crash, and the 503 lingering close stays off the accept thread. Temp DB only."""
import http.client
import json
import socket
import sqlite3
import threading
import time

import pytest

from kiko_lite import app as lite
from kiko_lite import db as kdb
from kiko_lite import resolver as res_mod
from kiko_lite.resolver import AsyncWriter, BillingWriter, Catalog
from kikolib import server

DEEP = ("[" * 100000 + "]" * 100000).encode()  # RecursionError in json.loads


def request_ids(db, table, col):
    con = sqlite3.connect(db.path)
    try:
        return [r[0] for r in con.execute(f"SELECT {col} FROM {table}")]
    finally:
        con.close()


def result(i, big=0):
    text = "x" * big
    return lite.RequestResult(request_id=f"r{i}", client_model="m", request={"t": text} if big else None,
                              response={"t": text} if big else None)


def wait_for(cond, secs=5.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def db(tmp_path):
    return kdb.Database(tmp_path / "t" / "kiko.db")


@pytest.fixture
def gated(db):
    """AsyncWriter factory over the real BillingWriter; the writer THREAD is held until ``gate`` is set."""
    gate = threading.Event()
    bw = BillingWriter(db, Catalog(db, 0.0))

    def writer(res):
        if threading.current_thread().name == "kiko-log-writer":
            gate.wait(10)
        bw(res)
    made = []

    def make(**kw):
        w = AsyncWriter(writer, **kw)
        made.append(w)
        return w
    yield type("G", (), {"gate": gate, "make": staticmethod(make)})
    gate.set()
    for w in made:
        w.stop(2.0)


def test_byte_bound_keeps_billing_drops_text(db, gated):
    w = gated.make(max_bytes=1000)
    w.start()
    w(result(1, 5000))  # the thread takes it (and holds it at the gate)
    assert wait_for(lambda: w.queue.qsize() == 0)
    w(result(2, 5000))  # empty queue: accepted with its text
    w(result(3, 5000))  # over the byte bound: queued billing-only
    w(result(4, 5000))
    assert w.dropped == 2
    gated.gate.set()
    w.stop(5.0)
    assert sorted(request_ids(db, "billing", "request_id")) == ["r1", "r2", "r3", "r4"]
    assert sorted(request_ids(db, "responses", "id")) == ["r1", "r2"]


def test_empty_queue_always_accepts_one_huge_result(db, gated):
    w = gated.make(max_bytes=10)
    w.start()
    w(result(1, 100000))
    gated.gate.set()
    w.stop(5.0)
    assert w.dropped == 0
    assert request_ids(db, "responses", "id") == ["r1"] and request_ids(db, "billing", "request_id") == ["r1"]


def test_queue_full_writes_billing_inline(db, gated):
    w = gated.make(maxsize=1)
    w.start()
    for i in range(6):
        w(result(i, 10))
    # the thread is still held: what is in the database now was written inline (billing only)
    assert w.dropped >= 3
    assert len(request_ids(db, "billing", "request_id")) >= 3
    assert request_ids(db, "responses", "id") == []
    gated.gate.set()
    w.stop(5.0)
    assert sorted(request_ids(db, "billing", "request_id")) == [f"r{i}" for i in range(6)]
    assert len(request_ids(db, "responses", "id")) <= 2  # the overflow lost its text only


def test_stop_returns_within_timeout_when_writer_is_blocked():
    gate = threading.Event()
    w = AsyncWriter(lambda res: gate.wait(10), maxsize=1)
    w.start()
    w(result(1))
    assert wait_for(lambda: w.queue.qsize() == 0)
    w(result(2))  # the thread holds one, the queue the other: the sentinel cannot be queued
    t0 = time.monotonic()
    w.stop(0.5)
    took = time.monotonic() - t0
    gate.set()
    assert took < 1.0, took  # one shared deadline, not 0.5 (sentinel) + 0.5 (join)


def test_after_stop_timeout_inline_writes_wait_for_the_old_writer():
    gate, busy, overlap, started = threading.Event(), [], [], threading.Event()

    def writer(res):
        if busy:
            overlap.append(res.request_id)
        busy.append(1)
        try:
            if threading.current_thread().name == "kiko-log-writer":
                started.set()
                gate.wait(10)
        finally:
            busy.pop()
    w = AsyncWriter(writer)
    w.start()
    w(result(1))
    assert started.wait(5)
    w.stop(0.2)  # times out: the old thread is still inside writer(r1)
    done = []
    t = threading.Thread(target=lambda: (w(result(2)), done.append(1)))
    t.start()
    time.sleep(0.3)
    assert not done  # the inline write waits for the old writer instead of racing it
    gate.set()
    t.join(5)
    assert done and not overlap


def test_no_inline_write_while_writer_thread_is_alive():
    names = []
    w = AsyncWriter(lambda res: names.append(threading.current_thread().name))
    w.start()
    ts = [threading.Thread(target=lambda k=k: [w(result(f"{k}-{i}")) for i in range(50)]) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert wait_for(lambda: len(names) == 200)
    assert set(names) == {"kiko-log-writer"}
    w.stop(2.0)


def test_start_stop_race_writes_every_result_once():
    seen, stop = [], threading.Event()
    w = AsyncWriter(lambda res: seen.append(res.request_id))
    sent = []

    def producer():
        i = 0
        while not stop.is_set():
            w(result(i))
            sent.append(i)
            i += 1
            time.sleep(0.0005)
    t = threading.Thread(target=producer)
    t.start()
    for _ in range(30):
        w.start()
        time.sleep(0.005)
        w.stop(1.0)
    stop.set()
    t.join(5)
    w.start()
    w.stop(2.0)
    assert wait_for(lambda: len(seen) == len(sent))
    assert sorted(seen) == sorted(f"r{i}" for i in sent)


def test_approx_bytes_counts_bytes_values():
    r = lite.RequestResult(request_id="a", client_model="m", request={"b": b"y" * 1000}, response={"s": "z" * 500})
    assert res_mod._approx_bytes(r) >= 1500


def test_deeply_nested_upstream_body_is_treated_as_non_json():
    assert isinstance(lite._upstream_message(DEEP), str)
    assert lite._bad_key_400(DEEP) is False
    assert lite._retry_info(DEEP) is None


def _recv_until_close(s, secs=5.0):
    end, data = time.monotonic() + secs, b""
    while time.monotonic() < end:
        try:
            c = s.recv(4096)
        except OSError:
            break
        if not c:
            break
        data += c
    return data


def test_503_linger_does_not_block_the_accept_thread():
    app = server.App()
    app.route("GET", "/x")(lambda req: server.JsonResponse({}))
    srv = server.Server(app, port=0, max_connections=1).start()
    held = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    socks = []
    try:
        held.request("GET", "/x")
        held.getresponse().read()  # the only slot is taken (keep-alive)
        t0 = time.monotonic()
        for _ in range(50):  # idle clients: send nothing, never close
            socks.append(socket.create_connection(("127.0.0.1", srv.port), timeout=10))
        for s in socks:
            s.settimeout(5)
            assert s.recv(4096).startswith(b"HTTP/1.1 503")
        # 50 x 0.2 s on the accept thread would be 10 s
        assert time.monotonic() - t0 < 3.0
        # the idle clients' lingering closes (0.2 s each, at most _LINGERERS at once) end on their own; wait until
        # every slot of this server is free again, else the next client may find them all busy and get a bare close
        assert wait_for(lambda: srv._httpd._lingerers._value == server._LINGERERS, 10.0), \
            f"lingerer slots not released: {srv._httpd._lingerers._value} of {server._LINGERERS} free"
        # a client that sends its request after the 503 still reads the whole body: no RST
        c = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        c.sendall(b"GET /x HTTP/1.1\r\nHost: localhost\r\n\r\n" + b"z" * 20000)
        data = _recv_until_close(c)
        c.close()
        assert data.startswith(b"HTTP/1.1 503")
        assert json.loads(data.split(b"\r\n\r\n", 1)[1])["error"]["type"] == "overloaded_error"
    finally:
        for s in socks:
            s.close()
        held.close()
        srv.stop()
