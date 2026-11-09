# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Tests for tools/linux_keystore_check.py with mocked subprocess / shutil.which (no real wallet)."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "linux_keystore_check", Path(__file__).resolve().parents[2] / "tools" / "linux_keystore_check.py")
lc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lc)
from kikolib import keystore  # noqa: E402


class MemStore:
    """In-memory store; optional misbehaviour."""

    def __init__(self, *, fail_set=False, stale=False, no_delete=False):
        self.d, self.fail_set, self.stale, self.no_delete = {}, fail_set, stale, no_delete
        self.deleted = False

    def get(self, n):
        return self.d.get(n)

    def set(self, n, v):
        if self.fail_set:
            return False
        if self.stale and n in self.d:
            return True  # claims success but keeps the old value
        self.d[n] = v
        return True

    def delete(self, n):
        self.deleted = True
        if self.no_delete:
            return False
        self.d.pop(n, None)
        return True


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(keystore, "_run", keystore._run)  # main() replaces it; restore afterwards
    monkeypatch.setattr(lc, "_timeout", 20)
    lc._secrets.clear()
    lc._calls.clear()


def test_round_trip_pass_and_deletes(capsys):
    s = MemStore()
    r = lc.round_trip(s, "KIKO_SELFTEST_aa", can_delete=True, keep=False)
    assert r["passed"] and s.deleted and s.d == {} and not r["cleanup_needed"]
    out = capsys.readouterr().out
    for v in lc._secrets:
        assert v not in out and v not in json.dumps(r)


def test_round_trip_fails_when_set_fails():
    r = lc.round_trip(MemStore(fail_set=True), "N", can_delete=True, keep=False)
    assert not r["passed"] and r["steps"][0]["ok"] is False


def test_round_trip_fails_when_overwrite_keeps_old_value():
    s = MemStore(stale=True)
    r = lc.round_trip(s, "N", can_delete=True, keep=False)
    assert not r["passed"]
    assert any(st["step"].startswith("get equals value 2") and not st["ok"] for st in r["steps"])


def test_kwallet_no_delete_reports_manual_cleanup():
    s = MemStore()
    r = lc.round_trip(s, "KIKO_SELFTEST_bb", can_delete=False, keep=False)
    assert r["passed"] and not s.deleted and r["cleanup_needed"]
    assert "manual cleanup needed: entry KIKO_SELFTEST_bb in kdewallet/Passwords" in r["note"]


def test_keep_skips_delete():
    s = MemStore()
    r = lc.round_trip(s, "N", can_delete=True, keep=True)
    assert r["passed"] and not s.deleted and "N" in s.d and r["cleanup_needed"]


def test_delete_failure_fails_and_flags_cleanup():
    r = lc.round_trip(MemStore(no_delete=True), "N", can_delete=True, keep=False)
    assert not r["passed"] and r["cleanup_needed"]


def test_store_exception_is_contained():
    class Boom(MemStore):
        def set(self, n, v):
            raise RuntimeError(v)  # contained; the value must not surface

    r = lc.round_trip(Boom(), "N", can_delete=True, keep=False)
    assert not r["passed"]
    assert not any(v in json.dumps(r) for v in lc._secrets)


def test_run_cmd_timeout_and_missing(monkeypatch):
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(subprocess, "run", boom)
    assert lc.run_cmd(["dcop"])["timeout"] is True

    def missing(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", missing)
    assert lc.run_cmd(["dcop"])["error"] == "FileNotFoundError"


def test_tracked_run_timeout_logged(monkeypatch):
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(subprocess, "run", boom)
    assert lc._tracked_run(["secret-tool", "lookup", "a", "b"]) is None
    assert lc._calls[-1]["timeout"] and "TIMED OUT" in lc._last_call()


def test_scrub_and_fingerprint():
    lc._secrets.append("s3cr3t-value")
    assert "s3cr3t-value" not in lc.scrub("x s3cr3t-value y")
    assert "s3cr3t" not in lc.fp("s3cr3t-value") and "len=12" in lc.fp("s3cr3t-value")


def _fake_secret_tool(monkeypatch):
    """Mock secret-tool at the subprocess level: store/lookup/clear on a dict; stderr echoes the value."""
    db: dict[str, str] = {}

    def cp(args, rc=0, out="", err=""):
        return subprocess.CompletedProcess(args, rc, out, err)

    def run(args, input=None, **kw):
        if args[:2] == ["secret-tool", "store"]:
            db[args[-1]] = input
            return cp(args, err="stored " + input)  # hostile: stderr echoes the value
        if args[:2] == ["secret-tool", "lookup"]:
            return cp(args, out=db[args[-1]]) if args[-1] in db else cp(args, rc=1)
        if args[:2] == ["secret-tool", "clear"]:
            db.pop(args[-1], None)
            return cp(args)
        raise FileNotFoundError(args[0])
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/x")
    monkeypatch.setattr(lc.shutil, "which", lambda t: "/usr/bin/secret-tool" if t == "secret-tool" else None)
    return db


def test_main_secret_tool_pass_json_no_values(monkeypatch, capsys, tmp_path):
    db = _fake_secret_tool(monkeypatch)
    monkeypatch.delenv("KIKO_KEYSTORE_IMPL", raising=False)
    out_json = tmp_path / "r.json"
    rc = lc.main(["--backend", "secret-tool", "--json", str(out_json)])
    assert rc == 0 and db == {}
    text = capsys.readouterr().out + out_json.read_text(encoding="utf-8")
    assert lc._secrets and all(v not in text for v in lc._secrets)
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["exit_code"] == 0 and data["passed_backends"] == ["secret-tool"]
    assert data["factory"]["KIKO_KEYSTORE_IMPL=secret-tool"] == "SecretToolStore"


def test_main_auto_none_available_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lc.shutil, "which", lambda t: None)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "x")
    assert lc.main([]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_wallet_timeout_fails_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "x")
    monkeypatch.setattr(lc.shutil, "which", lambda t: "/bin/x" if t == "secret-tool" else None)

    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(subprocess, "run", boom)
    assert lc.main(["--backend", "secret-tool"]) == 1
    assert "TIMED OUT" in capsys.readouterr().out


