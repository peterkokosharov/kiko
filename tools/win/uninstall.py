# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Standalone uninstaller, copied into every app folder (no repo needed).

    python -I uninstall.py [--purge]

Reads install-record.json next to this script. Order: shortcuts, files, empty dirs. A file whose
sha256 changed is kept and reported. data/ and logs/ are removed only with --purge.
Standard library only; works on any OS (shortcut paths are expanded from %APPDATA% or ~/.local).
"""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

RECORD = "install-record.json"
RUNTIME_DIRS = ("data", "logs")


def appdata_root() -> Path:
    v = os.environ.get("APPDATA")
    return Path(v) if v else Path.home() / ".local" / "share"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def uninstall(app_dir: Path, purge: bool = False, out=print) -> int:
    """Remove what the record lists. Returns 0 if clean, 1 if something was kept or failed."""
    app_dir = Path(app_dir)
    rec_path = app_dir / RECORD
    if not rec_path.is_file():
        out(f"no {RECORD} in {app_dir}")
        return 2
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    created = rec.get("created", [])
    kept = []
    for e in (e for e in created if e["kind"] == "shortcut"):
        p = appdata_root() / e["path"]
        if p.exists():
            p.unlink()
            out(f"removed shortcut {p.name}")
            try:
                p.parent.rmdir()  # only if empty
            except OSError:
                pass
    files = [e for e in created if e["kind"] == "file"]
    for e in files:
        p = app_dir / e["path"]
        if e["path"] in (RECORD, "uninstall.py") or not p.exists():
            continue
        if e.get("sha256") and sha256_file(p) != e["sha256"]:
            kept.append(e["path"])
            out(f"KEPT (modified): {e['path']}")
            continue
        p.unlink()
    dirs = sorted((e for e in created if e["kind"] == "dir"), key=lambda e: -len(Path(e["path"]).parts))
    for e in dirs:
        d = app_dir / e["path"]
        if d.is_dir():
            for c in d.rglob("__pycache__"):
                shutil.rmtree(c, ignore_errors=True)
            try:
                d.rmdir()
            except OSError:
                if not any(k.startswith(e["path"] + "/") for k in kept):
                    out(f"left (not empty): {e['path']}")
    for name in RUNTIME_DIRS:
        d = app_dir / name
        if d.is_dir():
            if purge:
                shutil.rmtree(d)
                out(f"purged {name}")
            else:
                out(f"kept {name} (use --purge to remove)")
    if not kept:
        rec_path.unlink()
        sp = app_dir / "uninstall.py"
        if sp.exists():
            try:
                sp.unlink()
            except OSError:
                pass
        shutil.rmtree(app_dir / "__pycache__", ignore_errors=True)
        try:
            app_dir.rmdir()
            out(f"removed {app_dir}")
        except OSError:
            out(f"app folder kept (not empty): {app_dir}")
    return 1 if kept else 0


def main(argv) -> int:
    purge = "--purge" in argv
    return uninstall(Path(__file__).resolve().parent, purge)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
