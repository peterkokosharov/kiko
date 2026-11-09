# Manual test plan - KDE Plasma (Qt layer)

Status: **UNTESTED and never compiled**. Also usable on Linux Mint with `-DKT_GUI=qt`.
[API] steps show "Management API not available" until kiko has `/kiko/v1/*` (API-CONTRACT.md).
Prerequisite: the user unit `kiko-lite.service` is installed (`tools/install-service.sh`).

| # | Step | Expected result |
|---|---|---|
| 1 | Build with `-DKT_GUI=qt` as in BUILD.md; CMake reports Qt6 (or "Qt6 not found, using Qt5"); run `ctest` | 6/6 pass |
| 2 | `systemctl --user stop kiko-lite`; run `./build/kiko-tray` | red icon in the Plasma system tray (StatusNotifier) |
| 3 | Run it twice | the second prints "already running" and exits |
| 4 | Start kiko-lite | icon turns green (orange when /health is not ok; blue right after Start) |
| 5 | Right-click the icon | the full menu; "Codex (coming)" greyed |
| 6 | Left or double-click the icon | Status dialog opens |
| 7 | Start / Stop / Restart | systemctl runs; a tray message shows the result |
| 8 | Open each dialog [API] | QDialog with a table or read-only monospaced text; one per kind; Close button; closing a dialog does not quit the tray |
| 9 | Keys dialog | `NAME: *****` plus present/missing; "Open KDE Wallet" starts kwalletmanager6/5 (or a message if missing) |
| 10 | Stop kiko, open Models | "kiko is not running"; no freeze |
| 11 | Restart plasmashell (`kquitapp6 plasmashell; kstart plasmashell`) | the icon returns |
| 12 | Scale 150% | icon and dialogs scale |
| 13 | Menu > Quit | icon gone; process ends |
