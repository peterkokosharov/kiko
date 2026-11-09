#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Live probe for the Linux backends of kikolib/keystore.py (secret-tool, kwallet, dcop).

Usage, from the repo / bundle root, nothing to install (stdlib only, Python 3.11+):

    python3 tools/linux_keystore_check.py [--backend auto|secret-tool|kwallet|dcop]
                                          [--json out.json] [--keep] [--timeout 20]

It reports the machine (OS, Python, desktop session, tools on PATH), the keystore probes and the
factory choice, then runs a set / get / overwrite / get / delete / get round trip against each
available backend using a THROWAWAY entry (KIKO_SELFTEST_<8 hex>) and a random throwaway value.
dcop gets extra read-only diagnostics (raw listings, kwalletd method signatures).

Secret values are never printed or written: only lengths and short sha256 prefixes, and every
output line and the JSON file are scrubbed of the throwaway values as a second guard. Other
wallet entries are never read or listed. Exit code 0 if at least one backend passed the full
round trip, else 1 (Windows: "not applicable", exit 1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kikolib import factory, keystore  # noqa: E402

TOOL_VERSION = "1"
BACKENDS = ("secret-tool", "kwallet", "dcop")
_PROBES = {"secret-tool": "_probe_secret_tool", "kwallet": "_probe_kwallet", "dcop": "_probe_dcop"}
_CLASSES = {"secret-tool": "SecretToolStore", "kwallet": "KWalletStore", "dcop": "DcopStore"}
_CAN_DELETE = {"secret-tool": True, "kwallet": False, "dcop": True}
_TOOLS = ("secret-tool", "kwallet-query", "dcop", "kwalletd", "kded", "tdewalletd", "tdewalletmanager",
          "kwalletd5", "kwalletd6", "kded5", "dbus-launch")
_PROMPT_NOTE = ("NOTE: if a wallet password window or a KWallet 'allow access' (Allow once / Always / Deny) "
                "window appears, STOP: the human must answer it (the script waits up to the timeout).")

_secrets: list[str] = []      # throwaway values to scrub from every output
_calls: list[dict[str, Any]] = []   # subprocess call log of the current backend (no values)
_timeout = 20


# --- output with scrubbing ------------------------------------------------------------------

def scrub(text: str) -> str:
    for s in _secrets:
        if s:
            text = text.replace(s, "<REDACTED>")
    return text


def say(text: str = "") -> None:
    print(scrub(text), flush=True)


def fp(value: str | None) -> str:
    """Fingerprint for reports: length and sha256 prefix, never the value."""
    if value is None:
        return "None"
    return f"len={len(value)} sha256={hashlib.sha256(value.encode()).hexdigest()[:8]}"


# --- subprocess helpers ---------------------------------------------------------------------

def run_cmd(args: list[str], stdin: str | None = None, timeout: float | None = None) -> dict[str, Any]:
    """Run one command; never raises. Returns rc/out/err/timeout/error (all scrubbed)."""
    t = _timeout if timeout is None else timeout
    res: dict[str, Any] = {"cmd": scrub(" ".join(args)), "rc": None, "out": "", "err": "",
                           "timeout": False, "error": None}
    try:
        r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=t)
        res.update(rc=r.returncode, out=scrub(r.stdout or ""), err=scrub(r.stderr or ""))
    except subprocess.TimeoutExpired:
        res["timeout"] = True
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        res["error"] = type(exc).__name__
    return res


_LOG_STDOUT = {"open", "writePassword", "removeEntry", "close"}  # int / handle returns; never readPassword


def _dcop_target_method(args: list[str]) -> tuple[str, str]:
    """For ``dcop ...`` argv: (target, method) as DcopStore builds it (kded kwalletd, or kwalletd)."""
    if args[1:3] == ["kded", "kwalletd"]:
        return "kded kwalletd", args[3] if len(args) > 3 else ""
    return "kwalletd", args[2] if len(args) > 2 else ""