def test_main_windows_not_applicable(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    assert lc.main([]) == 1
    assert "NOT APPLICABLE" in capsys.readouterr().out


def test_analyze_signatures_matches_kde3_assumptions():
    text = ("int open(TQString wallet, unsigned int wId)\n"
            "TQString readPassword(int handle, TQString folder, TQString key)\n"
            "int writePassword(int handle, TQString folder, TQString key, TQString value)\n"
            "int removeEntry(int handle, TQString folder, TQString key)\n"
            "int close(int handle, bool force)\n")
    v = lc.analyze_signatures(text)
    assert all(x["matches"] for x in v.values())
    assert not lc.analyze_signatures("int open(TQString wallet)\n")["open"]["matches"]
    assert not lc.analyze_signatures("")["close"]["matches"]


def test_dcop_diagnostics_without_dcop(monkeypatch):
    monkeypatch.setattr(lc.shutil, "which", lambda t: None)
    assert lc.dcop_diagnostics()["available"] is False


# --- dcop path: fake dcop at the subprocess level ---------------------------------------------

_FUNCS = ("int open(TQString wallet, unsigned int wId)\n"
          "TQString readPassword(int handle, TQString folder, TQString key)\n"
          "int writePassword(int handle, TQString folder, TQString key, TQString value)\n"
          "int removeEntry(int handle, TQString folder, TQString key)\n"
          "int close(int handle, bool force)\n")


def _cp(args, rc=0, out="", err=""):
    return subprocess.CompletedProcess(args, rc, out, err)


def _fake_dcop(monkeypatch, *, open_result="7", listing=None):
    """kded/kwalletd target works; the bare 'kwalletd' target fails like real DCOP would."""
    db: dict[str, str] = {}
    listing = listing if listing is not None else {
        (): "kicker\nkded\n", ("kded",): "kwalletd\nkdebug\n", ("kded", "kwalletd", "functions"): _FUNCS,
        ("kded", "kwalletd", "isEnabled"): "true\n", ("kded", "kwalletd", "wallets"): "kdewallet\n",
        ("kded", "kwalletd", "localWallet"): "kdewallet\n"}

    def run(args, input=None, **kw):
        if args[0] != "dcop":
            raise FileNotFoundError(args[0])
        rest = tuple(args[1:])
        if rest in listing:
            return _cp(args, out=listing[rest])
        if rest[:2] == ("kded", "kwalletd") and len(rest) > 2:
            meth, a = rest[2], rest[3:]
            if meth == "open":
                return _cp(args, out=open_result + "\n")
            if open_result == "-1":
                return _cp(args, out="-1\n")
            if meth == "writePassword":
                db[a[2]] = a[3]
                return _cp(args, out="0\n")
            if meth == "readPassword":
                return _cp(args, out=db.get(a[2], "") + "\n")
            if meth == "removeEntry":
                db.pop(a[2], None)
                return _cp(args, out="0\n")
            if meth == "close":
                return _cp(args, out="0\n")
        return _cp(args, rc=1, err="ERROR: no such object/function")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lc.shutil, "which", lambda t: "/opt/trinity/bin/dcop" if t == "dcop" else None)
    return db


def test_dcop_store_round_trip_through_tracker(monkeypatch):
    db = _fake_dcop(monkeypatch)
    monkeypatch.setattr(keystore, "_run", lc._tracked_run)
    r = lc.round_trip(keystore.DcopStore(), "KIKO_SELFTEST_cc", can_delete=True, keep=False)
    assert r["passed"] and db == {}
    methods = {c["method"] for c in lc._calls}
    assert {"open", "writePassword", "readPassword", "removeEntry", "close"} <= methods
    assert all(c["target"] == "kded kwalletd" for c in lc._calls)
    opens = [c for c in lc._calls if c["method"] == "open"]
    assert opens[0]["stdout"] == "7"
    assert any(c["method"] == "writePassword" and "writePassword 7" in c["cmd"] for c in lc._calls)
    assert all(c["stdout"] is None for c in lc._calls if c["method"] == "readPassword")
    blob = json.dumps(lc._calls) + json.dumps(r)
    assert lc._secrets and not any(v in blob for v in lc._secrets)


