# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""
kiko_build.py — KIKO build tool (Maven-inspired lifecycle)

Usage:
  python kiko_build.py working    — run unit test suite
  python kiko_build.py release    — build kiko-w-<version>.zip  (Windows)
  python kiko_build.py linux      — build kiko-l-<version>.zip  (Linux)
  python kiko_build.py portable   — build kiko-p-<version>.zip  (portable, no global pip)
"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
RELEASES = ROOT / "releases"
VERSION = "0.2.0"
ZIP_NAME = f"kiko-w-{VERSION}.zip"
ZIP_NAME_LINUX = f"kiko-l-{VERSION}.zip"

# ── content strings ────────────────────────────────────────────────────────────

README_EN = """\
KIKO v{version}
Windows distribution
========================================

WHAT IS THIS
  KIKO is a minimal AI model gateway. It lets you call Google Gemini,
  NVIDIA NIM, and other AI providers through one OpenAI-compatible
  local API (http://localhost:4000). It logs every call (tokens + cost)
  to a local SQLite database — no cloud, no PostgreSQL, no Redis.

REQUIREMENTS
  - Python 3.12  (already on Windows 11 22H2+; check: python --version)
  - Internet access to reach AI provider APIs

INSTALL  (run once, no Admin rights needed)
  Double-click  install.bat
  -- or --
  Open cmd and run:
    pip install kiko-{version}-py3-none-any.whl

CONFIGURE
  1. Copy kiko-sample.yaml to a folder of your choice, e.g.:
       C:\\Users\\<you>\\.kircho\\kiko-config.yaml
  2. Edit the file — keep only the models you have API keys for.
     Keys are NEVER stored in the file.

SET API KEYS  (in cmd, before every start)
  set GEMINI_API_KEY=your_google_ai_studio_key
  set NVIDIA_API_KEY=your_nvidia_nim_key
  set MASTER_KEY=sk-choose-any-secret

  Tip: put these three lines in a start.bat next to your config file.

SEED DATABASE  (run once after install)
  kiko --config kiko-config.yaml --init-db

START SERVER
  kiko --config kiko-config.yaml --port 4000

  The server is ready when you see:
    KIKO running on http://127.0.0.1:4000 (Press CTRL+C to quit)

USEFUL COMMANDS
  kiko --list-models          show all models in database
  kiko --discover             ping each model, update status

TEST WITH CURL (optional)
  curl http://localhost:4000/health
  curl -H "Authorization: Bearer sk-choose-any-secret" ^
       -H "Content-Type: application/json" ^
       -d "{{\"model\":\"gemini-flash-lite\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}" ^
       http://localhost:4000/v1/chat/completions

STOP
  Ctrl+C in the terminal window

BILLING DATABASE
  Stored at: C:\\Users\\<you>\\.kircho\\kircho.db  (SQLite, open with DB Browser)
  Table: billing  (model, tokens_in, tokens_out, cost_usd, ts)
""".format(version=VERSION)

README_BG = """\
KIKO v{version}
Windows разпространение
========================================

КАКВО Е ТОВА
  KIKO е минимален AI gateway. Позволява ти да извикваш Google Gemini,
  NVIDIA NIM и други AI доставчици чрез един OpenAI-съвместим локален
  API (http://localhost:4000). Всяко извикване се записва (токени + цена)
  в локална SQLite база данни — без cloud, без PostgreSQL, без Redis.

ИЗИСКВАНИЯ
  - Python 3.12  (вече е на Windows 11 22H2+; провери: python --version)
  - Интернет достъп до API-тата на AI доставчиците

ИНСТАЛАЦИЯ  (веднъж, без Admin права)
  Кликни два пъти върху  install.bat
  -- или --
  Отвори cmd и изпълни:
    pip install kiko-{version}-py3-none-any.whl

КОНФИГУРАЦИЯ
  1. Копирай kiko-sample.yaml в папка по избор, например:
       C:\\Users\\<ти>\\.kircho\\kiko-config.yaml
  2. Редактирай файла — остави само моделите, за които имаш API ключове.
     Ключовете НИКОГА не се записват в файла.

ЗАДАЙ API КЛЮЧОВЕ  (в cmd, преди всяко стартиране)
  set GEMINI_API_KEY=твоят_google_ai_studio_ключ
  set NVIDIA_API_KEY=твоят_nvidia_nim_ключ
  set MASTER_KEY=sk-избери-произволна-тайна

  Съвет: сложи тези три реда в start.bat до конфиг файла.

ЗАПЪЛНИ БАЗАТА ДАННИ  (веднъж след инсталация)
  kiko --config kiko-config.yaml --init-db

СТАРТИРАЙ СЪРВЪРА
  kiko --config kiko-config.yaml --port 4000

  Сървърът е готов когато видиш:
    KIKO running on http://127.0.0.1:4000 (Press CTRL+C to quit)

ПОЛЕЗНИ КОМАНДИ
  kiko --list-models          показва всички модели в базата
  kiko --discover             пингва всеки модел, обновява статуса

СПРИ СЪРВЪРА
  Ctrl+C в терминала

БАЗА ДАННИ С РАЗХОДИ
  Местоположение: C:\\Users\\<ти>\\.kircho\\kircho.db  (SQLite)
  Таблица: billing  (model, tokens_in, tokens_out, cost_usd, ts)
""".format(version=VERSION)

