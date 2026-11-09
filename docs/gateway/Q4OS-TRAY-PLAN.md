<!-- SPDX-License-Identifier: MIT -->
<!-- Author: Peter Kokosharov <peter.kokosharov@gmail.com> -->
# kiko-UI for Q4OS 6 (TDE): plan for the coding agent

Reader: the local coding agent on the owner's Q4OS 6 machine (Debian Trixie, TDE R14 / Trinity).
Goal: finish the owner's almost-working TQt kiko-UI, a standalone tray-icon program, without touching the panel.
Anything not verified from the source is marked "(check on the machine)".

## RULES (read them before every step)

1. Do only the current step. Never start the next step on your own.
2. Before each step, state in ONE line what you will change. Then do it.
3. Change no file the step does not name.
4. Never edit kiko-lite itself (`kikolib/`, `kiko_lite/`). It is not part of this work. Missing kiko-lite endpoints are added by the orchestrator, not by you.
5. Never touch system files, `/opt/trinity`, `kickerrc` or the running panel unless the step says so.
6. Never restart or reconfigure kicker (no `dcop kicker ...`, no `killall kicker`, no edits of the panel or systray config) unless a step says so.
7. Back up before every change: `cp FILE FILE.bak-<step>`. Or work in a git repo and commit after each green step.
8. After each step, run its check. If the check fails, STOP. Report: what you ran, the output, your one-sentence guess. Wait for the owner.
9. Never try more than one fix without reporting.
10. Never store, show, log or write a key value. Never write `~/.kiko/.env`. Never write the SQLite DB directly.
11. Backend calls (HTTP, CLI, DB) must never block the GUI event loop. Use a timer, an asynchronous child process, or a worker thread, each with a timeout (for example 10 s). A hung backend must never freeze the menu or a dialog. In a TDE program start child processes with TDEProcess (or KRun), not TQProcess; mixing the two can install competing SIGCHLD handlers (from memory, check on the machine). Use one of them everywhere, including the existing code.
12. Do not ask "what now?" after breaking something. Stop BEFORE the risky action instead.
13. Do not install packages or use sudo unless the step says so and the owner agreed.
14. Every test that WRITES (add, remove, enable, disable, alias, settings, import) runs against a scratch DB: add `--db /tmp/kiko-test.db` to the kiko-lite command (every subcommand accepts `--db`; the env var `KIKO_DB_PATH` also overrides the default path, db.py `default_path`). Never test writes on the live `~/.kiko/kiko.db`. Before the first write on the live DB (only when the owner asks for a real change) make the backup from step 1.6.
15. Any install step: back up the old binary and the old `.desktop` file first. No `make install`. No sudo without the owner.

## Architecture and why the panel broke

1. The existing program is a standalone tray-icon application: its own process, an icon in the system tray (kicker only displays it), and a menu. Menu items run start / stop / restart or open a dialog window (logs, models, billing, ...) inside the same program.
2. Keep that design. Icon, menu and dialogs stay in one process. That is fine, because the process is separate from kicker. Do not turn it into a kicker applet and do not split it into more processes.
3. A crash of this program cannot kill the panel. So the earlier panel breakage most likely came from something else: installing files into kicker or `/opt/trinity`, restarting kicker, or editing `kickerrc` or the panel's systray config.
4. Therefore: run the program from the build folder, and do not restart or reconfigure kicker unless a step says so (rule 6). Phase 1 gives a backup and recovery commands in case it happens anyway.
5. The one thing that can still go wrong inside this process is a frozen GUI. Rule 11 covers it.

## kiko-lite facts (from the source in this repo)

- Start command used by the user unit (`tools/kiko-lite.service`): `kiko-lite serve --no-auth --host 127.0.0.1 --rss-limit-mb 1024`. The service name is `kiko-lite` (`systemctl --user ... kiko-lite`), installed by `tools/install-service.sh` from the kiko-lite zip.
- `serve` defaults (kiko_lite/cli.py): `--host 127.0.0.1`, `--port 4000`. Base URL: `http://127.0.0.1:4000`.
- HTTP routes (kiko_lite/app.py), the complete list:
  - `GET /health` returns `{"status": "ok"}`, needs no key.
  - `POST /v1/messages`
  - `POST /v1/messages/count_tokens`
  There are NO management routes (models, keys, costs, logs).
