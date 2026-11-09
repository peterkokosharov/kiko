# Manual test plan - Linux Mint 22.3 Cinnamon

Status: **untested** (never compiled by its author). [API] steps show "Management API not available"
until kiko has the `/kiko/v1/*` endpoints (API-CONTRACT.md); that is the expected result.
Prerequisite: the user unit from the kiko repo is installed (`tools/install-service.sh`), so
`systemctl --user status kiko-lite` works.

| # | Step | Expected result |
|---|---|---|
| 1 | Install packages and build as in BUILD.md; `ctest --test-dir build --output-on-failure` | 6/6 tests pass; CMake printed "XApp status icon: found" |
| 2 | `systemctl --user stop kiko-lite`, run `./build/kiko-tray` | a **red** icon in the Cinnamon panel tray; tooltip "KIKO: not running" |
| 3 | Run it again | "kiko-tray is already running" on stderr; one icon |
| 4 | `systemctl --user start kiko-lite` | icon turns **green** within about 5 s |
| 5 | Make /health answer not-ok | icon **orange** |
| 6 | Right-click the icon | full menu (status line, Start, Stop, Restart, Status, Logs, Models, Model classes, Keys, Costs, Settings, About, "Codex (coming)" greyed, Quit) |
| 7 | Left-click the icon | Status window opens |
| 8 | Menu > Start / Stop / Restart | blue while starting; notification via notify-send; `systemctl --user status kiko-lite` agrees |
| 9 | Menu > Start with the unit not installed | notification "systemctl ... failed: Unit kiko-lite.service not found" |
| 10 | Open Logs, Models, Model classes, Keys, Costs, Settings, About [API] | GTK windows: table or text; one window per kind; a second open refreshes and raises it |
| 11 | Keys dialog | `NAME: *****` plus present/missing, no values |
| 12 | Keys > "Open keyring (seahorse)" | Passwords and Keys (seahorse) opens; if not installed a notification says so |
| 13 | Stop kiko, open Models | "kiko is not running"; no UI freeze |
| 14 | Restart the panel: `cinnamon --replace &` (or log out and in) | the icon comes back with the right colour |
| 15 | Settings > Display: HiDPI 200% | icon and dialogs scale; nothing clipped |
| 16 | Build without libxapp-dev in a fresh build dir | icon still appears (GtkStatusIcon fallback) |
| 17 | Menu > Quit | the icon is gone; process ends; kiko-lite keeps running |
