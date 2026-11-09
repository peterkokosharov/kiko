# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.factory: choice order, override/env, errors, caching, report."""
import threading

import pytest

from kikolib import factory


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("KIKO_DEMO_IMPL", raising=False)
    factory.clear()
    yield
    factory.clear()


def _reg(name, ok=True, prio=100, reason="r"):
    factory.register("demo", name, probe=lambda: (ok, reason), load=lambda: name + "-impl", priority=prio)


def test_auto_picks_lowest_priority_ok():
    _reg("a", prio=30)
    _reg("b", prio=10, ok=False, reason="nope")
    _reg("c", prio=20)
    assert factory.choose("demo") == "c-impl"
    rep = factory.report()[0]
    assert (rep["kind"], rep["chosen"], rep["source"]) == ("demo", "c", "auto")
    assert [c["name"] for c in rep["candidates"]] == ["b", "c", "a"]
    assert rep["candidates"][0] == {"name": "b", "ok": False, "reason": "nope"}


def test_override_beats_env_beats_auto(monkeypatch):
    _reg("a", prio=1)
    _reg("b")
    _reg("c")
    monkeypatch.setenv("KIKO_DEMO_IMPL", "b")
    assert factory.choose("demo") == "b-impl"
    assert factory.report()[0]["source"] == "env"
    factory.reset()
    assert factory.choose("demo", override="c") == "c-impl"
    assert factory.report()[0]["source"] == "override"


def test_new_override_after_cached_choice():
    _reg("a", prio=1)
    _reg("b")
    assert factory.choose("demo") == "a-impl"
    assert factory.choose("demo", override="b") == "b-impl"
    assert factory.choose("demo") == "b-impl"  # new choice is now the cached one
    assert factory.report()[0]["source"] == "override"


def test_errors():
    with pytest.raises(LookupError):
        factory.choose("missing")
    _reg("a", ok=False)
    with pytest.raises(RuntimeError, match="no implementation available"):
        factory.choose("demo")
    with pytest.raises(LookupError, match="unknown"):
        factory.choose("demo", override="zzz")
    with pytest.raises(RuntimeError, match="unavailable"):
        factory.choose("demo", override="a")
    assert factory.report()[0]["chosen"] is None


def test_probe_exception_is_unavailable():
    def boom():
        raise OSError("x")

    factory.register("demo", "bad", probe=boom, load=lambda: 1)
    with pytest.raises(RuntimeError):
        factory.choose("demo")
    assert factory.report()[0]["candidates"][0]["ok"] is False


def test_cache_and_reset():
    calls = []
    factory.register("demo", "a", probe=lambda: (True, ""), load=lambda: calls.append(1) or object())
    first = factory.choose("demo")
    assert factory.choose("demo") is first
    assert len(calls) == 1
    factory.reset()
    assert factory.choose("demo") is not first


def test_thread_safe_single_load():
    calls = []

    def load():
        calls.append(1)
        return object()

    factory.register("demo", "a", probe=lambda: (True, ""), load=load)
    out = []
    ts = [threading.Thread(target=lambda: out.append(factory.choose("demo"))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and len({id(o) for o in out}) == 1