INSTALL_BAT = """\
@echo off
echo KIKO v{version} - Installer
echo ================================
echo.

python --version 2>nul | findstr /C:"3.12" >nul
if errorlevel 1 (
    echo ERROR: Python 3.12 is required.
    echo Download from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo Installing KIKO...
pip install kiko-{version}-py3-none-any.whl --quiet
if errorlevel 1 (
    echo.
    echo ERROR: Installation failed. See output above.
    pause
    exit /b 1
)

echo.
echo KIKO installed successfully!
echo.
echo Next steps:
echo   1. Copy kiko-sample.yaml to kiko-config.yaml and edit (set your model list)
echo   2. Read README-EN.txt or commands.txt for setup instructions
echo   3. Run: kiko --config kiko-config.yaml --init-db
echo   4. Run: kiko --config kiko-config.yaml --port 4000
echo.
pause
""".format(version=VERSION)

START_BAT = """\
@echo off
rem KIKO start script
rem API keys are read automatically from Windows Credential Manager.
rem To store a key (run once in cmd):
rem   cmdkey /generic:KIKO/KIKO_IDENTITY/GEMINI_API_KEY /user:GEMINI_API_KEY /pass:YOUR_KEY
rem   cmdkey /generic:KIKO/KIKO_IDENTITY/NVIDIA_API_KEY /user:NVIDIA_API_KEY /pass:YOUR_KEY
rem   cmdkey /generic:KIKO/KIKO_IDENTITY/MASTER_KEY     /user:MASTER_KEY     /pass:sk-your-secret
rem
rem Edit the config path below if you placed your kiko-config.yaml elsewhere.
set KIKO_CONFIG=%USERPROFILE%\\.kircho\\kiko-config.yaml
kiko --config "%KIKO_CONFIG%" --port 4000
"""


README_EN_LINUX = """\
KIKO v{version}
Linux distribution
========================================

WHAT IS THIS
  KIKO is a minimal AI model gateway. It lets you call Google Gemini,
  NVIDIA NIM, and other AI providers through one OpenAI-compatible
  local API (http://localhost:4000). It logs every call (tokens + cost)
  to a local SQLite database — no cloud, no PostgreSQL, no Redis.

REQUIREMENTS
  - Python 3.12  (check: python3.12 --version)
  - pip  (check: pip --version)
  - systemd (user session)
  - Internet access to reach AI provider APIs

INSTALL  (run once, no root needed)
  bash install.sh

CONFIGURE
  1. Copy kiko-sample.yaml to a folder of your choice, e.g.:
       ~/.kircho/kiko-config.yaml
  2. Edit the file — keep only the models you have API keys for.
     Keys are NEVER stored in the file.

SET API KEYS  (in your shell, before every start)
  export GEMINI_API_KEY=your_google_ai_studio_key
  export NVIDIA_API_KEY=your_nvidia_nim_key
  export MASTER_KEY=sk-choose-any-secret

  Tip: put these three lines in a start.sh next to your config file.

SEED DATABASE  (run once after install)
  kiko --config kiko-config.yaml --init-db

START SERVER
  systemctl --user start kiko

  The server is ready when you see it in status:
    systemctl --user status kiko

USEFUL COMMANDS
  kiko --list-models          show all models in database
  kiko --discover             ping each model, update status

STOP
  systemctl --user stop kiko

BILLING DATABASE
  Stored at: ~/.kircho/kircho.db  (SQLite, open with DB Browser)
  Table: billing  (model, tokens_in, tokens_out, cost_usd, ts)
""".format(version=VERSION)

