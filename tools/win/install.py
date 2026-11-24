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
import os
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


def rex_show(rex: Path, path: str) -> bytes:
    """A file of the rex repo at git HEAD (the rex working tree is never read)."""
    r = subprocess.run(["git", "-C", str(rex), "show", f"HEAD:{path}"], capture_output=True)
    if r.returncode:
        raise SystemExit(f"{path} not in git HEAD of {rex}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def kiko_launchers(dest: Path) -> dict:
    """Generated launchers; they name the installing Python by absolute path (no PATH lookup)."""
    py, chain = sys.executable, dest / "kiko_chain.py"
    return {
        "kiko-chain.cmd": f'@echo off\r\n"{py}" -I "{chain}" %*\r\n',
        "kiko-chain-stop.cmd": f'@echo off\r\n"{py}" -I "{chain}" --stop\r\n',
    }


def install_kiko(dest: Path, rex_src: Path | None = None, kikolib: Path | None = None) -> "portable.Installation":
    kikolib = Path(kikolib or dest.parent / "kikolib")
    if not (kikolib / portable.RECORD).is_file():
        raise SystemExit(f"kikolib is not installed ({kikolib}); run: install.py kikolib")
    rex = Path(rex_src or Path.home() / "rex")
    gateway = rex_show(rex, "scripts/claude_cli_gateway.py")
    procutil = rex_show(rex, "runtimelib/procutil.py")
    commit = git("rev-parse", "HEAD").decode().strip()
    sys.path.insert(0, str(REPO / "gateway" / "tools"))
    import kiko_lite_bundle  # noqa: E402
    with tempfile.TemporaryDirectory() as td:
        try:
            zpath = kiko_lite_bundle.build(Path(td))
        except kiko_lite_bundle.BundleError as e:
            raise SystemExit(f"kiko-lite bundle failed: {e}")
        z = zipfile.ZipFile(zpath)
        names = [i for i in z.infolist() if not i.is_dir()]
        version = z.read("VERSION").decode().split()[1] if "VERSION" in z.namelist() else "unknown"
        inst = portable.Installation("kiko", f"kiko-lite {version}", dest, "kiko-src", commit)
        try:
            for info in names:
                n = info.filename
                # kikolib comes from apps/kikolib; the bundle's own launchers are replaced by ours
                if n.startswith("kikolib/") or n in ("kiko-lite", "kiko-lite.cmd"):
                    continue
                inst.add_file("kiko-lite/" + n, z.read(info))
        except BaseException:
            inst.rollback()
            raise
        finally:
            z.close()
    try:
        inst.add_file("gateway/scripts/claude_cli_gateway.py", gateway)
        inst.add_file("gateway/runtimelib/procutil.py", procutil)
        inst.add_file("kiko_chain.py", (HERE / "kiko_chain.py").read_bytes())
        for name, text in kiko_launchers(dest).items():
            inst.add_file(name, text.encode("utf-8"))
    except BaseException:
        inst.rollback()
        raise
    return inst


def post_kiko(dest: Path) -> None:
    """Runtime data (not recorded): tokens and the database. Kept on reinstall, removed by --purge."""
    env = dict(os.environ, KIKO_KIKOLIB_DIR=os.environ.get("KIKO_KIKOLIB_DIR", str(dest.parent / "kikolib")))
    r = subprocess.run([sys.executable, "-I", str(dest / "kiko_chain.py"), "--init"], env=env,
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    if r.returncode:
        raise SystemExit("data init failed")


TARGETS = {"kikolib": install_kikolib, "kiko": install_kiko}
POST = {"kiko": post_kiko}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="install.py")
    ap.add_argument("target", choices=sorted(TARGETS))
    ap.add_argument("--dest", help="app folder (default ~/apps/<target>)")
    ap.add_argument("--rex-src", help="rex repo for the gateway files (kiko target; default ~/rex)")
    ap.add_argument("--kikolib", help="installed kikolib folder (kiko target; default <dest>/../kikolib)")
    a = ap.parse_args(argv)
    dest = Path(a.dest) if a.dest else portable.default_dest(a.target)
    dest = dest.resolve()
    if (dest / portable.RECORD).exists():
        # check first: a modified file must abort before anything is removed
        try:
            old = portable.verify(dest)
        except (OSError, ValueError, KeyError) as ex:
            print(f"cannot read the old install record ({ex}); aborting, nothing touched")
            return 1
        changed = [p for p in old if p.startswith(("changed:", "unsafe path:", "no sha256:"))]
        if changed:
            print("old install has modified or unsafe entries; aborting, nothing touched:\n" + "\n".join(changed))
            return 1
        print(f"existing install in {dest}: removing it first (data kept)")
        if portable.uninstall(dest) != 0:
            print("could not remove the old install cleanly; aborting")
            return 1
    elif dest.exists() and any(c.name not in ("data", "logs") for c in dest.iterdir()):
        print(f"{dest} exists, is not empty and has no install record; aborting")
        return 1
    if a.target == "kiko":
        inst = install_kiko(dest, Path(a.rex_src) if a.rex_src else None, Path(a.kikolib) if a.kikolib else None)
    else:
        inst = TARGETS[a.target](dest)
    try:
        rec = inst.finish()
        problems = portable.verify(dest)
    except BaseException:
        inst.rollback()
        raise
    if problems:
        print("\n".join(problems))
        inst.rollback()
        return 1
    if a.target in POST:
        if a.kikolib:
            os.environ["KIKO_KIKOLIB_DIR"] = str(Path(a.kikolib).resolve())
        POST[a.target](dest)
    print(f"installed {inst.app} {inst.version} -> {dest}\nrecord: {rec}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
