# kiko-tray build matrix

One repository, five thin platform clients. Source only is committed; binaries, build folders,
local databases and keys are not.

| Laptop | OS / desktop | Port | Build command | Tests | Status | Commit |
|---|---|---|---|---|---|---|
| mint-01 | Linux Mint 22.3 Cinnamon | GTK3 (`tray/gtk3`) | `bash tray/tools/build-mint.sh` | 6/6 C + 8/8 API + 287 gateway | **works** | `20a0661`, `f08fdb4` |
| plasma-01 | KDE Plasma | Qt6/Qt5 (`tray/qt6`) | `bash tray/tools/build-plasma.sh` | — | unverified | — |
| q4os-01 | Q4OS 6 / TDE | TQt3 applet (`tray/tqt3`) | `bash tray/tools/build-q4os.sh` | — | unverified | — |
| win10-01 | Windows 10 | Win32 (`tray/win`) | `powershell -File tray\tools\build-win10.ps1`, or CI `.github/workflows/win-tray.yml` (see BUILD.md) | — | build via CI, still unverified on a real desktop | — |
| mac-01 | macOS | Cocoa (`tray/mac`) | `bash tray/tools/build-macos.sh` | — | unverified | — |

The Mint row is the only one built so far: tray icon (red / orange / green), menu and every dialog work
against the management API in `gateway/kiko_lite/management.py`.

## Rules for every laptop

1. `git pull --rebase` before building.
2. Run the script for your platform, then `ctest` (the script does it).
3. Commit only the fix and the test result. Update **your** row above with the commit hash and
   the real test count. Do not commit `build/`, `kiko.db`, `.env`, key material or icons generated at runtime.
4. Keys stay in the local keyring (`/peterk/<KEY>`, user `peterk` on Mint). Never in the repo.
5. If the port does not compile, commit the build log as `docs/build-logs/<laptop>-<date>.txt`,
   mark the row `blocked`, and let the next laptop fix it.