README_BG_LINUX = """\
KIKO v{version}
Linux разпространение
========================================

КАКВО Е ТОВА
  KIKO е минимален AI gateway. Позволява ти да извикваш Google Gemini,
  NVIDIA NIM и други AI доставчици чрез един OpenAI-съвместим локален
  API (http://localhost:4000). Всяко извикване се записва (токени + цена)
  в локална SQLite база данни — без cloud, без PostgreSQL, без Redis.

ИЗИСКВАНИЯ
  - Python 3.12  (провери: python3.12 --version)
  - pip  (провери: pip --version)
  - systemd (потребителска сесия)
  - Интернет достъп до API-тата на AI доставчиците

ИНСТАЛАЦИЯ  (веднъж, без root права)
  bash install.sh

КОНФИГУРАЦИЯ
  1. Копирай kiko-sample.yaml в папка по избор, например:
       ~/.kircho/kiko-config.yaml
  2. Редактирай файла — остави само моделите, за които имаш API ключове.
     Ключовете НИКОГА не се записват в файла.

ЗАДАЙ API КЛЮЧОВЕ  (в shell, преди всяко стартиране)
  export GEMINI_API_KEY=твоят_google_ai_studio_ключ
  export NVIDIA_API_KEY=твоят_nvidia_nim_ключ
  export MASTER_KEY=sk-избери-произволна-тайна

  Съвет: сложи тези три реда в start.sh до конфиг файла.

ЗАПЪЛНИ БАЗАТА ДАННИ  (веднъж след инсталация)
  kiko --config kiko-config.yaml --init-db

СТАРТИРАЙ СЪРВЪРА
  systemctl --user start kiko

  Сървърът е готов когато статусът показва "active (running)":
    systemctl --user status kiko

ПОЛЕЗНИ КОМАНДИ
  kiko --list-models          показва всички модели в базата
  kiko --discover             пингва всеки модел, обновява статуса

СПРИ СЪРВЪРА
  systemctl --user stop kiko

БАЗА ДАННИ С РАЗХОДИ
  Местоположение: ~/.kircho/kircho.db  (SQLite)
  Таблица: billing  (model, tokens_in, tokens_out, cost_usd, ts)
""".format(version=VERSION)


# ── helpers ────────────────────────────────────────────────────────────────────

def _get_version():
    import re
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0.0.0"


# ── modes ──────────────────────────────────────────────────────────────────────

def mode_working():
    """Run the unit test suite."""
    result = subprocess.run([
        "uv", "run", "--no-project",
        "--with", "fastapi",
        "--with", "uvicorn",
        "--with", "pyyaml",
        "--with", "litellm==1.104.2",
        "--with", "pytest",
        "--with", "pytest-asyncio",
        "python", "-m", "pytest",
        "tests/kiko/test_kiko_sqlite_logger.py",
        "tests/kiko/test_kiko_config_loader.py",
        "tests/kiko/test_kiko_db.py",
        "-v",
    ], cwd=ROOT)
    sys.exit(result.returncode)


