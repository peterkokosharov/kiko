# Author: claude-sonnet-4-6
"""
kiko_build_tray.py — KIKO Tray build tool.

Usage:
  python kiko_build_tray.py working   — run unit tests
  python kiko_build_tray.py release   — build wheel + zip
"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
RELEASES = ROOT / "releases"
VERSION = "0.1.0"
ZIP_NAME = f"kiko-tray-w-{VERSION}.zip"

README = """\
kiko-tray v{version}
Windows distribution
========================================

WHAT IS THIS
  kiko-tray puts a KIKO icon in the Windows system tray.
  Right-click it to start / stop the KIKO proxy server.

REQUIREMENTS
  - Python 3.12+
  - KIKO v0.2.0+ already installed  (install kiko first from kiko-w-*.zip)

INSTALL  (run once)
  pip install kiko_tray-{version}-py3-none-any.whl

FIRST RUN
  kiko-tray

  The tray icon appears immediately. Right-click → Start KIKO.

CONFIG
  The tray app reads: %USERPROFILE%\\.kircho\\kiko-config.yaml
  Set a different path with the KIKO_CONFIG environment variable.

API KEYS
  Same as KIKO — store in Windows Credential Manager:
    cmdkey /generic:RONI/KIRCHO/GEMINI_API_KEY /user:GEMINI_API_KEY /pass:YOUR_KEY
    cmdkey /generic:RONI/KIRCHO/MASTER_KEY     /user:MASTER_KEY     /pass:sk-your-secret
""".format(version=VERSION)


def mode_working() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
    )
    sys.exit(result.returncode)


def mode_release() -> None:
    DIST.mkdir(exist_ok=True)
    RELEASES.mkdir(exist_ok=True)

    for f in DIST.glob("*.whl"):
        f.unlink()

    print("[1/4] Building wheel...")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(DIST)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("ERROR: wheel build failed")
        sys.exit(1)

    wheels = sorted(DIST.glob("*.whl"))
    if not wheels:
        print("ERROR: no .whl found in dist/")
        sys.exit(1)
    wheel = wheels[-1]
    print(f"       {wheel.name}")

    print("[2/4] Writing README...")
    (DIST / "README.txt").write_text(README, encoding="utf-8")

    print(f"[3/4] Packing {ZIP_NAME}...")
    with zipfile.ZipFile(DIST / ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(wheel, wheel.name)
        zf.write(DIST / "README.txt", "README.txt")

    import shutil
    shutil.copy2(DIST / ZIP_NAME, RELEASES / ZIP_NAME)

    size_kb = (RELEASES / ZIP_NAME).stat().st_size // 1024
    print(f"[4/4] Done: releases\\{ZIP_NAME}  ({size_kb} KB)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "working":
        mode_working()
    elif mode == "release":
        mode_release()
    else:
        print(__doc__)
        sys.exit(1)
