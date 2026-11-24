# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Portable installer.

    python -I tools/win/install.py kikolib [--dest DIR]

Uninstall: python -I <DIR>/uninstall.py [--purge]. Targets are functions registered in TARGETS;
add kiko / tray / sisi the same way (a function (dest: Path) -> Installation).
"""
import argparse
import io
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import portable  # noqa: E402


def git(*args: str) -> bytes:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def archive_head(*paths: str) -> zipfile.ZipFile:
    """Files from git HEAD (not the working tree), as a zip."""
    return zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", "HEAD", *paths)))


def install_kikolib(dest: Path) -> "portable.Installation":
    commit = git("rev-parse", "HEAD").decode().strip()
    z = archive_head("gateway/kikolib", "gateway/tools/kikolib_manifest.py")
    prefix = "gateway/kikolib/"
    init = z.read(prefix + "__init__.py").decode("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    inst = portable.Installation("kikolib", m.group(1) if m else "unknown", dest, "kiko-src", commit)
    try:
        inst.ensure_dirs("kikolib")
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            inst.add_file("kikolib/" + info.filename[len(prefix):], z.read(info))
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "kikolib_manifest.py"
            script.write_bytes(z.read("gateway/tools/kikolib_manifest.py"))
            r = subprocess.run([sys.executable, "-I", str(script), "check", str(dest / "kikolib")],
                               capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode:
            raise SystemExit("manifest check FAILED on the installed copy; rolled back")
    except BaseException:
        inst.rollback()
        raise
    return inst


TARGETS = {"kikolib": install_kikolib}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="install.py")
    ap.add_argument("target", choices=sorted(TARGETS))
    ap.add_argument("--dest", help="app folder (default ~/apps/<target>)")
    a = ap.parse_args(argv)
    dest = Path(a.dest) if a.dest else portable.default_dest(a.target)
    dest = dest.resolve()
    if (dest / portable.RECORD).exists():
        print(f"existing install in {dest}: removing it first (data kept)")
        if portable.uninstall(dest) != 0:
            print("could not remove the old install cleanly; aborting")
            return 1
    elif dest.exists() and any(c.name not in ("data", "logs") for c in dest.iterdir()):
        print(f"{dest} exists, is not empty and has no install record; aborting")
        return 1
    inst = TARGETS[a.target](dest)
    rec = inst.finish()
    problems = portable.verify(dest)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"installed {inst.app} {inst.version} -> {dest}\nrecord: {rec}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
