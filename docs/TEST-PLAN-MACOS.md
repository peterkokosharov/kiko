# Manual test plan - macOS (11 or newer)

Status: **UNTESTED and never compiled** - written without a Mac. Treat every step as a first run.
[API] steps show "Management API not available" until kiko has `/kiko/v1/*` (API-CONTRACT.md).

| # | Step | Expected result |
|---|---|---|
| 1 | Build as in BUILD.md; `ctest --test-dir build --output-on-failure` | 6/6 pass |
| 2 | Edit `docs/macos/com.kiko.lite.plist` (path to kiko-lite), copy to `~/Library/LaunchAgents/` | file in place |
| 3 | `open build/kiko-tray.app` | a coloured dot in the menu bar; **no Dock icon**, no app window |
| 4 | Dot colours: kiko stopped / running / not-ok / just started | red / green / orange / blue |
| 5 | Click the dot | menu with the full item list; "Codex (coming)" greyed |
| 6 | Menu > Start / Stop / Restart | launchctl runs (check `launchctl print gui/$(id -u)/com.kiko.lite`); a notification banner shows the result |
| 7 | Open Status, Logs, Models, Model classes, Keys, Costs, Settings, About [API] | native windows; table view for tables, monospaced read-only text for logs; one window per kind |
| 8 | Keys dialog | `NAME: *****` plus present/missing; no values |
| 9 | Keys > "Open Keychain Access" | Keychain Access launches |
| 10 | Stop kiko, open Models | "kiko is not running"; the menu stays responsive |
| 11 | Start the app twice | the second exits with "already running" |
| 12 | Dark and light menu bar | the coloured dot is visible on both |
| 13 | Quit from the menu | the dot disappears; kiko keeps running |
