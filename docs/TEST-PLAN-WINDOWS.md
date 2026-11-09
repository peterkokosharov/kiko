# Manual test plan - Windows 10 and 11

Status: **untested** (the code was never compiled by its author). Run once on Windows 10 and once on
Windows 11. "Management API" = the future `/kiko/v1/*` endpoints (API-CONTRACT.md); until kiko has them,
steps marked [API] show "Management API not available", which is the expected result.
Note: the app calls Windows DLLs directly (Win32 API from C); build tools are listed in BUILD.md.

| # | Step | Expected result |
|---|---|---|
| 1 | Build as in BUILD.md; run `ctest --test-dir build -C Release` | all 6 tests pass |
| 2 | Stop kiko. Run `kiko-tray.exe` | no console window; a **red** icon near the clock (maybe under the ^ overflow); tooltip "KIKO: not running (port 4000)" |
| 3 | Run `kiko-tray.exe` a second time | message "kiko-tray is already running"; still one icon |
| 4 | Start kiko-lite (`kiko-lite serve --no-auth --host 127.0.0.1`) | within about 5 s the icon turns **green**; tooltip "KIKO: running" |
| 5 | Make /health answer something other than ok | icon **orange**, tooltip "degraded" |
| 6 | Right-click the icon | menu: status line, Start, Stop, Restart, Status, Logs, Models, Model classes, Keys, Costs, Settings, About, "Codex (coming)" (greyed), Quit |
| 7 | With kiko down: Start enabled, Stop disabled. With kiko up: the reverse | as described |
| 8 | Menu > Start (kiko down) | icon turns **blue** at once, balloon "kiko started", then green when kiko answers |
| 9 | Menu > Stop (kiko started by the tray) | balloon "kiko stopped", icon red. Stop on a kiko started elsewhere: balloon "was not started by the tray" |
| 10 | Menu > Restart | blue, then green |
| 11 | Double-click the icon | Status window opens (address, health, API info) |
| 12 | Open Logs, Models, Model classes, Keys, Costs, Settings, About [API] | each is a real window with Segoe UI text; without the API a one-line "Management API not available"; with it: table (Models/Classes/Keys/Costs) or text (Logs, newest line visible) |
| 13 | Open the same dialog twice | one window only, brought to front, content refreshed |
| 14 | Press Refresh in a dialog; press Esc; Tab through the controls | content reloads; Esc closes; Tab moves focus; Close closes |
| 15 | Keys dialog | lines like `NVIDIA_API_KEY: *****` with "present"/"missing"; no key value anywhere |
| 16 | Keys > "Open Credential Manager" | Windows Credential Manager opens |
| 17 | Settings dialog | shows port, start command, token SOURCE ("env KIKO_API_TOKEN" or "file ..."), never the token |
| 18 | Stop kiko, open Models/Costs | "kiko is not running"; the UI stays responsive (no freeze) |
| 19 | Restart Explorer (Task Manager > Windows Explorer > Restart) | the tray icon reappears by itself with the right colour |
| 20 | Set display scaling to 150%, restart the app; drag a dialog to a monitor with another scaling if available | icon sharp; dialogs and fonts scale, nothing clipped |
| 21 | Menu > Quit | the icon disappears at once (no ghost icon when the mouse crosses the tray); process gone; kiko keeps running |
| 22 | Kill `kiko-tray.exe` in Task Manager | the stale icon vanishes when hovered (normal Windows behaviour); a new start works |
