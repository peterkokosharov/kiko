# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""API keys straight from the OS secret store (no third-party keyring).

Backends (factory kind "keystore", override with env KIKO_KEYSTORE_IMPL):
  wcm         Windows Credential Manager via ctypes/advapi32; target KIKO/KIKO_IDENTITY/<NAME>
  keychain    macOS ``security`` CLI (service KIKO, account <NAME>)
  secret-tool GNOME Keyring / KDE via libsecret; attribute ``kiko-identity <NAME>``
  kwallet     KDE ``kwallet-query`` (wallet kdewallet, folder Passwords)
  dcop        TDE / Q4OS kwalletd over DCOP -- UNVERIFIED on live TDE

Every backend has get(name) -> str | None, set(name, value) -> bool, delete(name) -> bool.
Names are upper-cased. Subprocess calls use list args (no shell), a timeout (env
KIKO_KEYSTORE_TIMEOUT seconds, default 15: a locked wallet may show an unlock prompt) and
swallow errors; a timeout is counted (``timeout_count``) so callers can say "keystore did not
answer" instead of "not found". Secret values are never logged or put in exceptions.

Known limits: the macOS ``security add-generic-password -w <value>`` and the TDE
``dcop ... writePassword`` take the value as a command-line argument (visible in the
process list while they run); neither tool offers a safe alternative. secret-tool and
kwallet-query read the value from stdin. kwallet-query cannot delete entries.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Mapping

from . import factory

KIND = "keystore"
DEFAULT_TIMEOUT = 15.0
TIMEOUT_ENV = "KIKO_KEYSTORE_TIMEOUT"
NO_ANSWER = "keystore did not answer (locked? unlock prompt open?)"
_timeouts = 0
_timeouts_lock = threading.Lock()


def timeout_seconds() -> float:
    """Seconds a keystore tool may take: env KIKO_KEYSTORE_TIMEOUT, else 15 (bad / non-positive values: 15)."""
    try:
        v = float(os.environ.get(TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_TIMEOUT
    return v if v > 0 and v == v and v != float("inf") else DEFAULT_TIMEOUT


def timeout_count() -> int:
    """How many keystore tool calls ran into the timeout since start (compare before / after a lookup)."""
    return _timeouts


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str] | None:
    global _timeouts
    try:
        return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout_seconds())
    except subprocess.TimeoutExpired:
        with _timeouts_lock:
            _timeouts += 1
        return None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _out(r: subprocess.CompletedProcess[str] | None) -> str | None:
    if r is not None and r.returncode == 0 and r.stdout and r.stdout.strip():
        return r.stdout.strip()
    return None


def _ok(r: subprocess.CompletedProcess[str] | None) -> bool:
    return r is not None and r.returncode == 0


# --- Windows Credential Manager (ctypes call layer, patched in tests) ---------------------

def _wcm_struct() -> Any:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    return CREDENTIAL


def _wcm_read(target: str) -> str | None:
    import ctypes

    cred_t = _wcm_struct()
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.c_void_p()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)):  # CRED_TYPE_GENERIC
        return None
    try:
        cred = ctypes.cast(ptr, ctypes.POINTER(cred_t)).contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        adv.CredFree(ptr)
    return blob.decode("utf-16-le") or None


def _wcm_write(target: str, value: str) -> bool:
    import ctypes

    cred_t = _wcm_struct()
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
    cred = cred_t()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "kiko"
    return bool(adv.CredWriteW(ctypes.byref(cred), 0))


def _wcm_delete(target: str) -> bool:
    import ctypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    return bool(adv.CredDeleteW(target, 1, 0))


class WcmStore:
    """Windows Credential Manager."""

    @staticmethod
    def _target(name: str) -> str:
        return f"KIKO/KIKO_IDENTITY/{name.upper()}"

    def get(self, name: str) -> str | None:
        try:
            return _wcm_read(self._target(name))
        except Exception:
            return None

    def set(self, name: str, value: str) -> bool:
        try:
            return _wcm_write(self._target(name), value)
        except Exception:
            return False

    def delete(self, name: str) -> bool:
        try:
            return _wcm_delete(self._target(name))
        except Exception:
            return False


class KeychainStore:
    """macOS Keychain via the ``security`` CLI. set() puts the value in argv (unavoidable)."""

    def get(self, name: str) -> str | None:
        return _out(_run(["security", "find-generic-password", "-s", "KIKO", "-a", name.upper(), "-w"]))

    def set(self, name: str, value: str) -> bool:
        return _ok(_run(["security", "add-generic-password", "-s", "KIKO", "-a", name.upper(), "-w", value, "-U"]))

    def delete(self, name: str) -> bool:
        return _ok(_run(["security", "delete-generic-password", "-s", "KIKO", "-a", name.upper()]))


class SecretToolStore:
    """libsecret via ``secret-tool`` (needs a D-Bus session)."""

    def get(self, name: str) -> str | None:
        return _out(_run(["secret-tool", "lookup", "kiko-identity", name.upper()]))

    def set(self, name: str, value: str) -> bool:
        up = name.upper()
        return _ok(_run(["secret-tool", "store", "--label", f"kiko {up}", "kiko-identity", up], stdin=value))

    def delete(self, name: str) -> bool:
        return _ok(_run(["secret-tool", "clear", "kiko-identity", name.upper()]))


