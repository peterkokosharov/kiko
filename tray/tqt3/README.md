# kiko TDE Panel Applet

A TDE (Trinity Desktop Environment) kicker panel applet (TQt3) that controls the kiko-lite proxy server.
The Bulgarian original (Latin letters) is kept in `README.bg-latin.md`.

## Contents

| File | Description |
|------|-------------|
| `src/kiko_applet.cpp` | Main applet code (KPanelApplet) |
| `src/kiko_applet.h` | Applet header |
| `src/kiko_process.cpp` | Service control through `systemctl --user` (kiko-proxy service) |
| `src/kiko_process.h` | Process header |
| `src/kiko_log_panel.cpp` | Log panel (billing/usage table) |
| `src/kiko_log_panel.h` | Log panel header |
| `kiko-applet.desktop` | Desktop entry for kicker |
| `icons/Kiko_*.png` | Icons for the different states |
| `CMakeLists.txt` | Build file |
| `compat_headers/` | TQt3 compatibility headers |

## The 11 fixes

1. `m_logPanel` initialised with `nullptr` (it was a garbage pointer, SIGSEGV on "Show Logs").
2. `m_logBuffer` added as a `TQStringList` (it was not declared).
3. Duplicate `#include "moc_kiko_log_panel.cpp"` removed (CMake generates the MOC source).
4. `TQTableItem` constructor: added the `TQTableItem::Never` EditType.
5. `setHorizontalHeaderLabels`, `setAlignment`, `setTextColor` do not exist in TQt3; replaced or removed.
6. `TQCheckBox`: added `#include <tqcheckbox.h>`.
7. `checkServiceStatus()` uses `m_serviceName` instead of the hard-coded `"kiko"`.
8. `.latin1()` on a temporary `TQString` fixed (dangling pointer).
9. `TDEGlobal::iconLoader()` / `TDEGlobal::dirs()`: null checks before use (SIGSEGV).
10. `m_iconStopped.pixmap()`: null check before `setPixmap`.
11. `setServiceName("kiko-proxy")` matches `kiko-proxy.service`.

## Build

```bash
cd tqt3
rm -rf build
cmake -S . -B build -DCMAKE_CXX_FLAGS="-I/tmp/kiko-deps/usr/include" -DSQLITE3_LIB=/tmp/kiko-deps/usr/lib/x86_64-linux-gnu/libsqlite3.so
make -C build -j$(nproc)
```

(The original README used the owner's local paths; the two `/tmp/kiko-deps` values are only needed when the
SQLite headers are not installed system-wide.)

Requires:
- `libtqt3-mt-dev` (TQt3 headers and libraries)
- `tdelibs14-trinity-dev` (TDE headers)
- `tdebase-trinity-dev` (kpanelapplet.h)
- `libsqlite3-dev` (SQLite headers; otherwise local paths such as `/tmp/kiko-deps` can be used)
- `tqmoc` (tqt3-dev-tools)
- `pkg-config`, `glib-2.0`

## Install (needs sudo)

The original README refers to an install script, `install.applet.sh`, kept outside this repository.
It copies:
- `kiko_applet.so` + `kiko_applet.la` to `/opt/trinity/lib/trinity/`
- `kiko-applet.desktop` to `/opt/trinity/share/apps/kicker/applets/`
- `Kiko_*.png` to `/opt/trinity/share/icons/hicolor/22x22/apps/`

## Run

1. Restart the kicker panel.
2. Right-click the kicker, "Add applet", choose "kiko Proxy".
3. The applet manages `kiko-proxy.service` (systemd user unit):
   ```bash
   systemctl --user status kiko-proxy.service
   systemctl --user restart kiko-proxy.service
   ```

## Port

The kiko-lite proxy listens by default on `http://127.0.0.1:4000`. Health endpoint: `GET /health`.

## Author

Peter Kokosharov <peter.kokosharov@gmail.com>

License: MIT, Peter Kokosharov <peter.kokosharov@gmail.com>
