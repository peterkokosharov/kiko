# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.keystore: all OS calls are faked; the real secret store is never touched."""
import subprocess
import sys

import pytest

from kikolib import factory, keystore


class Fake:
    """Records subprocess.run calls and answers from a script of (returncode, stdout)."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def __call__(self, args, **kw):
        self.calls.append((list(args), kw))
        rc, out = self.replies.pop(0) if self.replies else (0, "")
        return subprocess.CompletedProcess(args, rc, out, "")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("KIKO_KEYSTORE_TIMEOUT", raising=False)
    monkeypatch.delenv("KIKO_KEYSTORE_IMPL", raising=False)
    # Safety net: any un-faked subprocess call fails the test instead of reaching an OS store.
    def forbid(*a, **k):
        raise AssertionError("real subprocess.run called")
    monkeypatch.setattr(subprocess, "run", forbid)
    factory.clear()
    keystore.register_all()
    yield
    factory.clear()
    keystore.register_all()


def _fake(monkeypatch, replies=None):
    f = Fake(replies)
    monkeypatch.setattr(subprocess, "run", f)
    return f


def test_secret_tool_commands(monkeypatch):
    f = _fake(monkeypatch, [(0, "sek\n"), (0, ""), (0, "")])
    s = keystore.SecretToolStore()
    assert s.get("nvidia_api_key") == "sek"
    assert f.calls[0][0] == ["secret-tool", "lookup", "kiko-identity", "NVIDIA_API_KEY"]
    assert s.set("k", "v") is True
    args, kw = f.calls[1]
    assert args == ["secret-tool", "store", "--label", "kiko K", "kiko-identity", "K"]
    assert kw["input"] == "v" and "v" not in args and kw["timeout"] == 15.0
    assert s.delete("k") is True
    assert f.calls[2][0] == ["secret-tool", "clear", "kiko-identity", "K"]


def test_failures_swallowed(monkeypatch):
    s = keystore.SecretToolStore()
    _fake(monkeypatch, [(1, "")])
    assert s.get("x") is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 3)
    monkeypatch.setattr(subprocess, "run", boom)
    assert s.get("x") is None and s.set("x", "v") is False
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert s.delete("x") is False


def test_keychain_commands(monkeypatch):
    f = _fake(monkeypatch, [(0, "pw\n"), (0, ""), (0, "")])
    s = keystore.KeychainStore()
    assert s.get("a") == "pw"
    assert f.calls[0][0] == ["security", "find-generic-password", "-s", "KIKO", "-a", "A", "-w"]
    assert s.set("a", "val")
    assert f.calls[1][0] == ["security", "add-generic-password", "-s", "KIKO", "-a", "A", "-w", "val", "-U"]
    assert s.delete("a")
    assert f.calls[2][0] == ["security", "delete-generic-password", "-s", "KIKO", "-a", "A"]


def test_kwallet_commands(monkeypatch):
    f = _fake(monkeypatch, [(0, "pw\n"), (0, "")])
    s = keystore.KWalletStore()
    assert s.get("a") == "pw"
    assert f.calls[0][0] == ["kwallet-query", "-r", "A", "-f", "Passwords", "kdewallet"]
    assert s.set("a", "val")
    args, kw = f.calls[1]
    assert args == ["kwallet-query", "-w", "A", "-f", "Passwords", "kdewallet"]
    assert kw["input"] == "val"
    assert s.delete("a") is False


def test_dcop_get_set_delete(monkeypatch):
    f = _fake(monkeypatch, [(0, "7\n"), (0, "pw\n"), (0, ""),
                            (0, "7\n"), (0, "0\n"), (0, ""),
                            (0, "7\n"), (0, "0\n"), (0, "")])
    s = keystore.DcopStore()
    assert s.get("a") == "pw"
    assert [c[0] for c in f.calls[:3]] == [
        ["dcop", "kded", "kwalletd", "open", "kdewallet", "0"],
        ["dcop", "kded", "kwalletd", "readPassword", "7", "Passwords", "A"],
        ["dcop", "kded", "kwalletd", "close", "7", "0"],
    ]
    assert s.set("a", "val")
    assert f.calls[4][0] == ["dcop", "kded", "kwalletd", "writePassword", "7", "Passwords", "A", "val"]
    assert s.delete("a")
    assert f.calls[7][0] == ["dcop", "kded", "kwalletd", "removeEntry", "7", "Passwords", "A"]


def test_dcop_falls_back_to_plain_kwalletd(monkeypatch):
    f = _fake(monkeypatch, [(1, ""), (0, "7\n"), (0, "pw\n"), (0, "")])
    assert keystore.DcopStore().get("a") == "pw"
    assert f.calls[0][0][:3] == ["dcop", "kded", "kwalletd"]
    assert f.calls[1][0] == ["dcop", "kwalletd", "open", "kdewallet", "0"]
    assert f.calls[2][0] == ["dcop", "kwalletd", "readPassword", "7", "Passwords", "A"]


def test_dcop_printed_failure_values(monkeypatch):
    # non-zero printed return = failure even with exit code 0
    _fake(monkeypatch, [(0, "7\n"), (0, "1\n"), (0, "")])
    assert keystore.DcopStore().set("a", "v") is False
    _fake(monkeypatch, [(0, "7\n"), (0, "-1\n"), (0, "")])
    assert keystore.DcopStore().delete("a") is False
    _fake(monkeypatch, [(0, "7\n"), (0, "-1\n"), (0, "")])
    assert keystore.DcopStore().get("a") is None


def test_dcop_open_failure_no_close(monkeypatch):
    f = _fake(monkeypatch, [(0, "-1\n"), (0, "-1\n")])  # both targets refuse
    assert keystore.DcopStore().get("a") is None
    assert len(f.calls) == 2 and all(c[0][-3] == "open" for c in f.calls)
    _fake(monkeypatch, [(0, "junk\n"), (0, "junk\n")])
    assert keystore.DcopStore().get("a") is None


def test_get_secret_falls_through_stores(monkeypatch):
    class Empty:
        def get(self, n): return None
    class Boom:
        def get(self, n): raise OSError("x")
    class Has:
        def get(self, n): return "found"
    factory.clear()
    factory.register("keystore", "a", probe=lambda: (True, ""), load=Empty, priority=1)
    factory.register("keystore", "b", probe=lambda: (True, ""), load=Boom, priority=2)
    factory.register("keystore", "c", probe=lambda: (False, "no"), load=lambda: 1 / 0, priority=3)
    factory.register("keystore", "d", probe=lambda: (True, ""), load=Has, priority=4)
    assert keystore.get_secret("x", env={}) == "found"
    monkeypatch.setenv("KIKO_KEYSTORE_IMPL", "a")  # override: only that store
    factory.reset()
    assert keystore.get_secret("x", env={}) is None


def test_wcm_uses_call_layer(monkeypatch):
    seen = {}
    monkeypatch.setattr(keystore, "_wcm_read", lambda t: seen.setdefault("r", t) and "val")
    monkeypatch.setattr(keystore, "_wcm_write", lambda t, v: seen.update(w=(t, v)) or True)
    monkeypatch.setattr(keystore, "_wcm_delete", lambda t: seen.update(d=t) or True)
    s = keystore.WcmStore()
    assert s.get("gemini_api_key") == "val"
    assert seen["r"] == "KIKO/KIKO_IDENTITY/GEMINI_API_KEY"
    assert s.set("gemini_api_key", "v") and seen["w"] == ("KIKO/KIKO_IDENTITY/GEMINI_API_KEY", "v")
    assert s.delete("gemini_api_key") and seen["d"] == "KIKO/KIKO_IDENTITY/GEMINI_API_KEY"

    def boom(t):
        raise OSError("x")
    monkeypatch.setattr(keystore, "_wcm_read", boom)
    assert s.get("a") is None


def test_probes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(keystore.shutil, "which", lambda n: "/usr/bin/" + n if n == "kwallet-query" else None)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    assert keystore._probe_wcm()[0] is False
    assert keystore._probe_secret_tool()[0] is False
    assert keystore._probe_kwallet()[0] is True
    assert keystore._probe_dcop()[0] is False
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/x")
    monkeypatch.setattr(keystore.shutil, "which", lambda n: "/usr/bin/" + n)
    assert keystore._probe_secret_tool()[0] is True
    assert keystore._probe_keychain()[0] is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert keystore._probe_keychain()[0] is True
    monkeypatch.setattr(sys, "platform", "win32")
    assert keystore._probe_wcm()[0] is True
    assert keystore._probe_secret_tool()[0] is False


def test_factory_order_linux_prefers_secret_tool(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(keystore.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/x")
    assert isinstance(factory.choose("keystore"), keystore.SecretToolStore)
    rep = factory.report()[0]
    assert rep["chosen"] == "secret-tool" and rep["source"] == "auto"
    assert {c["name"] for c in rep["candidates"]} == {"wcm", "keychain", "secret-tool", "kwallet", "dcop"}


def test_env_override_to_unavailable_errors(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("KIKO_KEYSTORE_IMPL", "wcm")
    with pytest.raises(RuntimeError):
        factory.choose("keystore")


def test_get_secret_env_first(monkeypatch):
    # factory would raise if consulted: env hit must not reach the store
    monkeypatch.setattr(keystore, "_store", lambda: (_ for _ in ()).throw(AssertionError("store used")))
    assert keystore.get_secret("K", env={"K": "fromenv"}) == "fromenv"
    assert keystore.get_secret("k", env={"K": "upper"}) == "upper"


def test_get_set_secret_via_store(monkeypatch):
    class Mem:
        d = {}
        def get(self, n): return self.d.get(n.upper())
        def set(self, n, v): self.d[n.upper()] = v; return True
        def delete(self, n): return bool(self.d.pop(n.upper(), None))
    factory.clear()
    factory.register("keystore", "mem", probe=lambda: (True, "mem"), load=Mem)
    assert keystore.get_secret("x", env={}) is None
    assert keystore.set_secret("x", "v") is True
    assert keystore.get_secret("x", env={}) == "v"


def test_no_store_available(monkeypatch):
    factory.clear()
    assert keystore.get_secret("x", env={}) is None
    assert keystore.set_secret("x", "v") is False


# ---- timeout (configurable, counted) ----

def test_timeout_default_and_env(monkeypatch):
    monkeypatch.delenv("KIKO_KEYSTORE_TIMEOUT", raising=False)
    assert keystore.timeout_seconds() == 15.0
    monkeypatch.setenv("KIKO_KEYSTORE_TIMEOUT", "42.5")
    assert keystore.timeout_seconds() == 42.5
    for bad in ("abc", "0", "-3", "nan", "inf", ""):
        monkeypatch.setenv("KIKO_KEYSTORE_TIMEOUT", bad)
        assert keystore.timeout_seconds() == 15.0


def test_run_passes_timeout_and_counts_timeouts(monkeypatch):
    seen = []

    def slow(args, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", slow)
    monkeypatch.setenv("KIKO_KEYSTORE_TIMEOUT", "7")
    before = keystore.timeout_count()
    assert keystore.SecretToolStore().get("x") is None
    assert seen == [7.0]
    assert keystore.timeout_count() == before + 1


def test_other_failures_are_not_counted_as_timeouts(monkeypatch):
    def broken(args, **kw):
        raise OSError("no such tool")

    monkeypatch.setattr(subprocess, "run", broken)
    before = keystore.timeout_count()
    assert keystore.SecretToolStore().get("x") is None
    assert keystore.timeout_count() == before