def test_dcop_failing_open_logs_methods_handle_both_targets(monkeypatch, capsys):
    _fake_dcop(monkeypatch, open_result="-1")
    monkeypatch.setattr(keystore, "_run", lc._tracked_run)
    r = lc.round_trip(keystore.DcopStore(), "KIKO_SELFTEST_dd", can_delete=True, keep=False)
    assert not r["passed"]
    detail = r["steps"][0]["detail"]
    assert "[kded kwalletd]" in detail and "[kwalletd]" in detail      # both targets
    assert "open" in detail and "stdout='-1'" in detail                 # method and returned handle
    assert any(s["step"] == "cleanup delete after failed set" for s in r["steps"])
    assert r["cleanup_needed"] and "KIKO_SELFTEST_dd" in r["note"]
    out = capsys.readouterr().out + json.dumps(r) + json.dumps(lc._calls)
    assert not any(v in out for v in lc._secrets)


def test_dcop_write_value_never_logged(monkeypatch):
    _fake_dcop(monkeypatch)
    monkeypatch.setattr(keystore, "_run", lc._tracked_run)
    keystore.DcopStore().set("N", "sUp3r-secret-VALUE")
    assert "sUp3r-secret-VALUE" not in json.dumps(lc._calls)
    keystore.DcopStore().get("N")
    assert "sUp3r-secret-VALUE" not in json.dumps(lc._calls)


def test_set_failed_cleans_up_when_backend_can_delete():
    s = MemStore(fail_set=True)
    r = lc.round_trip(s, "N", can_delete=True, keep=False)
    assert s.deleted and not r["passed"] and not r["cleanup_needed"]


def test_set_failed_kwallet_prints_manual_cleanup():
    r = lc.round_trip(MemStore(fail_set=True), "KIKO_SELFTEST_ee", can_delete=False, keep=False)
    assert not r["passed"] and r["cleanup_needed"] and "KIKO_SELFTEST_ee" in r["note"]


def test_main_restores_keystore_run(monkeypatch):
    orig = keystore._run
    db = _fake_secret_tool(monkeypatch)
    assert lc.main(["--backend", "secret-tool"]) == 0 and db == {}
    assert keystore._run is orig

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(lc, "run_backend", boom)
    with pytest.raises(RuntimeError):
        lc.main(["--backend", "secret-tool"])
    assert keystore._run is orig


def test_dcop_diagnostics_kded_kwalletd(monkeypatch, capsys):
    _fake_dcop(monkeypatch)
    d = lc.dcop_diagnostics()
    assert d["kded_exposes_kwalletd"] is True
    assert all(v["matches"] for v in d["functions"]["kded kwalletd"]["analysis"].values())
    out = capsys.readouterr().out
    assert "target [kded kwalletd]" in out and "OK" in out


def test_dcop_diagnostics_tdewalletd_variant(monkeypatch, capsys):
    _fake_dcop(monkeypatch, listing={
        (): "kicker\nkded\n", ("kded",): "tdewalletd\nkdebug\n", ("kded", "tdewalletd", "functions"): _FUNCS})
    d = lc.dcop_diagnostics()
    assert d["kded_exposes_kwalletd"] is False
    assert d["functions"]["kded tdewalletd"]["rc"] == 0
    assert d["functions"]["kded kwalletd"]["rc"] != 0
    out = capsys.readouterr().out
    assert "target [kded tdewalletd]" in out and "target [kded kwalletd]" not in out


def test_dcop_diagnostics_nothing(monkeypatch, capsys):
    _fake_dcop(monkeypatch, listing={(): "", ("kded",): ""})
    d = lc.dcop_diagnostics()
    assert d["kded_exposes_kwalletd"] is False
    assert "no signature comparison possible" in capsys.readouterr().out


def test_main_dcop_signatures_and_json(monkeypatch, capsys, tmp_path):
    db = _fake_dcop(monkeypatch)
    monkeypatch.delenv("KIKO_KEYSTORE_IMPL", raising=False)
    j = tmp_path / "d.json"
    assert lc.main(["--backend", "dcop", "--json", str(j)]) == 0 and db == {}
    out = capsys.readouterr().out
    assert "target [kded kwalletd]" in out and "MISMATCH" not in out
    data = json.loads(j.read_text(encoding="utf-8"))
    assert data["dcop_diagnostics"]["kded_exposes_kwalletd"] is True
    assert not any(v in out + j.read_text(encoding="utf-8") for v in lc._secrets)


def test_main_dcop_failure_summary_names_targets(monkeypatch, capsys):
    _fake_dcop(monkeypatch, open_result="-1")
    assert lc.main(["--backend", "dcop"]) == 1
    out = capsys.readouterr().out
    assert "subprocess calls of failed backend dcop" in out
    assert "[kded kwalletd]" in out and "[kwalletd]" in out
