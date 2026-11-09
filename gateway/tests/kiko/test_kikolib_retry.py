# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.retry with a fake send, fake sleep and fake clock."""
import io
from email.utils import formatdate

import pytest

from kikolib.http import ConnectError, ConnectionLost, TLSVerifyError, Headers, HttpTimeout, Response
from kikolib.retry import RetryPolicy, send_with_retry


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


def resp(status, **headers):
    return Response(status, "x", Headers(list(headers.items())), io.BytesIO(b""))


def seq(*items):
    items = list(items)
    calls = []

    def send():
        calls.append(1)
        it = items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it

    send.calls = calls
    return send


POL = RetryPolicy(jitter=False)


def run(send, policy=POL, **kw):
    c = Clock()
    out = send_with_retry(send, policy, sleep=c.sleep, clock=c, **kw)
    return out, c


def test_success_first_try():
    r = resp(200)
    (got, n), c = run(seq(r))
    assert got is r and n == 1 and c.sleeps == []


def test_retries_429_503_and_connect_errors_then_succeeds():
    ok = resp(200)
    send = seq(resp(429), resp(503), ConnectError("x"), HttpTimeout("t"), ok)
    (got, n), c = run(send)
    assert got is ok and n == 5
    assert c.sleeps == [1.0, 2.0, 4.0, 8.0]


def test_discarded_responses_are_closed_and_final_is_not():
    a, b = resp(503), resp(200)
    closed = []
    a.close = lambda: closed.append("a")
    b.close = lambda: closed.append("b")
    run(seq(a, b))
    assert closed == ["a"]


def test_retry_after_seconds_beyond_max_delay_is_honoured():
    send = seq(resp(429, **{"Retry-After": "7"}), resp(429, **{"Retry-After": "45"}), resp(200))
    (_, n), c = run(send)
    assert n == 3 and c.sleeps == [7.0, 10.0, 10.0, 10.0, 10.0, 5.0]  # 45 s > max_delay 20, in slices
    assert RetryPolicy().delay_for(1, "45") == 45.0


def test_retry_after_beyond_budget_stops_at_once():
    last = resp(429, **{"Retry-After": "500"})
    send = seq(last, resp(200))
    (got, n), c = run(send)
    assert got is last and n == 1 and c.sleeps == [] and len(send.calls) == 1


def test_retry_after_capped_by_remaining_budget():
    assert RetryPolicy(budget=90).delay_for(1, "60", remaining=30.0) == 30.0


def test_connection_lost_is_retried_but_tls_verify_error_is_not():
    ok = resp(200)
    (got, n), _ = run(seq(ConnectionLost("x"), ok))
    assert got is ok and n == 2
    send = seq(TLSVerifyError("bad cert"), ok)
    with pytest.raises(TLSVerifyError):
        run(send)
    assert len(send.calls) == 1


def test_retry_after_http_date():
    when = formatdate(1_000_000 + 5, usegmt=True)
    pol = RetryPolicy(jitter=False)
    assert pol.delay_for(1, when, now=1_000_000) == pytest.approx(5.0, abs=1.0)
    assert pol.delay_for(1, "garbage", now=0) == 1.0
    assert pol.delay_for(1, formatdate(0, usegmt=True), now=1000) == 0.0  # past date


def test_gives_up_after_attempts_returns_last_response():
    last = resp(503)
    (got, n), c = run(seq(resp(503), resp(503), last), RetryPolicy(attempts=3, jitter=False))
    assert got is last and n == 3 and len(c.sleeps) == 2


def test_gives_up_after_attempts_reraises_last_exception():
    err = ConnectError("final")
    send = seq(ConnectError("a"), err)
    c = Clock()
    with pytest.raises(ConnectError) as ei:
        send_with_retry(send, RetryPolicy(attempts=2, jitter=False), sleep=c.sleep, clock=c)
    assert ei.value is err


def test_budget_cap():
    pol = RetryPolicy(attempts=50, base_delay=10.0, max_delay=20.0, budget=25.0, jitter=False)
    send = seq(*[resp(503) for _ in range(50)])
    (got, n), c = run(send, pol)
    assert got.status == 503 and c.t <= 25.0 and n < 50
    assert sum(c.sleeps) == pytest.approx(25.0)


def test_non_retryable_returns_at_once():
    r = resp(400)
    send = seq(r)
    (got, n), c = run(send)
    assert got is r and n == 1 and c.sleeps == [] and len(send.calls) == 1


def test_on_wait_called_with_split_sleeps():
    waits = []
    pol = RetryPolicy(base_delay=25.0, max_delay=25.0, jitter=False)
    (_, n), c = run(seq(resp(503), resp(200)), pol, on_wait=lambda d, a, r: waits.append((d, a, r)))
    assert c.sleeps == [10.0, 10.0, 5.0]
    assert waits == [(10.0, 1, "http 503"), (10.0, 1, "http 503"), (5.0, 1, "http 503")]


def test_on_wait_reason_for_exception():
    waits = []
    run(seq(HttpTimeout("t"), resp(200)), on_wait=lambda d, a, r: waits.append(r))
    assert waits == ["HttpTimeout"]


def test_full_jitter_bounds():
    pol = RetryPolicy(rng=lambda: 0.5)
    assert pol.delay_for(3) == 2.0  # backoff 4 * 0.5
    assert RetryPolicy(rng=lambda: 1.0).delay_for(10) == 20.0  # capped by max_delay
    assert pol.delay_for(1, remaining=0.25) == 0.25
