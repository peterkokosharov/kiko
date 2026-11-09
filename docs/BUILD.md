# Building kiko-tray (native)

One top-level `native/CMakeLists.txt` (CMake >= 3.16). The platform layer is picked automatically.
**Nothing here has been compiled by its author** (no compiler on the authoring machine): expect to fix
small compile errors on the first build and report them.

Common options: `-DKT_BUILD_GUI=OFF` (core + tests only, no toolkit needed), `-DKT_BUILD_TESTS=OFF`,
`-DKT_FETCH_CJSON=OFF`, `-DKT_GUI=gtk|qt` (Linux).

## cJSON (all platforms)

JSON is parsed by cJSON 1.7.18 (MIT). Either put `cJSON.c`, `cJSON.h`, `LICENSE` into
`native/third_party/cjson/` (see its README.md for the exact URLs) or leave the folder as it is and let CMake
download tag `v1.7.18` at configure time (default `KT_FETCH_CJSON=ON`; needs git and network).

## Windows 10/11 (MSVC)

Install (once): Visual Studio Build Tools 2022 with the **Desktop development with C++** workload (it
includes the Windows 10/11 SDK and CMake), and Git (for the cJSON download). No Python is needed.

From "x64 Native Tools Command Prompt for VS 2022", in the repo folder `kiko-tray`:

```
cmake -S native -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
build\Release\kiko-tray.exe
```

MinGW-w64 also works: `cmake -S native -B build -G "MinGW Makefiles"`, `cmake --build build`,
`ctest --test-dir build` (windres embeds the manifest from `kiko-tray.rc`).

MinGW note: some MinGW toolchains embed their own default application manifest (RT_MANIFEST, resource id 1),
which can clash with the one in `kiko-tray.rc` ("duplicate resource" at link time) or leave the per-monitor
DPI / Common Controls 6 settings missing. Workaround: delete the line `1 24 "kiko-tray.manifest"` from
`native/win32/kiko-tray.rc`, rebuild, and ship a copy of `native/win32/kiko-tray.manifest` named
`kiko-tray.exe.manifest` next to the exe (Windows reads an external manifest only if none is embedded).
MSVC builds are not affected (CMake adds `/MANIFEST:NO`; the .rc manifest is the only one).

## Linux Mint 22.3 Cinnamon (GTK3 + XApp)

```
sudo apt install build-essential cmake pkg-config libgtk-3-dev libxapp-dev git
cmake -S native -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
./build/kiko-tray
```
`libxapp-dev` is optional: without it the build falls back to GtkStatusIcon. Runtime helpers: `seahorse`
(keyring button) and `notify-send` (package `libnotify-bin`, balloons). kiko is controlled with
`systemctl --user ... kiko-lite`; install the unit from the kiko repo (`tools/install-service.sh`).

## KDE Plasma / Kubuntu / Fedora KDE (Qt) - unverified

```
# Debian/Ubuntu/Kubuntu: sudo apt install build-essential cmake pkg-config git qt6-base-dev   (or qtbase5-dev)
# Fedora KDE:            sudo dnf install gcc gcc-c++ cmake git qt6-qtbase-devel              (or qt5-qtbase-devel)
cmake -S native -B build -DKT_GUI=qt
cmake --build build -j && ctest --test-dir build --output-on-failure
./build/kiko-tray
```
CMake looks for Qt6 first, then Qt5 (>= 5.15), Widgets module. The Qt layer also works on Mint
(`-DKT_GUI=qt`). The wallet button needs `kwalletmanager`.

## macOS - unverified

```
xcode-select --install          # Command Line Tools
brew install cmake              # Homebrew
cmake -S native -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j && ctest --test-dir build --output-on-failure
open build/kiko-tray.app
```
The bundle uses `LSUIElement` (menu bar only, no Dock icon). kiko is controlled via `launchctl`; install
the sample `docs/macos/com.kiko.lite.plist` to `~/Library/LaunchAgents/` after fixing the path inside.

Environment note: an app started with `open` (Finder, launchd) does **not** see variables exported in your
shell, so `KIKO_PORT` / `KIKO_API_TOKEN` are ignored there. Put the token into `~/.kiko/api-token` (first
line, mode 0600); the default port 4000 is used unless you start the binary from a shell
(`build/kiko-tray.app/Contents/MacOS/kiko-tray`). The sample LaunchAgent plist needs the absolute path to
kiko-lite and absolute log paths (launchd does not expand `~` or `$HOME`): replace the two
`REPLACE_WITH_HOME` placeholders with your home folder, for example `/Users/you`.

## Python tray manifests (not touched)

`pyproject.toml` and `installer.cfg` still list pystray and Pillow for the old Python tray. They need
owner approval to change: drop `pystray` and `Pillow` from `dependencies` (pyproject) and from
`[Include] packages` (installer.cfg) when the native tray replaces the Python one.