def _tracked_run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str] | None:
    """Replacement for keystore._run: longer timeout and a call log.

    The log has the first 5 argv items (for dcop: target, method, handle; the written value is at
    index 6 or later), rc, a timeout flag, stderr, and stdout only for the dcop methods whose output
    is an int / handle (never readPassword, never secret-tool / kwallet-query output).
    """
    is_dcop = bool(args) and args[0] == "dcop"
    target, method = _dcop_target_method(args) if is_dcop else ("", "")
    entry: dict[str, Any] = {"cmd": scrub(" ".join(args[:5] if is_dcop else args[:3])), "target": target,
                             "method": method, "rc": None, "timeout": False, "stderr": "", "stdout": None}
    try:
        r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=_timeout)
    except subprocess.TimeoutExpired:
        entry["timeout"] = True
        _calls.append(entry)
        return None
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        entry["stderr"] = type(exc).__name__
        _calls.append(entry)
        return None
    entry["rc"] = r.returncode
    entry["stderr"] = scrub((r.stderr or "").strip())[:200]
    if is_dcop and method in _LOG_STDOUT:
        entry["stdout"] = scrub((r.stdout or "").strip())[:100]
    _calls.append(entry)
    return r


def _fmt_call(c: dict[str, Any]) -> str:
    tgt = f"[{c['target']}] " if c.get("target") else ""
    if c["timeout"]:
        return f"{tgt}{c['cmd']} -> TIMED OUT after {_timeout}s (wallet password / allow-access window? locked?)"
    out = f" stdout={c['stdout']!r}" if c.get("stdout") is not None else ""
    return f"{tgt}{c['cmd']} -> rc={c['rc']}{out} stderr={c['stderr']!r}"


def _calls_since(mark: int) -> str:
    """All subprocess calls made since index ``mark`` (both dcop targets), for a failed step."""
    cs = _calls[mark:]
    return " | ".join(_fmt_call(c) for c in cs) if cs else "no subprocess call"


def _last_call() -> str:
    return _calls_since(len(_calls) - 1) if _calls else "no subprocess call"


# --- machine report -------------------------------------------------------------------------

def os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k in ("NAME", "VERSION", "PRETTY_NAME", "ID", "VERSION_ID"):
            out[k] = v.strip().strip('"')
    return out


def collect_system() -> dict[str, Any]:
    env = os.environ
    return {
        "platform": sys.platform,
        "os_release": os_release(),
        "python": platform.python_version(),
        "XDG_CURRENT_DESKTOP": env.get("XDG_CURRENT_DESKTOP", ""),
        "XDG_SESSION_TYPE": env.get("XDG_SESSION_TYPE", ""),
        "DBUS_SESSION_BUS_ADDRESS_present": bool(env.get("DBUS_SESSION_BUS_ADDRESS")),
        "DISPLAY_present": bool(env.get("DISPLAY")),
        "SSH_CONNECTION_present": bool(env.get("SSH_CONNECTION")),
        "KIKO_KEYSTORE_IMPL": env.get("KIKO_KEYSTORE_IMPL", ""),
    }


def collect_tools() -> dict[str, str | None]:
    return {t: shutil.which(t) for t in _TOOLS}


def collect_probes() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in ("wcm", "keychain", *BACKENDS):
        fn = getattr(keystore, "_probe_" + name.replace("-", "_"))
        try:
            ok, reason = fn()
        except Exception as exc:  # a probe must never kill the report
            ok, reason = False, f"probe raised {type(exc).__name__}"
        out[name] = {"ok": bool(ok), "reason": str(reason)}
    return out


def collect_factory() -> dict[str, Any]:
    """What factory.choose("keystore") returns auto and with each forced KIKO_KEYSTORE_IMPL."""
    saved = os.environ.get("KIKO_KEYSTORE_IMPL")
    out: dict[str, Any] = {}

    def pick() -> str:
        factory.reset()
        try:
            return type(factory.choose(keystore.KIND)).__name__
        except (LookupError, RuntimeError) as exc:
            return f"ERROR: {exc}"

    try:
        os.environ.pop("KIKO_KEYSTORE_IMPL", None)
        out["auto (KIKO_KEYSTORE_IMPL unset)"] = pick()
        for name in BACKENDS:
            os.environ["KIKO_KEYSTORE_IMPL"] = name
            out[f"KIKO_KEYSTORE_IMPL={name}"] = pick()
    finally:
        if saved is None:
            os.environ.pop("KIKO_KEYSTORE_IMPL", None)
        else:
            os.environ["KIKO_KEYSTORE_IMPL"] = saved
        factory.reset()
    return out


# --- round trip -----------------------------------------------------------------------------

