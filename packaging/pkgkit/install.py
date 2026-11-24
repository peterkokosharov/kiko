# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Installer run by install.cmd from the unzipped package:  python -I _pkg\\install.py [--dest DIR]

Reads _pkg/app.json (written by build.py), then: checks Windows 10+ and Python, checks payload/ against
_pkg/MANIFEST (sha256), copies the payload to %USERPROFILE%\\apps\\<name> (or --dest), copies uninstall.cmd and
_pkg/uninstall.py there, creates the Start-menu shortcut under Programs\\apps, writes install-record.json and runs
the app's own check command. Prints "OK <name> <ver> installed in <dir>" or "FAILED: <reason>" (and rolls back).
Standard library only.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))  # python -I does not put the script folder on sys.path
import manifest  # noqa: E402
import uninstall as un  # noqa: E402
import winlnk  # noqa: E402

SHORTCUT_DIR = "Microsoft/Windows/Start Menu/Programs/apps"  # relative to %APPDATA%


class Failed(Exception):
    pass


def sha256_bytes(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


class Installation:
    """Collects what is created, so the record and a rollback list exactly the same things."""

    def __init__(self, app: dict, dest: Path):
        self.app, self.dest = app, Path(dest)
        self.created: list = []
        self.made_dest = False

    def start(self):
        if self.dest.exists():
            if (self.dest / un.RECORD).exists():
                raise Failed(f"{self.app['name']} is already installed in {self.dest}; run its uninstall.cmd first")
            if any(self.dest.iterdir()):
                raise Failed(f"{self.dest} exists and is not empty; it is not touched")
        else:
            self.dest.mkdir(parents=True)
            self.made_dest = True

    def _dir(self, rel: str):
        d = self.dest / rel
        if not d.exists():
            d.mkdir()
            self.created.append({"path": rel, "kind": "dir", "sha256": ""})

    def add_file(self, rel: str, data: bytes):
        parts = rel.split("/")
        for i in range(1, len(parts)):
            self._dir("/".join(parts[:i]))
        (self.dest / rel).write_bytes(data)
        self.created.append({"path": rel, "kind": "file", "sha256": sha256_bytes(data)})

    def add_shortcut(self, rel: str):
        self.created.append({"path": rel, "kind": "shortcut", "sha256": ""})

    def write_record(self) -> Path:
        rec = {"app": self.app["name"], "version": self.app["version"], "pkgkit": self.app.get("pkgkit", ""),
               "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "python": sys.version.split()[0],
               "created": self.created}
        p = self.dest / un.RECORD
        p.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        return p

    def rollback(self):
        try:
            self.write_record()
            un.uninstall(self.dest, False, out=lambda *_: None, tries=3)
        except Exception:  # noqa: BLE001  (rollback must never hide the original error)
            pass
        (self.dest / un.CMD_NAME).unlink(missing_ok=True)
        (self.dest / un.RECORD).unlink(missing_ok=True)
        if self.made_dest:
            shutil.rmtree(self.dest, ignore_errors=True)
        else:
            try:
                self.dest.rmdir()
            except OSError:
                pass


def check_environment(app: dict):
    if sys.platform != "win32":
        raise Failed("this package is for Windows")
    if sys.getwindowsversion().major < 10:
        raise Failed("Windows 10 or newer is required")
    need = tuple(int(x) for x in app["min_python"].split("."))
    if sys.version_info[:2] < need:
        raise Failed(f"{app['name']} needs Python {app['min_python']} or newer (found {sys.version.split()[0]})")


def run_check(app: dict, dest: Path):
    cmd = [str(dest / app["check"][0].replace("/", os.sep))] + list(app["check"][1:])
    try:
        r = subprocess.run(cmd, cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as ex:
        raise Failed(f"check command could not run ({ex})") from ex
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or app["version"] not in out:
        raise Failed(f"check command {' '.join(app['check'])} gave exit {r.returncode}: {out.strip()[:300]}")
    return out.strip()


def install(app: dict, dest: Path) -> Path:
    check_environment(app)
    problems = manifest.check(ROOT / "payload", HERE / "MANIFEST", skip=())
    if problems:
        raise Failed("payload does not match MANIFEST (damaged or changed zip): " + "; ".join(problems[:5]))
    inst = Installation(app, dest)
    inst.start()
    try:
        payload = ROOT / "payload"
        for p in sorted(payload.rglob("*")):
            if p.is_file():
                inst.add_file(p.relative_to(payload).as_posix(), p.read_bytes())
        inst.add_file(un.CMD_NAME, (ROOT / un.CMD_NAME).read_bytes())
        inst.add_file("_pkg/uninstall.py", (HERE / "uninstall.py").read_bytes())
        sc = app.get("shortcut")
        if sc:
            target = str(dest / sc["target"].replace("/", os.sep))
            args = sc.get("args", "")
            if sc.get("console", True):  # a command-line app: open a console that stays
                args = f'/k ""{target}" {args}"'.rstrip()
                target = os.environ.get("COMSPEC", "cmd.exe")
            lnk = un.appdata_root() / SHORTCUT_DIR / (sc["name"] + ".lnk")
            res = winlnk.create_shortcut(lnk, target, args, str(Path.home()), app.get("description", ""))
            inst.add_shortcut(Path(res["path"]).relative_to(un.appdata_root()).as_posix())
        inst.write_record()
        run_check(app, Path(dest))
    except BaseException:
        inst.rollback()
        raise
    return Path(dest)


def main(argv: list) -> int:
    app = json.loads((HERE / "app.json").read_text(encoding="utf-8"))
    dest = Path.home() / "apps" / app["name"]
    if "--dest" in argv:
        dest = Path(argv[argv.index("--dest") + 1])
    try:
        install(app, dest)
    except Failed as ex:
        print(f"FAILED: {ex}")
        return 1
    except Exception as ex:  # noqa: BLE001
        print(f"FAILED: unexpected error: {type(ex).__name__}: {ex}")
        return 1
    print(f"OK {app['name']} {app['version']} installed in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
