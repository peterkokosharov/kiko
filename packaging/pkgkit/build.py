# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Build <name>-<ver>-win-x64.zip from a project's app.toml (Python 3.11+, standard library only).

    python -I build.py --root REPO --app REPO/packaging/app.toml [--out REPO/dist] [--extra SRC=DEST ...]

--extra adds one file to the payload (DEST relative to the payload folder; e.g. a prebuilt DLL).
Zip layout:  <name>-<ver>-win-x64/{install.cmd, uninstall.cmd, README.txt, payload/, _pkg/}.
Tests are never packaged (tests/, test_*, conftest*, __pycache__, *.pyc, .git*); after writing, the zip is listed
and the build FAILS if a test file is inside. .cmd/.bat/.txt files are written with CRLF line endings.
Refuses to run if this pkgkit copy does not match its own SHA256SUMS.
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import manifest  # noqa: E402

KIT_FILES = ("install.py", "uninstall.py", "winlnk.py", "manifest.py")
CRLF_EXT = (".cmd", ".bat", ".txt")
BAD_DIRS = {"tests", "__pycache__", ".git"}


def kit_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (HERE / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "unknown"


def is_test_path(rel: str) -> bool:
    """True for anything that must not be in a package."""
    parts = rel.replace("\\", "/").split("/")
    if any(p in BAD_DIRS or p.startswith(".git") for p in parts):
        return True
    name = parts[-1]
    return name.startswith("test_") or name.startswith("conftest") or name.endswith((".pyc", ".pyo"))


def crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def write(dst: Path, data: bytes):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(crlf(data) if dst.suffix.lower() in CRLF_EXT else data)


def fill(text: str, subs: dict) -> str:
    for k, v in subs.items():
        text = text.replace(f"@{k}@", v)
    return text


def copy_tree(src: Path, dst: Path):
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if p.is_file() and not is_test_path(rel):
            write(dst / rel, p.read_bytes())


def build(root: Path, app_toml: Path, out: Path, extras: list) -> Path:
    problems = manifest.check(HERE)
    if problems:
        raise SystemExit("pkgkit does not match its SHA256SUMS (edited copy?): " + "; ".join(problems))
    cfg = tomllib.loads(app_toml.read_text(encoding="utf-8"))
    a = cfg["app"]
    name, minpy = a["name"], a["min_python"]
    ver = re.search(r'__version__\s*=\s*"([^"]+)"', (root / a["version_file"]).read_text(encoding="utf-8"))
    if not ver:
        raise SystemExit(f"no __version__ in {a['version_file']}")
    version = ver.group(1)
    top = f"{name}-{version}-win-x64"
    subs = {"NAME": name, "VERSION": version, "MINPY": minpy, "PKGKIT": kit_version(),
            "MINPY_TUPLE": ", ".join(minpy.split("."))}
    pdir = cfg["payload"]["dir"]
    with tempfile.TemporaryDirectory(prefix="pkgkit_") as td:
        stage = Path(td) / top
        payload = stage / "payload" / pdir
        for inc in cfg["payload"]["include"]:
            s = root / inc
            if s.is_dir():
                copy_tree(s, payload / inc)
            elif s.is_file():
                write(payload / inc, s.read_bytes())
            else:
                raise SystemExit(f"payload.include not found: {inc}")
        for f in cfg["payload"].get("file", []):
            write(payload / f["dest"], (root / f["src"]).read_bytes())
        for ex in extras:
            src, _, dest = ex.partition("=")
            write(payload / dest, Path(src).read_bytes())
        for n in ("install.cmd", "uninstall.cmd"):
            write(stage / n, fill((HERE / (n + ".in")).read_text(encoding="utf-8"), subs).encode("utf-8"))
        readme = fill(
            f"@NAME@ @VERSION@ for Windows 10/11 (x64), installed per user, no admin rights needed.\n\n"
            f"Install:    double-click install.cmd (or run: install.cmd /quiet)\n"
            f"            needs Python @MINPY@ or newer (python.org, tick \"Add to PATH\").\n"
            f"            Installs to %USERPROFILE%\\apps\\@NAME@ and adds a Start-menu shortcut (Programs\\apps).\n"
            f"Uninstall:  run uninstall.cmd (here, or in the installed folder). It removes exactly what the\n"
            f"            installer recorded; changed files are kept and reported. data\\ and logs\\ are removed\n"
            f"            only with: uninstall.cmd /purge\n"
            f"Not installing? The program is in payload\\{pdir}\\ and runs from there too.\n\n"
            f"Built with pkgkit @PKGKIT@.\n", subs)
        extra = cfg.get("readme", {}).get("extra")
        if extra:
            readme += "\n" + (root / extra).read_text(encoding="utf-8")
        write(stage / "README.txt", readme.encode("utf-8"))
        kit = stage / "_pkg"
        for n in KIT_FILES:
            write(kit / n, (HERE / n).read_bytes())
        sc = cfg.get("shortcut")
        app_json = {"name": name, "version": version, "min_python": minpy, "pkgkit": kit_version(),
                    "description": a.get("description", ""), "check": cfg["check"]["cmd"]}
        if sc:
            app_json["shortcut"] = sc
        write(kit / "app.json", (json.dumps(app_json, indent=2) + "\n").encode("utf-8"))
        (kit / "MANIFEST").write_bytes(manifest.render(manifest.compute(stage / "payload", skip=())))
        out.mkdir(parents=True, exist_ok=True)
        zpath = out / f"{top}.zip"
        zpath.unlink(missing_ok=True)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    z.write(p, f"{top}/{p.relative_to(stage).as_posix()}")
    return zpath


def list_zip(zpath: Path) -> int:
    """Print the zip contents; return the number of test files found inside (must be 0)."""
    bad = 0
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        for i in infos:
            rel = i.filename.split("/", 1)[1]
            flag = "  <-- TEST FILE" if is_test_path(rel) else ""
            bad += bool(flag)
            print(f"  {i.file_size:9d}  {i.filename}{flag}")
    print(f"{len(infos)} files, {zpath.stat().st_size} bytes: {zpath}")
    return bad


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--app", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--extra", action="append", default=[], metavar="SRC=DEST")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    zpath = build(root, a.app.resolve(), (a.out or root / "dist").resolve(), a.extra)
    if list_zip(zpath):
        zpath.unlink()
        print("BUILD FAILED: test files are inside the zip (zip deleted)")
        return 1
    print("BUILD OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