- Status: running = `systemctl --user is-active kiko-lite` prints `active` AND `GET /health` gives HTTP 200 and `status` = `ok`. stopped = `inactive`. starting = `activating` (the unit is Type=notify with Restart=on-failure, so this state is normal for a few seconds after start or after a crash; show a "starting" icon, not "error", unless it lasts longer than about 60 s). error = `failed`, or `active` but `/health` does not answer, or any other output of `is-active` (`deactivating`, `unknown`, empty).
- The kiko-lite launcher path: read the `ExecStart=` line of `systemctl --user cat kiko-lite` (check on the machine). Do not guess it.
- Start/Stop/Restart: `systemctl --user start|stop|restart kiko-lite`.
- Logs: `journalctl --user -u kiko-lite -n 200 --no-pager`.
- CLI subcommands (kiko_lite/cli.py). All accept `--db PATH`. Default DB `~/.kiko/kiko.db`. Output is TEXT, not JSON.
  - `import YAML [--dry-run]`
  - `add-model ALIAS --provider PROVIDER/ID --api-key-env NAME [--api-base URL] [--class CLASS] [--priority N] [--price-in X] [--price-out X] [--price-cache X]` (adds OR updates; see the WARNING below: do not use it to change a class or priority)
  - `list-models` (models, aliases, settings)
  - `list-classes` (name, keyword, role, enabled models)
  - `add-class NAME --keyword KW [--role TEXT]`
  - `enable-model ALIAS`, `disable-model ALIAS`, `delete-model ALIAS`
  - `add-alias GLOB TARGET`, `delete-alias GLOB`
  - `set-setting KEY VALUE`, `unset-setting KEY` (keys: `monthly_cap_usd`, `disable_billing`, `health_check_interval`, `health_no_probe`)
  - `fix-key ALIAS`
  - `costs [--days N]` (default 30)
  - `doctor` (offline; shows keystore backend, per-model key lines, monthly cap and spend; never prints a secret). Key line format (kiko_lite/cli.py): `      key NAME: found in <store>` or `      key NAME: NOT FOUND`. Match the exact text `: found in ` for present and `: NOT FOUND` for missing. A plain search for "FOUND" also matches "NOT FOUND": never use it.
  - `serve ...` (the UI never runs it; systemd does)
- There is no `probe` and no `change-class` subcommand.
- WARNING: there is NO safe way to change a model's class or priority with kiko-lite today. Re-running `add-model` overwrites the stored `api_base` (becomes empty if omitted) and `priority` (resets to 100), and replaces custom prices with catalogue prices. `list-models` does not show `api_base` or prices, and it prints the mapped provider (for example "custom"), which is not a valid `--provider` input. So never re-run `add-model` to change a class or a priority. The dialog action "change class / set priority" is DISABLED for the kiko-lite backend until kiko-lite gets a proper endpoint or subcommand (the orchestrator adds it, not you). Assigning a class when ADDING a new model (`add-model ... --class CLASS`) is fine.
- Security note (known limitation of kiko-lite, not of the UI): with the `dcop` keystore backend (TDE) kiko-lite passes a key value on the command line when it stores it, so the value is briefly visible in the process list (kikolib/keystore.py, "Known limits"). The UI cannot change that. Do not work around it.
- Keys live in the desktop wallet, not in the DB. `kikolib/keystore.py` backend order: `secret-tool` (GNOME keyring), `kwallet-query`, `dcop` (TDE kwalletd; marked UNVERIFIED on live TDE in the source). Key NAMES, models, classes, aliases and settings live in the SQLite DB.

### Operations with no JSON route in kiko-lite yet: "needs a kiko-lite JSON endpoint"

For the kiko-lite adapter these use the CLI (text output) until the orchestrator adds endpoints. Nemotron does NOT add them.