def _step(steps: list[dict[str, Any]], name: str, ok: bool, detail: str = "") -> bool:
    steps.append({"step": name, "ok": bool(ok), "detail": scrub(detail)})
    say(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({scrub(detail)})" if detail else ""))
    return bool(ok)


def round_trip(store: Any, name: str, *, can_delete: bool, keep: bool,
               api_get: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """set -> get -> overwrite -> get -> [api get] -> delete -> get None, with a throwaway value."""
    v1 = "kiko-selftest-" + secrets.token_hex(16)
    v2 = "kiko-selftest-" + secrets.token_hex(16)
    _secrets.extend([v1, v2])
    steps: list[dict[str, Any]] = []
    cleanup = False
    note = ""

    def try_(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # a backend must not crash the check
            return f"EXC:{type(exc).__name__}"

    def calls_if(bad: bool, mark: int) -> str:
        return "; calls: " + _calls_since(mark) if bad else ""

    m = len(_calls)
    set1 = try_(lambda: store.set(name, v1)) is True
    ok = _step(steps, "set value 1", set1, f"value {fp(v1)}" + calls_if(not set1, m))
    if ok:
        cleanup = True
        m = len(_calls)
        g = try_(lambda: store.get(name))
        ok = _step(steps, "get equals value 1", g == v1,
                   f"expected {fp(v1)}, got {fp(g if isinstance(g, str) else None)}" + calls_if(g != v1, m))
        if ok:
            m = len(_calls)
            s2 = try_(lambda: store.set(name, v2)) is True
            ok = _step(steps, "overwrite with value 2", s2, f"value {fp(v2)}" + calls_if(not s2, m))
        if ok:
            m = len(_calls)
            g = try_(lambda: store.get(name))
            ok = _step(steps, "get equals value 2 (not value 1)", g == v2,
                       f"expected {fp(v2)}, got {fp(g if isinstance(g, str) else None)}" + calls_if(g != v2, m))
        if ok and api_get is not None:
            m = len(_calls)
            g = try_(api_get)
            ok = _step(steps, "keystore.get_secret() with KIKO_KEYSTORE_IMPL equals value 2", g == v2,
                       f"got {fp(g if isinstance(g, str) else None)}" + calls_if(g != v2, m))
    else:
        # set failed: it may still have written something; never leave a silent KIKO_SELFTEST entry
        if can_delete:
            m = len(_calls)
            d = try_(lambda: store.delete(name))
            _step(steps, "cleanup delete after failed set", d is True, "" if d is True else calls_if(True, m)[2:])
            if d is not True:
                note = f"manual cleanup needed: entry {name} may exist (set failed, delete failed)"
                say(f"    {note}")
        else:
            note = (f"manual cleanup may be needed: entry {name} in kdewallet/Passwords "
                    "(set failed; this backend cannot delete)")
            say(f"    {note}")
    if cleanup and keep:
        note = f"--keep: entry {name} left in the store; remove it by hand"
        say(f"    {note}")
    elif cleanup and not can_delete:
        note = f"manual cleanup needed: entry {name} in kdewallet/Passwords"
        say(f"    {note}")
    elif cleanup:
        m = len(_calls)
        d = try_(lambda: store.delete(name))
        dok = _step(steps, "delete", d is True, "" if d is True else calls_if(True, m)[2:])
        if dok:
            m = len(_calls)
            g = try_(lambda: store.get(name))
            ok = _step(steps, "get after delete is None", g is None,
                       f"got {fp(g if isinstance(g, str) else None)}" + calls_if(g is not None, m)) and ok
        else:
            ok = False
            note = f"manual cleanup needed: delete failed, entry {name} may remain"
            say(f"    {note}")
    return {"passed": bool(ok) and bool(steps) and all(s["ok"] for s in steps),
            "entry": name, "steps": steps, "cleanup_needed": bool(note), "note": note}


def run_backend(backend: str, keep: bool) -> dict[str, Any]:
    probe_ok, reason = getattr(keystore, _PROBES[backend])()
    res: dict[str, Any] = {"backend": backend, "probe_ok": bool(probe_ok), "probe_reason": reason,
                           "status": "skipped", "passed": False, "steps": [], "note": "",
                           "cleanup_needed": False, "calls": []}
    say(f"  backend {backend}: probe {'ok' if probe_ok else 'NOT ok'} ({reason})")
    if not probe_ok:
        return res
    say(f"  {_PROMPT_NOTE}")
    _calls.clear()
    name = "KIKO_SELFTEST_" + secrets.token_hex(4)
    say(f"    throwaway entry: {name}")
    store = getattr(keystore, _CLASSES[backend])()

    def api_get() -> str | None:
        saved = os.environ.get("KIKO_KEYSTORE_IMPL")
        os.environ["KIKO_KEYSTORE_IMPL"] = backend
        factory.reset()
        try:
            return keystore.get_secret(name, env={})
        finally:
            if saved is None:
                os.environ.pop("KIKO_KEYSTORE_IMPL", None)
            else:
                os.environ["KIKO_KEYSTORE_IMPL"] = saved
            factory.reset()

    rt = round_trip(store, name, can_delete=_CAN_DELETE[backend], keep=keep, api_get=api_get)
    res.update(rt)
    res["status"] = "PASS" if rt["passed"] else "FAIL"
    res["timeouts"] = sum(1 for c in _calls if c["timeout"])
    if res["timeouts"]:
        say(f"    WARNING {res['timeouts']} call(s) timed out after {_timeout}s: a wallet password "
            "window may be waiting, or the daemon is unreachable")
    res["calls"] = list(_calls)
    return res


# --- dcop diagnostics -----------------------------------------------------------------------

_EXPECT = {"open": 2, "readPassword": 3, "writePassword": 4, "removeEntry": 3, "close": 2}
_SIG = re.compile(r"^\s*(?P<ret>[\w:<>*&\s]+?)\s+(?P<m>\w+)\((?P<args>[^)]*)\)", re.M)


def analyze_signatures(text: str) -> dict[str, Any]:
    """Compare a ``functions`` listing with what DcopStore assumes (method names and arg counts)."""
    found: dict[str, list[str]] = {}
    for m in _SIG.finditer(text):
        if m.group("m") in _EXPECT:
            found.setdefault(m.group("m"), []).append(f"{m.group('ret').strip()} {m.group('m')}({m.group('args').strip()})")
    verdict: dict[str, Any] = {}
    for meth, want in _EXPECT.items():
        sigs = found.get(meth, [])
        counts = [len([a for a in s.split("(", 1)[1].rstrip(")").split(",") if a.strip()]) for s in sigs]
        verdict[meth] = {"signatures": sigs, "assumed_args": want, "matches": want in counts}
    return verdict


def dcop_diagnostics() -> dict[str, Any]:
    diag: dict[str, Any] = {"available": bool(shutil.which("dcop")), "commands": [], "functions": {}}
    if not diag["available"]:
        say("  dcop not on PATH: no DCOP diagnostics")
        return diag
    say("  (read-only commands; " + _PROMPT_NOTE.removeprefix("NOTE: ") + ")")

    def do(args: list[str]) -> dict[str, Any]:
        r = run_cmd(args)
        diag["commands"].append(r)
        state = "TIMEOUT" if r["timeout"] else (f"error {r['error']}" if r["error"] else f"rc={r['rc']}")
        say(f"  $ {r['cmd']}   -> {state}")
        for line in (r["out"] + (("\n[stderr] " + r["err"]) if r["err"].strip() else "")).strip().splitlines()[:60]:
            say(f"      {line}")
        return r

    apps = do(["dcop"])
    kded_objs = do(["dcop", "kded"])
    diag["kded_exposes_kwalletd"] = any(l.strip() == "kwalletd" for l in kded_objs["out"].splitlines())
    say(f"  kded exposes 'kwalletd': {diag['kded_exposes_kwalletd']}")
    targets: list[tuple[str, str]] = [("kded", "kwalletd")]
    # any other wallet-looking object (TDE may rename it, e.g. tdewalletd)
    for line in kded_objs["out"].splitlines():
        s = line.strip()
        if "wallet" in s.lower() and ("kded", s) not in targets:
            targets.append(("kded", s))
    for line in apps["out"].splitlines():
        s = line.strip()
        if "wallet" in s.lower() and not s.startswith("kded"):
            objs = do(["dcop", s])
            for o in objs["out"].splitlines():
                if o.strip():
                    targets.append((s, o.strip()))
    for app, obj in targets:
        r = do(["dcop", app, obj, "functions"])
        diag["functions"][f"{app} {obj}"] = {"rc": r["rc"], "analysis": analyze_signatures(r["out"])}
    for meth in ("isEnabled", "wallets", "localWallet"):
        do(["dcop", "kded", "kwalletd", meth])
    say("  DcopStore calls 'kded kwalletd <method>' first, then 'kwalletd <method>'; "
        "signature comparison per target:")
    shown = False
    for label, fv in diag["functions"].items():
        if fv["rc"] != 0:
            continue
        shown = True
        say(f"  DcopStore assumptions vs 'functions' output of target [{label}]:")
        for meth, v in fv["analysis"].items():
            say(f"    {meth:<14} assumed {v['assumed_args']} args: {'OK' if v['matches'] else 'MISMATCH/MISSING'}"
                f"  {v['signatures']}")
    if not shown:
        say("  no 'functions' listing succeeded: no signature comparison possible")
    return diag


# --- main -----------------------------------------------------------------------------------

def _table(results: list[dict[str, Any]]) -> None:
    say(f"  {'backend':<12} {'probe':<6} {'result':<8} note")
    for r in results:
        say(f"  {r['backend']:<12} {'ok' if r['probe_ok'] else 'no':<6} {r['status']:<8} {r['note'] or r['probe_reason']}")


def main(argv: list[str] | None = None) -> int:
    global _timeout
    ap = argparse.ArgumentParser(description="Live check of the Linux keystore backends of kikolib.")
    ap.add_argument("--backend", default="auto", choices=("auto", *BACKENDS))
    ap.add_argument("--json", metavar="FILE", help="write the full machine-readable result here")
    ap.add_argument("--keep", action="store_true", help="do not delete the throwaway entry")
    ap.add_argument("--timeout", type=float, default=20.0, help="per subprocess call, seconds (default 20)")
    args = ap.parse_args(argv)
    _timeout = args.timeout
    _secrets.clear()

    result: dict[str, Any] = {"tool_version": TOOL_VERSION, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                              "backend_arg": args.backend, "keep": args.keep}
    say("== System ==")
    result["system"] = collect_system()
    for k, v in result["system"].items():
        say(f"  {k}: {v}")
    na = sys.platform == "win32"
    say("== Tools on PATH ==")
    result["tools"] = collect_tools()
    for k, v in result["tools"].items():
        say(f"  {k:<18} {v or '-'}")
    say("== Probes (kikolib.keystore) ==")
    result["probes"] = collect_probes()
    for k, v in result["probes"].items():
        say(f"  {k:<12} {'ok' if v['ok'] else 'no':<3} {v['reason']}")
    say("== Factory choice ==")
    result["factory"] = collect_factory()
    for k, v in result["factory"].items():
        say(f"  {k}: {v}")

    results: list[dict[str, Any]] = []
    if na:
        say("== Round trip ==")
        say("  not applicable: Windows (the Linux backends are checked on Linux only)")
        results = [{"backend": b, "probe_ok": False, "probe_reason": "not applicable (Windows)",
                    "status": "n/a", "passed": False, "steps": [], "note": "", "cleanup_needed": False,
                    "calls": []} for b in BACKENDS]
        result["dcop_diagnostics"] = {"available": False, "note": "not applicable (Windows)"}
    else:
        orig_run = keystore._run
        keystore._run = _tracked_run  # longer timeout + call log; module-level hook of keystore
        say("== Round trip (throwaway entry, values never printed) ==")
        chosen = BACKENDS if args.backend == "auto" else (args.backend,)
        try:
            for b in chosen:
                results.append(run_backend(b, args.keep))
        finally:
            keystore._run = orig_run
        if args.backend != "auto" and not results[0]["probe_ok"]:
            results[0]["note"] = "requested backend is not available"
        say("== dcop diagnostics ==")
        if args.backend in ("auto", "dcop"):
            result["dcop_diagnostics"] = dcop_diagnostics()
        else:
            result["dcop_diagnostics"] = {"skipped": True}
            say("  skipped (--backend is not dcop or auto)")
    result["backends"] = results
    passed = [r["backend"] for r in results if r["passed"]]
    result["passed_backends"] = passed
    result["manual_cleanup"] = [r["note"] for r in results if r.get("cleanup_needed")]
    result["exit_code"] = 0 if passed else 1
    say("== Summary ==")
    _table(results)
    for n in result["manual_cleanup"]:
        say(f"  {n}")
    for r in results:
        if r["status"] == "FAIL" and r.get("calls"):
            say(f"  subprocess calls of failed backend {r['backend']} (target, argv head, result):")
            for c in r["calls"][-30:]:
                say(f"    {_fmt_call(c)}")
    if na:
        say("  RESULT: NOT APPLICABLE (Windows); exit code 1")
    else:
        say(f"  RESULT: {'PASS (' + ', '.join(passed) + ')' if passed else 'FAIL: no backend passed the full round trip'}")
    if args.json:
        try:
            Path(args.json).write_text(scrub(json.dumps(result, indent=2, ensure_ascii=False)), encoding="utf-8")
            say(f"  JSON written: {args.json}")
        except OSError as exc:
            say(f"  could not write JSON: {type(exc).__name__}")
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
