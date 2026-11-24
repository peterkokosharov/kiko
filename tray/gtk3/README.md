<!-- SPDX-License-Identifier: MIT -->
<!-- Author: Peter Kokosharov <peter.kokosharov@gmail.com> -->
<!-- AI models: claude-sonnet-5-5 (this README); the C sources in this folder: nvidia-nemotron-3-ultra -->

# tray/gtk3 - kiko-tray for Linux (GTK3)

The Linux tray front-end of kiko, for Linux Mint / Cinnamon and any desktop that shows GTK3 status icons.
It puts one icon in the panel that shows whether the kiko gateway is running, and a menu to start, stop and
restart it and to open small windows (status, logs, models, model classes, keys, costs, settings, about).

The three C files here are only the GTK3 *view* (`gtk_view.c`), the service control (`gtk_host.c`) and
`main` (`gtk_main.c`). The state machine, HTTP client, JSON, icon drawing and dialog models live in
`tray/common/` and are shared with the Windows, macOS and Qt ports.

The sources were written by NVIDIA Nemotron 3 Ultra (see the `AI models:` header lines). Status: the
docs disagree whether it was ever run on a real Mint desktop (`docs/BUILD.md` says verified, the test plan says
untested); treat the GUI as untested until the manual plan [`docs/TEST-PLAN-MINT.md`](../../docs/TEST-PLAN-MINT.md)
has been walked through. The Linux CI job proves that it builds and that the core tests pass.

## Linux dependencies

Build (Debian / Ubuntu / Mint):

```
sudo apt install build-essential cmake pkg-config libgtk-3-dev git
sudo apt install libxapp-dev          # optional: native Cinnamon status icon (XApp)
```

`libxapp-dev` is optional. Without it the build uses `GtkStatusIcon` (works, but looks less native on
Cinnamon, and is gone on desktops that dropped it). cJSON is vendored in `tray/common/third_party/cjson/`;
no download is needed.

Run time:

| Package | Needed for |
|---|---|
| `libgtk-3-0`, `libxapp1` (if built with XApp) | the program itself |
| `libnotify-bin` (`notify-send`) | balloon messages after Start/Stop |
| `seahorse` | the "Open keyring" button in the Keys window |
| systemd user session | Start / Stop / Restart (`systemctl --user`) |
| the kiko gateway and its user unit `kiko-lite.service` (override: env `KIKO_UNIT`) | something to control (`kiko install`) |

## Build

From the repository root:

```
cmake -S tray -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure     # the portable core tests (7)
```

Result: `build/kiko-tray`. `-DKT_BUILD_GUI=OFF` builds only the core and its tests (no GTK needed).
A `.deb` is built with `bash packaging/build-tray-gtk3-deb.sh`. Tests are never part of the `.deb`.

## Run

```
./build/kiko-tray
```

The icon colour is the state: red = kiko not running, blue = starting, green = running and healthy,
orange = running but `/health` is not ok. Left click opens the status window, right click the menu.
Only one instance runs per user (lock file `$XDG_RUNTIME_DIR/kiko-tray.lock`; a second start prints
"kiko-tray is already running" and exits). Quit leaves kiko-lite running.

## Configuration

There is no config file. Environment variables only:

| Variable | Meaning | Default |
|---|---|---|
| `KIKO_PORT` | gateway port the tray polls (1-65535) | `4000` |
| `KIKO_API_TOKEN` | token for the management API (`/kiko/v1/*`) | not set |
| `~/.kiko/api-token` | first line is used as the token when `KIKO_API_TOKEN` is not set | not present |

The token is never logged or shown (the Keys window shows `NAME: *****` and present/missing only).
The gateway is polled every 5 s; after Start the tray waits up to 30 s for it to come up.
Icon PNGs for XApp are cached in `~/.cache/kiko-tray/`.

## Limits

- Controls only the systemd user unit named `kiko-lite` (`systemctl --user start|stop|restart kiko-lite`).
  A kiko started in some other way is watched, not controlled. No unit installed -> a notification says so.
- Windows that need the management API (logs, models, classes, keys, costs, settings) show
  "Management API not available" until the gateway serves the `/kiko/v1/*` endpoints
  (see `docs/API-CONTRACT.md`).
- Needs a panel with a status-icon area (Cinnamon, MATE, XFCE with the plugin, KDE via the legacy
  protocol). GNOME Shell shows no icon without an extension. Wayland-only sessions are untested.
- Not translated (English only); HiDPI works through GTK scaling but only 200% was looked at.
- Only the portable core is covered by automated tests (ctest). The GTK3 layer itself has no unit tests;
  CI proves that it compiles and links. Behaviour is checked by hand with the manual plan above.
