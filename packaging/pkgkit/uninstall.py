# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Standalone uninstaller, copied to <app folder>/_pkg/uninstall.py (no repo, no other pkgkit file needed).

    python -I uninstall.py [--app DIR] [--purge]

Reads install-record.json in the app folder (default: the parent of this script's folder) and removes exactly
what it lists. Order: shortcuts, files, empty dirs. A file whose sha256 changed is KEPT and reported; a file that
stays locked after retries is KEPT (locked). data/ and logs/ are removed only with --purge. uninstall.cmd cannot
be deleted by Python while cmd runs it; it is skipped here and the .cmd deletes itself and the folder.
Exit code: 0 = clean, 1 = something was kept, skipped or failed, 2 = no record.
Standard library only.
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

RECORD = "install-record.json"
CMD_NAME = "uninstall.cmd"
RUNTIME_DIRS = ("data", "logs")


def appdata_root() -> Path:
    v = os.environ.get("APPDATA")
    return Path(v) if v else Path.home() / ".local" / "share"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _is_link(p: Path) -> bool:
    if os.path.islink(p):
        return True
    j = getattr(p, "is_junction", None)  # Python 3.12+; on 3.11 junctions are not detected here
    return bool(j and j())


def _inside(root: Path, rel) -> "Path | None":
    """root/rel, or None when rel is unsafe: not a plain relative path, has '..', is empty, leaves root once
    resolved, or passes through a symlink/junction below root."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or Path(rel).is_absolute() \
            or Path(rel).drive or Path(rel).anchor:
        return None
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    root = Path(root)
    p = root.joinpath(*parts)
    try:
        if not p.resolve().is_relative_to(root.resolve()):
            return None
    except (OSError, ValueError):
        return None
    cur = root
    for x in parts:
        cur = cur / x
        if _is_link(cur):
            return None
    return p


def shortcut_root() -> Path:
    return appdata_root() / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def shortcut_path(rel) -> "Path | None":
    """A recorded shortcut path (relative to %APPDATA%), only if it lies in the Start Menu Programs folder."""
    p = _inside(appdata_root(), rel)
    if p is None:
        return None
    try:
        if not p.resolve().is_relative_to(shortcut_root().resolve()):
            return None
    except (OSError, ValueError):
        return None
    return p


def unlink_retry(p: Path, tries: int = 10, delay: float = 0.2) -> bool:
    """Delete p; a briefly locked file (Defender, the indexer) is retried. False if still locked."""
    for i in range(tries):
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if i < tries - 1:
                time.sleep(delay)
    return False


def uninstall(app_dir: Path, purge: bool = False, out=print, tries: int = 10) -> int:
    app_dir = Path(app_dir)
    rec_path = app_dir / RECORD
    if not rec_path.is_file():
        out(f"no {RECORD} in {app_dir}")
        return 2
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    created = [e for e in rec.get("created", []) if isinstance(e, dict)]
    me = Path(__file__).resolve()
    kept, bad = [], []
    sc_root = shortcut_root().resolve()
    for e in (e for e in created if e.get("kind") == "shortcut"):
        p = shortcut_path(e.get("path"))
        if p is None:
            bad.append(e.get("path"))
            out(f"SKIPPED (unsafe shortcut path): {e.get('path')!r}")
            continue
        if p.exists():
            if not unlink_retry(p, tries):
                kept.append(str(e.get("path")))
                out(f"KEPT (locked): {e.get('path')}")
                continue
            out(f"removed shortcut {p.name}")
            try:
                if p.parent.resolve() != sc_root:
                    p.parent.rmdir()  # only if empty, never the Programs folder itself
            except OSError:
                pass
    own = None
    for e in (e for e in created if e.get("kind") == "file"):
        p = _inside(app_dir, e.get("path"))
        if p is None:
            bad.append(e.get("path"))
            out(f"SKIPPED (unsafe path): {e.get('path')!r}")
            continue
        if e["path"] in (RECORD, CMD_NAME) or not p.exists():
            continue
        if p.resolve() == me:
            own = (e, p)  # this very script goes last
            continue
        if not e.get("sha256"):
            bad.append(e["path"])
            out(f"SKIPPED (no sha256 in record): {e['path']}")
            continue
        if not p.is_file() or sha256_file(p) != e["sha256"]:
            kept.append(e["path"])
            out(f"KEPT (modified): {e['path']}")
            continue
        if not unlink_retry(p, tries):
            kept.append(e["path"])
            out(f"KEPT (locked): {e['path']}")
    if own is not None:
        e, p = own
        if sha256_file(p) != e.get("sha256"):
            kept.append(e["path"])
            out(f"KEPT (modified): {e['path']}")
        elif not kept and not bad:
            try:
                p.unlink()
            except OSError:
                kept.append(e["path"])
                out(f"KEPT (locked): {e['path']}")
    dirs = sorted((e for e in created if e.get("kind") == "dir"),
                  key=lambda e: -len(str(e.get("path")).replace("\\", "/").split("/")))
    for e in dirs:
        d = _inside(app_dir, e.get("path"))
        if d is None:
            bad.append(e.get("path"))
            out(f"SKIPPED (unsafe dir path): {e.get('path')!r}")
            continue
        if d.is_dir():
            for c in d.rglob("__pycache__"):
                if not _is_link(c):
                    shutil.rmtree(c, ignore_errors=True)
            try:
                d.rmdir()
            except OSError:
                if not any(k.startswith(e["path"] + "/") for k in kept):
                    out(f"left (not empty): {e['path']}")
    for name in RUNTIME_DIRS:
        d = app_dir / name
        if _is_link(d):
            out(f"SKIPPED {name}: it is a link or junction, not touched")
        elif d.is_dir():
            if purge:
                shutil.rmtree(d)
                out(f"purged {name}")
            else:
                out(f"kept {name} (use /purge to remove)")
    if not kept and not bad:
        rec_path.unlink()
        left = [x.name for x in app_dir.iterdir()]
        if not left:
            app_dir.rmdir()
            out(f"removed {app_dir}")
        elif left == [CMD_NAME]:
            out(f"removed everything listed in the record; {CMD_NAME} deletes itself and the folder")
        else:
            out(f"app folder kept (not empty: {', '.join(sorted(left))}): {app_dir}")
    else:
        out("not fully removed: the KEPT items stay; the record stays so uninstall can be run again")
    return 1 if (kept or bad) else 0


def main(argv: list) -> int:
    purge = any(a.lower() in ("--purge", "/purge") for a in argv)
    app = Path(__file__).resolve().parent.parent
    if "--app" in argv:
        app = Path(argv[argv.index("--app") + 1])
    return uninstall(app, purge)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
