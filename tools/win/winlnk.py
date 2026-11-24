# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Create, read and delete Windows .lnk shortcuts with ctypes COM (standard library only).

    create_shortcut(lnk_path, target, args="", workdir="", description="") -> dict
    read_shortcut(path) -> dict   (target, args, workdir, description, kind)
    delete_shortcut(path)

COM objects used: CLSID_ShellLink, IShellLinkW, IPersistFile. After saving, the .lnk is read back and
compared with what was asked for. If COM fails or the read-back differs, a .url file is written instead
(kind "url"; a warning is logged, and the returned "path" then ends in .url, so callers must record
res["path"], not the .lnk path they asked for). Windows only: on other systems every function raises OSError.
"""
import ctypes
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                    ("d", ctypes.c_ubyte * 8)]

    _ole32 = ctypes.OleDLL("ole32")
    _ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    _ole32.IIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]

    def _guid(s: str) -> "GUID":
        g = GUID()
        _ole32.IIDFromString(s, ctypes.byref(g))
        return g

    CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
    IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
    IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"

    # vtable slots (IUnknown = 0..2)
    _QI, _RELEASE = 0, 2
    _SL = {"GetPath": 3, "GetDescription": 6, "SetDescription": 7, "GetWorkingDirectory": 8,
           "SetWorkingDirectory": 9, "GetArguments": 10, "SetArguments": 11, "SetPath": 20}
    _PF = {"Load": 5, "Save": 6}

    class _Obj:
        """Minimal COM interface pointer: call(slot, argtypes, *args)."""

        def __init__(self, ptr):
            self.ptr = ctypes.c_void_p(ptr)

        def call(self, slot, argtypes, *args):
            vtbl = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[slot])
            return fn(self.ptr, *args)

        def query(self, iid):
            out = ctypes.c_void_p()
            self.call(_QI, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(_guid(iid)), ctypes.byref(out))
            return _Obj(out.value)

        def release(self):
            vtbl = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[_RELEASE])(self.ptr)

    def _new_link() -> "_Obj":
        ptr = ctypes.c_void_p()
        _ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_ShellLink)), None, 1,
                                ctypes.byref(_guid(IID_IShellLinkW)), ctypes.byref(ptr))
        return _Obj(ptr.value)

    def _com_write(path, target, args, workdir, description):
        sl = _new_link()
        pf = None
        try:
            pf = sl.query(IID_IPersistFile)
            W = [wintypes.LPCWSTR]
            sl.call(_SL["SetPath"], W, target)
            sl.call(_SL["SetArguments"], W, args)
            sl.call(_SL["SetWorkingDirectory"], W, workdir)
            sl.call(_SL["SetDescription"], W, description)
            pf.call(_PF["Save"], [wintypes.LPCWSTR, wintypes.BOOL], path, True)
        finally:
            if pf is not None:
                pf.release()
            sl.release()

    def _com_read(path):
        sl = _new_link()
        pf = None
        try:
            pf = sl.query(IID_IPersistFile)
            pf.call(_PF["Load"], [wintypes.LPCWSTR, wintypes.DWORD], path, 0)
            res = {}
            for key, slot in (("target", "GetPath"), ("args", "GetArguments"),
                              ("workdir", "GetWorkingDirectory"), ("description", "GetDescription")):
                buf = ctypes.create_unicode_buffer(1024)
                if slot == "GetPath":
                    sl.call(_SL[slot], [wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                            buf, 1024, None, 0)
                else:
                    sl.call(_SL[slot], [wintypes.LPWSTR, ctypes.c_int], buf, 1024)
                res[key] = buf.value
            return res
        finally:
            if pf is not None:
                pf.release()
            sl.release()

    class _Com:
        def __enter__(self):
            _ole32.CoInitialize(None)

        def __exit__(self, *a):
            _ole32.CoUninitialize()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _write_url(path: Path, target: str) -> Path:
    url = path.with_suffix(".url")
    url.write_text(f"[InternetShortcut]\r\nURL={Path(target).as_uri()}\r\n", encoding="utf-8")
    return url


def create_shortcut(lnk_path, target, args="", workdir="", description="") -> dict:
    """Write lnk_path (.lnk), verify it by reading it back, else fall back to a .url file.
    Returns {"kind": "lnk"|"url", "path": <actual file>}."""
    if not IS_WINDOWS:
        raise OSError("winlnk: Windows only")
    path = Path(lnk_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _Com():
            _com_write(str(path), str(target), args, workdir, description)
            back = _com_read(str(path))
        if _norm(back["target"]) != _norm(str(target)) or back["args"] != args:
            raise OSError(f"read-back mismatch: {back}")
        return {"kind": "lnk", "path": str(path)}
    except (OSError, ValueError) as ex:
        log.warning("winlnk: .lnk failed for %s (%s); writing a .url file instead", path, ex)
        if path.exists():
            path.unlink()
        return {"kind": "url", "path": str(_write_url(path, str(target)))}


def read_shortcut(path) -> dict:
    path = Path(path)
    if path.suffix.lower() == ".url":
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("URL="):
                return {"kind": "url", "target": line[4:], "args": "", "workdir": "", "description": ""}
        raise OSError("no URL= line")
    if not IS_WINDOWS:
        raise OSError("winlnk: Windows only")
    with _Com():
        res = _com_read(str(path))
    res["kind"] = "lnk"
    return res


def delete_shortcut(path) -> bool:
    p = Path(path)
    if p.exists():
        p.unlink()
        return True
    return False
