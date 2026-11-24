# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Portable install core: copy, record, verify, uninstall (standard library only).

An app lives in one folder (default ~/apps/<name>). install-record.json lists everything the installer
created, so uninstall.py (copied into the folder) can remove exactly that. See
docs/WINDOWS-PORTABLE-INSTALL.md.
"""
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import uninstall as _un  # noqa: E402  (the same module that is copied into every app folder)

RECORD = _un.RECORD
UNINSTALL_NAME = "uninstall.py"
SHORTCUT_SUBDIR = "Microsoft/Windows/Start Menu/Programs/apps"  # relative to %APPDATA%


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def default_dest(name: str) -> Path:
    return Path.home() / "apps" / name


class Installation:
    """Collects what is created inside one app folder, then writes install-record.json."""

    def __init__(self, app: str, version: str, dest: Path, repo: str = "", commit: str = ""):
        self.app, self.version, self.dest = app, version, Path(dest)
        self.source = {"repo": repo, "commit": commit}
        self.created: list[dict] = []

    def _dir(self, rel: str):
        d = self.dest / rel
        if not d.exists():
            d.mkdir(parents=True)
            self.created.append({"path": rel.replace("\\", "/"), "kind": "dir", "sha256": ""})

    def ensure_dirs(self, rel: str):
        """Create rel and any missing parents, recording each new one."""
        parts = Path(rel).parts
        for i in range(1, len(parts) + 1):
            self._dir("/".join(parts[:i]))

    def add_file(self, rel: str, data: bytes):
        rel = rel.replace("\\", "/")
        if "/" in rel:
            self.ensure_dirs(rel.rsplit("/", 1)[0])
        (self.dest / rel).write_bytes(data)
        self.created.append({"path": rel, "kind": "file", "sha256": sha256_bytes(data)})

    def add_tree(self, rel: str):
        """Record a folder that will be filled by another tool (a private venv): uninstall removes it whole.
        Call before creating it, so a failed install is rolled back too."""
        self.created.append({"path": rel.replace("\\", "/"), "kind": "tree", "sha256": ""})

    def add_shortcut(self, appdata_rel: str):
        """Record a shortcut (path relative to %APPDATA%) that was already created."""
        self.created.append({"path": appdata_rel.replace("\\", "/"), "kind": "shortcut", "sha256": ""})

    def finish(self) -> Path:
        self.dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / "uninstall.py", self.dest / UNINSTALL_NAME)
        self.created.append({"path": UNINSTALL_NAME, "kind": "file",
                             "sha256": sha256_bytes((self.dest / UNINSTALL_NAME).read_bytes())})
        rec = {"app": self.app, "version": self.version, "source": self.source,
               "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "python": sys.version.split()[0], "created": self.created}
        p = self.dest / RECORD
        p.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        return p

    def rollback(self):
        """Undo a failed install: remove everything collected so far."""
        for e in sorted(self.created, key=lambda e: -len(e["path"])):
            p = _un._inside(self.dest, e["path"])
            if p is None:
                continue
            if e["kind"] == "file" and p.is_file():
                p.unlink()
            elif e["kind"] in ("dir", "tree") and p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        for e in self.created:
            if e["kind"] == "shortcut":
                sp = _un._shortcut_path(e["path"])
                if sp is not None:
                    sp.unlink(missing_ok=True)
        for n in (UNINSTALL_NAME, RECORD):
            (self.dest / n).unlink(missing_ok=True)
        try:
            self.dest.rmdir()
        except OSError:
            pass


def load_record(dest: Path) -> dict:
    return json.loads((Path(dest) / RECORD).read_text(encoding="utf-8"))


def verify(dest: Path) -> list[str]:
    """Problems found comparing the record with the disk (empty list = intact)."""
    dest = Path(dest)
    problems = []
    for e in load_record(dest)["created"]:
        if e.get("kind") == "shortcut":
            if _un._shortcut_path(e.get("path")) is None:
                problems.append(f"unsafe path: {e.get('path')!r}")
            continue
        if e.get("kind") in ("dir", "tree"):
            if _un._inside(dest, e.get("path")) is None:
                problems.append(f"unsafe path: {e.get('path')!r}")
            continue
        if e.get("kind") != "file":
            continue
        p = _un._inside(dest, e.get("path"))
        if p is None:
            problems.append(f"unsafe path: {e.get('path')!r}")
        elif not p.is_file():
            problems.append(f"missing: {e['path']}")
        elif not e.get("sha256") and _un.needs_sha(e):
            problems.append(f"no sha256: {e['path']}")
        elif e.get("sha256") and _un.sha256_file(p) != e["sha256"]:
            problems.append(f"changed: {e['path']}")
    return problems


def uninstall(dest: Path, purge: bool = False, out=print) -> int:
    return _un.uninstall(Path(dest), purge, out)