| Operation | Today (kiko-lite) | Status |
|---|---|---|
| status | `GET /health` + `systemctl --user is-active` | works, no JSON needed |
| list models / classes | CLI `list-models`, `list-classes` | needs a kiko-lite JSON endpoint |
| add / remove / enable model | CLI `add-model`, `delete-model`, `enable-model`, `disable-model` | needs a kiko-lite JSON endpoint |
| change class / set priority | NONE (re-running `add-model` is unsafe, see WARNING) | needs a kiko-lite JSON endpoint (action disabled in the UI until it exists) |
| keys presence | CLI `doctor` | needs a kiko-lite JSON endpoint |
| costs, monthly cap | CLI `costs`, `set-setting`; read-only DB read allowed | needs a kiko-lite JSON endpoint |
| logs | `journalctl --user -u kiko-lite` | no endpoint planned |
| import yaml | CLI `import [--dry-run]` | no endpoint planned |
| probe | none (runs inside the server); show `doctor` + `/health` | needs a kiko-lite JSON endpoint |

## Backend adapter layer

One interface, several implementations. Operations: `status`, `list_models`, `add_model`, `remove_model`, `change_class`, `keys_presence`, `costs`, `logs`. The dialogs talk to this interface, never to a backend directly.

1. kiko-lite: as in the table above (HTTP where a route exists, else CLI). Parse CLI text only as far as needed; otherwise show it verbatim.
2. kiko-full (our stripped LiteLLM fork). Real routes, from litellm/proxy/kiko_server.py:
   - `GET /health` and `GET /health/liveliness`: no key; returns `{"status":"healthy","classes":{CLASS:{"alive":n,"total":n}}}`.
   - `GET /kiko/models`: list models with class, status and "selected" per class.
   - `POST /kiko/models/{model_name}/class` with JSON body `{"class": "SONNET"}`: change the class (answers 400 for a bad class, 404 for an unknown model).
   - `POST /kiko/shutdown`: stops the proxy. The UI does NOT call it (use systemd or the owner's own start method).
   - `GET /v1/models` and `GET /models`: model names.
   - The `/kiko/*` routes accept only 127.0.0.1 / ::1 and need the master key: header `Authorization: Bearer <MASTER_KEY>` (also accepted: `x-api-key`). `/v1/models` also needs the key. `/docs` and `/openapi.json` exist (check on the machine).
   - Default bind: `127.0.0.1:4000`. There is NO route for add/remove model, keys or costs in this fork: for those use the owner's own tools (check on the machine) or the optional read-only DB read; the dialog action is disabled.
   - Read the master key from the wallet at call time; never store it in the UI source, a config file or a log. Check on the machine where the owner keeps it.
   Upstream LiteLLM (a different, full install, only if the owner uses it): its management routes (for example `/model/info`, `/model/new`, `/model/delete`, `/spend/logs`, `/key/info`) are from memory. Read the real ones from the running server's `/openapi.json` before coding (check on the machine). Do not assume they exist in kiko-full.
3. Read-only direct DB read of the billing/costs table: optional, `sqlite3` read-only mode (`file:...?mode=ro`). Writes ALWAYS go through the API or CLI, never through the DB.
4. Every call is asynchronous with a timeout (rule 11). Errors are shown verbatim.
5. The backend is chosen by one setting (kiko-lite, kiko-full, litellm) plus base URL. Default: kiko-lite at `http://127.0.0.1:4000`.

## Keys rule

The UI never stores, shows or logs a key value, and never writes `.env` or the DB directly. The Keys dialog shows only key names and whether each is found / working (from `kiko-lite doctor`; kiko-full has no key route, so show nothing there; upstream LiteLLM: check on the machine). No value field, no show button, no clipboard.

Display format: one row per key, `NVIDIA_API_KEY: *****` (the name, then a fixed string of stars, never a real length or part of the value), plus an "Open wallet" button or link. The button starts the wallet application where the value lives, as an asynchronous child process (rule 11). The wallet app is chosen from the keystore backend that `kiko-lite doctor` reports ("Implementations (kikolib factory)" section):
- `dcop` or `kwallet`: the TDE wallet manager, command `kwalletmanager` (TDE package may use a different name; check on the machine with `which kwalletmanager`).
- `secret-tool` (GNOME keyring): `seahorse` (check on the machine).
- If the program is not installed, show "wallet application not found" and do nothing else. Do not install it.
The UI never reads the value to open the wallet. The same rule and the same masked row apply to the `ANTHROPIC_AUTH_TOKEN` line in the Claude UI dialog.

## If the panel breaks (exact commands)

Run them in this order. Stop at the first one that works. Report what you ran.

1. `dcop kicker Panel restart`. If it fails, list the objects with `dcop kicker` and try `dcop kicker kicker restart` (from memory, check on the machine).
2. `killall kicker; setsid kicker >/dev/null 2>&1 &`. Use `setsid` and redirect the output: a plain `kicker &` dies with the agent's shell (from memory).
3. Restore the config: `cp ~/.trinity/share/config/kickerrc.bak-phase1 ~/.trinity/share/config/kickerrc`, then command 2.
4. Remove a bad file you installed. User level: delete only the file you added under `~/.trinity` or `~/.local`, then command 2. System level (anything under `/opt/trinity`): ONLY with sudo and ONLY if the owner agrees.
5. Last resort: log out and log back in.

(Exact config paths: check on the machine with `tde-config --path config`. TDE R14 renamed the tool; if `tde-config` does not exist, try `kde-config --path config`.)

## Report format (after each phase)

```
PHASE n: done / not done
CHECK: <command and its output, short>
NEXT: <next step number, waiting for owner OK>
```

## Phase 0: read-only inventory (change NOTHING)

0.1 Find the kiko-UI source.
- Commands: `ls ~`, `find ~ -maxdepth 3 -iname '*kiko*' -not -path '*/.cache/*' 2>/dev/null`
- Check: a list of candidate folders. If several, ask the owner which one.

0.2 List files and build system.
- Commands: `cd <ui dir>; ls -la; ls CMakeLists.txt Makefile *.pro configure* 2>/dev/null; git status 2>&1 | head`
- Check: you can say "cmake", "make with tqmoc" or "other".

0.3 Confirm it is a standalone program and find the toolchain.
- Commands: `grep -rn "KSystemTray\|KApplication\|main(" <ui dir> --include=*.cpp --include=*.h --include=*.txt | head -40`; `dpkg -l | grep -i "tdelibs14-trinity-dev\|tqt3-dev-tools\|cmake"`; `which tqmoc cmake`; `ls /opt/trinity`
- Check: report where `main()` and the tray class are, and which toolchain parts exist. If you find `KPanelApplet` instead, STOP and report. Do NOT install anything.

0.4 List the dialogs and menu items.
- Commands: `grep -rn "KDialog\|TQDialog\|KDialogBase\|insertItem\|KAction" <ui dir>`
- Check: a table "menu item / action (start, stop, restart, dialog) / file / class" (expected dialogs: logs, models, billing; others if present).

0.5 For each menu action and dialog, list the backend calls.
- Commands: `grep -rn "http\|TQHttp\|KIO\|TQProcess\|TDEProcess\|system(\|popen\|sqlite\|TQUrl" <ui dir>`
- Check: a table "dialog / call (URL, command or DB query) / which backend (kiko-lite, kiko-full, LiteLLM, direct DB)".

0.6 Find blocking work.
- Check: list every place on the GUI thread that waits: synchronous HTTP, `system()`, `popen`, `sleep`, blocking file or DB reads, `waitForFinished`, JSON parsing of data from the network. Give file and line.

0.7 Find what already works.
- Commands: read the code and notes only; `ls <ui dir>/build 2>/dev/null`. Do not build and do not run it.
- Check: a short list "works / broken or missing" per dialog.

0.8 Check the kiko-lite install and the wallet backend (check on the machine).
- Commands: `systemctl --user is-active kiko-lite`, `curl -s -m 3 http://127.0.0.1:4000/health`, `systemctl --user cat kiko-lite` (note the `ExecStart=` line: that is the launcher path), then `<launcher> doctor`.
- Check: report the port that answers, the "Implementations (kikolib factory)" lines and which keystore backend is chosen (expected: `dcop` KWallet; check on the machine). Report if `doctor` says `: NOT FOUND` for a key. Print no key value. Do not run `serve`. If the backend in use is kiko-full, say so and compare `curl -s -m 3 http://127.0.0.1:4000/openapi.json` route names with the kiko-full routes listed above (`/kiko/models`, `/kiko/models/{model_name}/class`). If it is upstream LiteLLM, report its route list for `/model`, `/spend`, `/key` (check on the machine).

0.9 Report the inventory in the report format. WAIT for the owner's OK before Phase 1.

## Phase 1: safety net

1.1 Put the UI source under git (or copy it).
- Files touched: new `.git` in the UI dir (or a copy `<ui dir>.orig`).
- Commands: `cd <ui dir>; git init; git add -A; git commit -m "baseline before changes"` (if it is already a repo: `git status`, and commit only if the owner agrees).
- Check: `git log --oneline` shows one commit; `git status` is clean.

1.2 Back up the panel config.
- Files touched: new `~/.trinity/share/config/kickerrc.bak-phase1` (path: check on the machine).
- Commands: `cp ~/.trinity/share/config/kickerrc ~/.trinity/share/config/kickerrc.bak-phase1`
- Check: `ls -l ~/.trinity/share/config/kickerrc*` lists both files, same size.

1.3 Write down the recovery commands.
- Files touched: new `<ui dir>/PANEL-RECOVERY.txt` with the "If the panel breaks" section copied.
- Check: `cat` shows the 5 recovery items.

1.4 Note what the owner installed earlier (read only).
- Commands: `find /opt/trinity ~/.trinity ~/.local ~/.config -iname '*kiko*' 2>/dev/null`
- Check: the exact paths are written into `PANEL-RECOVERY.txt`.

1.5 Test the panel restart command (do it only after 1.1 to 1.4). This is the only step that restarts kicker.
- Commands: `dcop kicker Panel restart`. If this fails, list the objects with `dcop kicker` and try `dcop kicker kicker restart` (from memory, check on the machine). Report which command worked.
- Check: the panel disappears for a moment and returns. If not, run recovery item 2, STOP and report.

1.6 Back up the kiko-lite DB (read-safe, do not change the DB).
- Files touched: new `~/.kiko/kiko.db.bak-phase1`.
- Commands: either stop the service first: `systemctl --user stop kiko-lite; cp ~/.kiko/kiko.db ~/.kiko/kiko.db.bak-phase1; systemctl --user start kiko-lite`; or, while it runs: `sqlite3 ~/.kiko/kiko.db ".backup '$HOME/.kiko/kiko.db.bak-phase1'"` (needs the `sqlite3` tool: check on the machine; do not install it). If the DB path differs (env `KIKO_DB_PATH`), use that path.
- Check: `ls -l ~/.kiko/kiko.db*` lists both files; the backup is not empty. Repeat this step before any later write to the live DB, with a new suffix (`bak-<step>`).

1.7 Make a scratch DB for all write tests.
- Commands: `<launcher> list-models --db /tmp/kiko-test.db` (creates or reads the scratch file; `/tmp/kiko-test.db` is only a scratch DB). Then `<launcher> add-class TESTCLASS --keyword test --db /tmp/kiko-test.db`.
- Check: `<launcher> list-classes --db /tmp/kiko-test.db` shows TESTCLASS; `ls -l ~/.kiko/kiko.db` has an unchanged modification time. (Whether `list-models` creates the file: check on the machine; if it does not, the `add-class` creates it.)

1.8 Report. WAIT for the owner's OK.

## Phase 2: adapter layer

Files touched: only the UI source dir. Build in a `build/` folder, run from there, no `make install`, no kicker restart.

2.1 Define the interface (one header, `backend.h`): `status`, `listModels`, `addModel`, `removeModel`, `changeClass`, `keysPresence`, `costs`, `logs`. Each call is asynchronous (TDEProcess or a worker thread, see rule 11) and returns text or a structure plus an error string, and each backend reports which operations it supports (for example `changeClass` is unsupported for kiko-lite). No implementation yet.
- Check: it compiles.

2.2 Implement kiko-lite: `status` (systemctl + `/health`), `logs` (journalctl), the rest through the CLI as in the table. `changeClass` returns "unsupported" (WARNING above). All as asynchronous child processes with a 10 s timeout. Show CLI stdout/stderr verbatim.
- Check per operation: run it with a command-line flag `<binary> --selftest <op>` (add it, no GUI); the output equals the same command typed in a terminal. Read-only operations only on the live DB; any write test uses `--db /tmp/kiko-test.db`.

2.3 Optional, only if the owner asks: read-only DB read for costs. Check: the number equals `costs --days 30`.

2.4 Only if the owner uses kiko-full: implement `status` (`GET /health`), `listModels` (`GET /kiko/models`) and `changeClass` (`POST /kiko/models/{model_name}/class`), with the master key as `Authorization: Bearer` read from the wallet at call time. The other operations return "unsupported". Upstream LiteLLM: only if the owner asks, after the routes are verified in Phase 0.8. Check each operation against `curl` on the same route (the `curl` header must not be written into any file or log; the owner types the key, or the owner runs it).

2.5 Replace the blocking calls found in step 0.6 with adapter calls. Number them 2.5.1, 2.5.2, ... one call per sub-step (for example 2.5.1 = the first call in the list from 0.6). Check for each: the dialog shows the same data as before and the menu stays responsive when the backend is stopped (stop the service, open the dialog: an error appears within the timeout, the menu still works).

2.6 Run from the build dir and confirm the panel is untouched. Commit. Report. WAIT for the owner's OK.

## Phase 3: fix or finish each dialog (one dialog per step, own commit)

Each dialog reads and writes through the adapter, shows errors verbatim, shows the exact command or request before it changes data, and asks for confirmation before delete. Check for each: the dialog opens, shows real data, and a deliberate bad input shows the backend error unchanged.

3.1 Logs: Refresh button, read-only text, no follow mode.
3.2 Models dialog: list, add, remove, enable/disable, probe.
- 3.2.1 list = `list-models`. Read only. Check: same output as in a terminal.
- 3.2.2 add = `add-model` (new alias only; if the alias exists, refuse and say "use the class/priority rules"; never update an existing model). Use the API key NAME only. Test with `--db /tmp/kiko-test.db`. Check: `list-models --db /tmp/kiko-test.db` shows it.
- 3.2.3 remove = `delete-model` (confirm first). Scratch DB for tests.
- 3.2.4 enable/disable = `enable-model` / `disable-model`. Scratch DB for tests.
- 3.2.5 probe = show the `/health` result plus the `doctor` lines for that model (kiko-lite has no probe command).
- Check for each: the dialog shows the exact command first, then the CLI output verbatim; `list-models --db /tmp/kiko-test.db` afterwards shows the change. Before the dialog is used on the live DB, the owner must OK it.
- To test against a scratch DB the dialog needs a "DB path" option (default: none, which means the CLI default). Add it as a hidden command-line flag `--db PATH` of the tray program that is passed to every kiko-lite call.
3.3 Model class dialog: list the classes, assign a model to a class, set the order inside the class.
- 3.3.1 List the classes with `list-classes` (read only). The seeded classes in kiko_lite/db.py are OPUS, SONNET, HAIKU, FABLE; the table is the truth, so always show what `list-classes` returns. Check: the dialog output equals `list-classes` typed in a terminal.
- 3.3.2 Add a class: `add-class NAME --keyword KW [--role TEXT]`. Test only with `--db /tmp/kiko-test.db`. Check: `list-classes --db /tmp/kiko-test.db` shows it.
- 3.3.3 Assign a class to a NEW model: `add-model ALIAS --provider ... --api-key-env NAME --class CLASS` (scratch DB for tests). This is the only class assignment kiko-lite supports today.
- 3.3.4 Change the class or the priority of an EXISTING model: for the kiko-lite backend the buttons are DISABLED with the text "needs a kiko-lite update" (see the WARNING above). Never run `add-model` for this. For the kiko-full backend the class change is `POST /kiko/models/{model_name}/class` with `{"class": "..."}` (route list above); there is no route for priority. Test the kiko-full call only if the owner has a kiko-full server and says which model may be changed.
- Check for 3.3.4: with the kiko-lite backend the buttons are greyed out and no command is run.
3.4 Keys: names and found / working only (Keys rule above). Parse the doctor lines with the exact text `: found in ` and `: NOT FOUND` (see the doctor note above). Rows look like `NVIDIA_API_KEY: *****` with an "Open wallet" button (Keys rule). Check: with a scratch DB holding one model whose key is missing, the dialog shows NOT FOUND for it and no value anywhere; the button starts the wallet application chosen from the `doctor` backend, or shows "wallet application not found".
3.5 Costs and cap:
- 3.5.1 Costs: `costs --days N` (read only). Check: same output as in a terminal.
- 3.5.2 Cap: `set-setting monthly_cap_usd VALUE` / `unset-setting monthly_cap_usd`; validate VALUE is a number first. Test with `--db /tmp/kiko-test.db`; check with `doctor --db /tmp/kiko-test.db` ("Monthly cap").
3.6 Claude UI dialog: configures Claude Code to use kiko. Details (sub-steps 3.6.1 to 3.6.5) are in the section "Claude UI dialog" below. Do not start it before 3.2 and 3.3 are green.
3.7 Status in the tray:
- 3.7.1 Poll every 5 s with a timer, asynchronously. States: running, starting (`activating`), stopped, error (mapping in the kiko-lite facts). A tooltip shows the state. Check: stop and start the service; the icon follows within 10 s; the panel stays up.
- 3.7.2 Menu Start, Stop, Restart (`systemctl --user start|stop|restart kiko-lite`, errors verbatim; disable the item while it runs). Check: each item changes the state as expected; report each one.
- 3.7.3 One menu item per dialog. Check: each opens its dialog and the menu stays responsive.

After each step: commit, report, WAIT.

### Claude UI dialog (step 3.6, sub-steps 3.6.1 to 3.6.5)

Purpose: the user picks, per Claude Code slot, which kiko model or class it maps to. The dialog writes one env file. A script then starts Claude Code with it.

Slots. The env var names must be checked against the installed Claude Code (`claude --help`, its docs, `claude --version`) before coding (check on the machine):
- `ANTHROPIC_BASE_URL=http://127.0.0.1:4000` (kiko-lite default port from kiko_lite/cli.py)
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `CLAUDE_CODE_SUBAGENT_MODEL`
- a FABLE slot, only if Claude Code has one (check on the machine; if not, do not invent a variable)
- `ANTHROPIC_AUTH_TOKEN`: needed only if kiko-lite runs with auth. The service uses `--no-auth`, so normally it is not needed. The dialog never asks for the value, never writes it and never logs it. If auth is on, the owner adds that one line to the file by hand. If the line exists, the dialog shows it masked as `ANTHROPIC_AUTH_TOKEN: *****` (with the same "Open wallet" button as in the Keys dialog, see the Keys rule) and keeps the line unchanged on save.

The choice list for each slot comes from `list-models` and `list-classes` (through the adapter). The value written is the model alias or the class name that kiko-lite resolves (check on the machine how kiko-lite maps a requested model name; classes are matched by their keyword in the requested name, see `list-classes`).

3.6.1 Read the existing file `~/.kiko/claude-code.env` if it exists (read only). Check: the dialog shows its slots, or empty fields.
3.6.2 Write the file:
- Format: `KEY=value` lines, no quotes, no `export`.
- Mode 600. Atomic write: write a temp file in `~/.kiko/` (same folder), set mode 600, then rename over the target. Keep the previous file as `~/.kiko/claude-code.env.bak`.
- Show the final file content in the dialog before saving. It must contain no secrets (only the masked AUTH_TOKEN case above).
- Check: `ls -l ~/.kiko/claude-code.env` shows `-rw-------`; `.bak` exists after the second save; `cat` equals what the dialog showed. Test first with a scratch path (give the dialog an env-file path option, for example a hidden `--env-file /tmp/claude-code.env` flag), not on the owner's real file.
3.6.3 Ship `run-claude.sh` into the UI source dir (and copy it to `~/.local/bin` only in Phase 5). It must do only this: `set -a; . "$HOME/.kiko/claude-code.env"; set +a; exec claude "$@"`. Same idea as the owner's ai_runtime project (check on the machine; the agent has not seen that file). Back up and do not overwrite an existing one. Make it executable.
- Check: `sh -n run-claude.sh` prints nothing; `./run-claude.sh --version` prints the Claude Code version; `sh -c '. ~/.kiko/claude-code.env; echo $ANTHROPIC_BASE_URL'` prints `http://127.0.0.1:4000`.
3.6.4 Optional menu item "Start Claude Code via kiko": opens a terminal running `run-claude.sh` as an asynchronous child process (terminal program: check on the machine, for example `x-terminal-emulator -e` or TDE `konsole -e`; check on the machine). The tray does not wait for it.
- Check: a terminal opens with Claude Code; the tray menu stays responsive.
3.6.5 The dialog must NOT edit `~/.claude/settings.json`, `~/.bashrc`, `~/.profile` or any other shell rc file. Alternative for the owner only, not built: put the same variables into `~/.claude/settings.json` (`env` block) or the shell rc by hand. Mention this in the dialog's help text.
- Check: `git diff` and `ls -l` show that only the files named in 3.6.2 and 3.6.3 changed.

## Phase 4: Import config.yaml

4.1 Menu item and dialog "Import config.yaml...": a file chooser, then `kiko-lite import --dry-run FILE`, show the output to the user. Only if the user presses Confirm, run `kiko-lite import FILE` and show the output verbatim. (Flags verified in kiko_lite/cli.py: `import YAML [--dry-run]`.)
- The key values go from the file into the wallet by kiko-lite itself (kikolib/keystore.py, order secret-tool, kwallet-query, dcop). The UI must not read, copy or log the file contents.
- After a successful import show: "The yaml file still holds your keys in plain text. Delete it or protect it." The UI NEVER deletes or changes the file.
- Check: run `list-models --db /tmp/kiko-test.db` before and after the dry-run: identical. ALL import tests use the scratch DB: the dialog passes `--db /tmp/kiko-test.db` (`import --dry-run FILE --db /tmp/kiko-test.db`; placement of `--db` after the subcommand is accepted by the parser). Test first with a file without real keys. Then check `doctor --db /tmp/kiko-test.db` shows the keys as `: found in ` (wallet backend: check on the machine). Run the live import only when the owner says so, after the DB backup of step 1.6 (repeat it as `bak-<step>`).
- Note: a test import with real keys writes them into the real wallet even when the DB is a scratch DB. Use a yaml with fake key values and tell the owner which key NAMES will be written, so they can be removed by hand.

4.2 Report. WAIT.

## Phase 5: install into the user's home plus autostart

5.1 Find which autostart folder TDE uses (check on the machine): `ls -d ~/.config/autostart ~/.trinity/Autostart 2>&1; tde-config --path autostart` (fallback `kde-config --path autostart`). Use the one the tool reports.
5.2 Install the binary: first back up what is there: `for f in <binary> run-claude.sh; do [ -e ~/.local/bin/$f ] && cp ~/.local/bin/$f ~/.local/bin/$f.bak-5.2; done`. Then `mkdir -p ~/.local/bin; cp build/<binary> run-claude.sh ~/.local/bin/`. No `make install`, no sudo, no `/opt/trinity` write, no .deb.
- Check: `~/.local/bin/<binary> &` starts it, the tray icon appears, Quit works, the panel is untouched. Do not restart kicker.
5.3 Add a `.desktop` file for autostart in the folder from 5.1 with `Type=Application`, `Name=kiko tray`, `Exec=<full path to the binary>`. Back up any existing file with the same name first (`cp FILE FILE.bak-5.3`). Do not edit `kickerrc` or the panel config.
- Check: the owner logs out and in. The tray icon appears and the panel is intact. Report. WAIT.