class KWalletStore:
    """KDE Wallet via ``kwallet-query`` (wallet kdewallet, folder Passwords)."""

    def get(self, name: str) -> str | None:
        return _out(_run(["kwallet-query", "-r", name.upper(), "-f", "Passwords", "kdewallet"]))

    def set(self, name: str, value: str) -> bool:
        return _ok(_run(["kwallet-query", "-w", name.upper(), "-f", "Passwords", "kdewallet"], stdin=value))

    def delete(self, name: str) -> bool:
        return False  # kwallet-query has no delete


class DcopStore:
    """TDE / Q4OS (KDE3) kwalletd over DCOP. UNVERIFIED on live TDE.

    In KDE3/TDE kwalletd lives inside kded, so ``dcop kded kwalletd <method>`` is tried
    first and ``dcop kwalletd <method>`` second. KDE3 signatures: open(wallet, wId) -> handle,
    readPassword(handle, folder, key) -> str, writePassword(handle, folder, key, value) -> 0 on
    success, removeEntry(handle, folder, key) -> 0 on success, close(handle, force). The printed
    return value is checked as well as the exit code. set() puts the value in argv (unavoidable).
    """

    _TARGETS = (["kded", "kwalletd"], ["kwalletd"])

    def _call(self, method: str, *args: str) -> str | None:
        """Run one wallet method; return its stripped stdout, or None on any failure."""
        for target in self._TARGETS:
            raw = _out(_run(["dcop", *target, "open", "kdewallet", "0"]))
            try:
                handle = int(raw) if raw is not None else -1
            except ValueError:
                handle = -1
            if handle < 0:
                continue
            try:
                r = _run(["dcop", *target, method, str(handle), "Passwords", *args])
            finally:
                _run(["dcop", *target, "close", str(handle), "0"])
            if r is not None and r.returncode == 0:
                return (r.stdout or "").strip()
        return None

    def get(self, name: str) -> str | None:
        out = self._call("readPassword", name.upper())
        return out if out and out != "-1" else None

    def set(self, name: str, value: str) -> bool:
        return self._call("writePassword", name.upper(), value) == "0"

    def delete(self, name: str) -> bool:
        return self._call("removeEntry", name.upper()) == "0"


# --- probes and registration --------------------------------------------------------------

def _probe_wcm() -> tuple[bool, str]:
    return (True, "Windows Credential Manager") if sys.platform == "win32" else (False, "not Windows")


def _probe_keychain() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "not macOS"
    return (True, "security found") if shutil.which("security") else (False, "security not on PATH")


def _probe_secret_tool() -> tuple[bool, str]:
    if sys.platform == "win32":
        return False, "Windows"
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False, "no DBUS_SESSION_BUS_ADDRESS"
    return (True, "secret-tool found") if shutil.which("secret-tool") else (False, "secret-tool not on PATH")


def _probe_kwallet() -> tuple[bool, str]:
    if sys.platform == "win32":
        return False, "Windows"
    return (True, "kwallet-query found") if shutil.which("kwallet-query") else (False, "kwallet-query not on PATH")


def _probe_dcop() -> tuple[bool, str]:
    if sys.platform == "win32":
        return False, "Windows"
    return (True, "dcop found") if shutil.which("dcop") else (False, "dcop not on PATH")


def register_all() -> None:
    """Register the backends with the factory (done at import; call again after factory.clear())."""
    for name, probe, cls, prio in (
        ("wcm", _probe_wcm, WcmStore, 10),
        ("keychain", _probe_keychain, KeychainStore, 10),
        ("secret-tool", _probe_secret_tool, SecretToolStore, 20),
        ("kwallet", _probe_kwallet, KWalletStore, 30),
        ("dcop", _probe_dcop, DcopStore, 40),
    ):
        factory.register(KIND, name, probe=probe, load=cls, priority=prio)


register_all()


def _store() -> Any | None:
    try:
        return factory.choose(KIND)
    except (LookupError, RuntimeError):
        return None


def get_secret(name: str, env: Mapping[str, str] = os.environ) -> str | None:
    """Secret ``name``: environment variable first, then the OS stores; else None.

    Without KIKO_KEYSTORE_IMPL every probe-ok store is tried in priority order until one
    returns a value (a store that is present but unusable, e.g. no daemon, is skipped).
    With KIKO_KEYSTORE_IMPL only that store is used.
    """
    v = env.get(name) or env.get(name.upper())
    if v:
        return v
    if os.environ.get("KIKO_KEYSTORE_IMPL"):
        stores = [_store()]
    else:
        stores = [impl for _, impl in factory.available(KIND)]
    for store in stores:
        if store is None:
            continue
        try:
            value = store.get(name)
        except Exception:
            value = None
        if value:
            return value
    return None


def set_secret(name: str, value: str) -> bool:
    """Store ``value`` in the first probe-ok (or overridden) store. False if none or it failed."""
    store = _store()
    return bool(store.set(name, value)) if store is not None else False