def mode_release():
    """Build the distribution ZIP."""
    DIST.mkdir(exist_ok=True)

    # Remove old build artefacts
    for f in DIST.glob("*.whl"):
        f.unlink()
    old_zip = DIST / ZIP_NAME
    if old_zip.exists():
        old_zip.unlink()

    # Build wheel (uv build — no pip install needed on host)
    print(f"[1/4] Building wheel...")
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

    # Write text files to dist/
    print("[2/4] Writing README and installer...")
    (DIST / "README-EN.txt").write_text(README_EN, encoding="utf-8")
    (DIST / "README-BG.txt").write_text(README_BG, encoding="utf-8")
    (DIST / "install.bat").write_text(INSTALL_BAT, encoding="utf-8")
    (DIST / "start.bat").write_text(START_BAT, encoding="utf-8")

    # Build ZIP
    print(f"[3/4] Packing {ZIP_NAME}...")
    with zipfile.ZipFile(DIST / ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(wheel, wheel.name)
        zf.write(ROOT / "kiko-sample.yaml", "kiko-sample.yaml")
        zf.write(ROOT / "commands.txt", "commands.txt")
        zf.write(ROOT / "LICENSE", "LICENSE")
        zf.write(ROOT / "CHANGELOG.md", "CHANGELOG.md")
        zf.write(DIST / "README-EN.txt", "README-EN.txt")
        zf.write(DIST / "README-BG.txt", "README-BG.txt")
        zf.write(DIST / "install.bat", "install.bat")
        zf.write(DIST / "start.bat", "start.bat")

    size_kb = (DIST / ZIP_NAME).stat().st_size // 1024

    # Copy to releases/
    import shutil
    RELEASES.mkdir(exist_ok=True)
    shutil.copy2(DIST / ZIP_NAME, RELEASES / ZIP_NAME)

    print(f"[4/4] Done: releases\\{ZIP_NAME}  ({size_kb} KB)")
    print()
    print(f"Contents:")
    with zipfile.ZipFile(DIST / ZIP_NAME) as zf:
        for info in zf.infolist():
            print(f"  {info.filename:45s}  {info.file_size // 1024:>6} KB")


def mode_release_linux():
    """Build the Linux distribution ZIP."""
    DIST.mkdir(exist_ok=True)

    # Remove old Linux build artefacts
    old_zip = DIST / ZIP_NAME_LINUX
    if old_zip.exists():
        old_zip.unlink()

    # Build wheel (uv build — no pip install needed on host)
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

    # Write Linux README files to dist/
    print("[2/4] Writing README files...")
    (DIST / "README-EN.txt").write_text(README_EN_LINUX, encoding="utf-8")
    (DIST / "README-BG.txt").write_text(README_BG_LINUX, encoding="utf-8")

    # Build ZIP — install.sh stored with executable bit
    print(f"[3/4] Packing {ZIP_NAME_LINUX}...")
    with zipfile.ZipFile(DIST / ZIP_NAME_LINUX, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(wheel, wheel.name)
        zf.write(ROOT / "kiko-sample.yaml", "kiko-sample.yaml")
        zf.write(ROOT / "kiko.service", "kiko.service")
        # install.sh needs executable bit preserved in zip metadata
        info = zipfile.ZipInfo("install.sh")
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o755 << 16
        zf.writestr(info, (ROOT / "install.sh").read_bytes())
        zf.write(ROOT / "LICENSE", "LICENSE")
        zf.write(ROOT / "CHANGELOG.md", "CHANGELOG.md")
        zf.write(DIST / "README-EN.txt", "README-EN.txt")
        zf.write(DIST / "README-BG.txt", "README-BG.txt")

    size_kb = (DIST / ZIP_NAME_LINUX).stat().st_size // 1024

    # Copy to releases/
    import shutil
    RELEASES.mkdir(exist_ok=True)
    shutil.copy2(DIST / ZIP_NAME_LINUX, RELEASES / ZIP_NAME_LINUX)

    print(f"[4/4] Done: releases/{ZIP_NAME_LINUX}  ({size_kb} KB)")
    print()
    print("Contents:")
    with zipfile.ZipFile(DIST / ZIP_NAME_LINUX) as zf:
        for info in zf.infolist():
            print(f"  {info.filename:45s}  {info.file_size // 1024:>6} KB")


_EMBED_PY_URL = (
    "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip"
)


def mode_portable():
    """Build the portable distribution ZIP (embeds Python + unpacks all wheels)."""
    import urllib.request
    import shutil

    # 1. Build kiko wheel
    subprocess.run(["uv", "build", "--wheel"], check=True)
    wheel = next(Path("dist").glob("kiko-*.whl"), None)
    if not wheel:
        sys.exit("ERROR: no wheel in dist/")

    ver = _get_version()

    # 2. Download all runtime dep wheels
    tmp_wheels = Path("build") / "portable_wheels"
    shutil.rmtree(tmp_wheels, ignore_errors=True)
    tmp_wheels.mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "download",
         "--dest", str(tmp_wheels),
         "--only-binary", ":all:",
         "--platform", "win_amd64",
         "--python-version", "312",
         str(wheel)],
        check=True,
    )

    # 3. Download embeddable Python (cached in build/)
    embed_cache = Path("build") / "python-3.12.7-embed-amd64.zip"
    if not embed_cache.exists():
        print("Downloading embeddable Python 3.12.7 ...")
        urllib.request.urlretrieve(_EMBED_PY_URL, embed_cache)
    else:
        print("Using cached embeddable Python.")

    # 4. Assemble the portable zip
    zip_path = Path("releases") / f"kiko-p-{ver}.zip"
    zip_path.parent.mkdir(exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:

        # 4a. Embedded Python — fix .pth so site is enabled (needed for sys.path)
        with zipfile.ZipFile(embed_cache) as ep:
            for name in ep.namelist():
                data = ep.read(name)
                if name.endswith("._pth"):
                    # Enable site so sys.path extensions work
                    data = data.replace(b"#import site", b"import site")
                zf.writestr(f"kiko/python/{name}", data)

        # 4b. Unpack every wheel into kiko/lib/
        for whl in tmp_wheels.glob("*.whl"):
            with zipfile.ZipFile(whl) as wz:
                for member in wz.namelist():
                    # Skip dist-info metadata (not needed at runtime)
                    if ".dist-info/" in member or member.endswith(".dist-info"):
                        continue
                    zf.writestr(f"kiko/lib/{member}", wz.read(member))

        # 4c. Launcher + config
        zf.write("dist/kiko_start.py",            "kiko/kiko_start.py")
        zf.write("dist/start-portable.bat",        "kiko/start.bat")
        zf.write("dist/kiko-portable-config.yaml", "kiko/kiko-config.yaml")

        # 4d. Empty data/ dir
        zf.writestr("kiko/data/.gitkeep", "")

    size_mb = zip_path.stat().st_size / 1_048_576
    print(f"Portable zip: {zip_path}  ({size_mb:.1f} MB)")
    print("Done. Distribute the zip; the user unzips and runs start.bat.")


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "working":
        mode_working()
    elif mode == "release":
        mode_release()
    elif mode == "linux":
        mode_release_linux()
    elif mode == "portable":
        mode_portable()
    else:
        print(__doc__)
        sys.exit(1)
