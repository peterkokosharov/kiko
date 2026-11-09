# KIKO — Linux Keystore Live Test (Q4OS TDE and Linux Mint)

**Author:** Peter Kokosharov  
**Covers:** `kikolib/keystore.py` Linux backends `secret-tool`, `kwallet`, `dcop`  
**Operator:** an AI agent (Claude Code) driving a shell on the machine under test, with a human nearby.

## 0. Purpose

kiko reads API keys from the OS secret store. On Linux three backends exist, and two of them
(`kwallet`, `dcop` = TDE/Q4OS kwalletd over DCOP) have never been run on a live system. This test
proves, on two real machines, that each available backend can store, read, overwrite and delete a
THROWAWAY entry, and that kiko falls back to a `.env` file when no store works. It also collects the
real DCOP method signatures so they can be compared with what the code assumes.

Machines:

| Machine | Expected backend | Python |
|---|---|---|
| Q4OS 6 Andromeda, TDE (Debian 13) | `dcop` (TDE kwalletd) | 3.13 |
| Linux Mint 22.3 Cinnamon (Ubuntu 24.04) | `secret-tool` (GNOME Keyring) | 3.12 |

Run the whole document on each machine. Nothing is installed by the kit itself.

## 1. Safety rules (read first, obey always)

1. No real API keys in this test. The script writes only entries named `KIKO_SELFTEST_<8 hex>` with random values.
2. Never paste a secret, password or wallet password into the chat or into a file. The script never prints values; if you see one anywhere, stop and tell the human.
3. Do not delete, overwrite or read wallet/keyring entries you did not create. Do not browse the wallet.
4. If a wallet or keyring password window appears, or a KWallet "allow access" window (buttons Allow once / Always / Deny) appears, STOP and ask the human to answer it. Do not type a password, click Allow/Always/Deny yourself, bypass the window or retry in a loop.
5. Installing a package is the human's decision. Ask first, give the exact command, wait for a yes.
6. Do not use `sudo`, do not change system settings, do not edit files outside the repo folder and `/tmp`.
7. Do not run `git push`, `git pull`, `git fetch` or `pip`. Do not commit.
8. If any command asks for something not described here, stop and ask the human.

## 2. Prerequisites

Work in the repo / bundle root (the folder that contains `kikolib/` and `tools/`). Python 3.11+ is required, stdlib only.

### 2.1 Common (both machines)

```bash
cd <repo-or-bundle-root>
ls kikolib/keystore.py tools/linux_keystore_check.py
python3 --version
```

Expected: both files listed; Python 3.12 or 3.13 (3.11 or newer passes).

Run the session from the desktop itself (a terminal window inside the graphical session), not over SSH. If you are on SSH, see section 8.1.

```bash
echo "DESKTOP=$XDG_CURRENT_DESKTOP DBUS=${DBUS_SESSION_BUS_ADDRESS:+yes} DISPLAY=${DISPLAY:+yes}"
```

Expected on Mint: `DESKTOP=X-Cinnamon DBUS=yes DISPLAY=yes`. Expected on Q4OS TDE: `DESKTOP=` may be empty or `TDE`, `DISPLAY=yes`; `DBUS` may be empty (TDE uses DCOP, not D-Bus; that is fine).

### 2.2 Q4OS / TDE

```bash
command -v dcop kded kwalletd tdewalletd 2>&1; echo "exit=$?"
```

`dcop` must be present (it ships with TDE). `kwalletd` / `tdewalletd` may or may not be separate binaries; the wallet normally runs as a `kded` module. If `dcop` is missing, record that and continue: the result is a finding, not a failure of the operator.

Make sure a wallet exists: in TDE this is the "TDE Wallet" (`kdewallet`). If none was ever created, the first access opens a wizard window: stop and ask the human to complete it (rule 4).

### 2.3 Linux Mint

```bash
command -v secret-tool; echo "exit=$?"
```

If `secret-tool` is missing, ask the human: "secret-tool is in the package libsecret-tools; may I install it? The command would be: `sudo apt install libsecret-tools`." Do nothing until the human answers yes (rule 5). If the answer is no, run the test anyway and classify the machine as follows. Section 3 will show `secret-tool` probe `no` (`secret-tool not on PATH`) and exit code 1: that is EXPECTED in this case. Do the section 7 fallback check. Final verdict for Mint: `BLOCKED: secret-tool not installed, human decided not to install; fallback check PASS` (or `fallback check FAIL`). BLOCKED is not a FAIL of kiko; it means the Linux store on Mint was not exercised and the human decides about installing. If the human says yes and installs it, run everything normally and the verdict is PASS/FAIL by section 9.

GNOME Keyring must be unlocked: Mint auto-unlocks it at login. If a keyring password window appears during the test, rule 4 applies.

## 3. Run the probe (main step)

```bash
python3 tools/linux_keystore_check.py --json /tmp/kiko_keystore_$(hostname).json > /tmp/kiko_keystore_$(hostname).txt 2>&1; echo "exit code: $?"
```

Run the line above as ONE shell call (the `$?` must come from the same call; no pipe is used, so the exit code is the script's). Then show the console output with `cat /tmp/kiko_keystore_$(hostname).txt`. The script prints a NOTE line before calls that may prompt (password window or KWallet "allow access" window). Each call times out after 20 s. If the screen shows such a window, stop and ask the human (rule 4); then rerun the same command.

On a failed step the console shows every subprocess call of that step (for dcop both targets, `[kded kwalletd]` and `[kwalletd]`, with the method, the handle, the return code and, for open/writePassword/removeEntry/close, the returned number; never readPassword output). Copy these lines into your report unchanged.

Options: `--backend auto|secret-tool|kwallet|dcop` (default `auto`: every available backend), `--keep` (do not delete the throwaway entry), `--timeout N` (seconds per call).

### 3.1 Expected output (shape)

```
== System ==        OS, python, desktop, D-Bus/DISPLAY present yes/no
== Tools on PATH == secret-tool, kwallet-query, dcop, kwalletd, kded ... (path or -)
== Probes ==        one line per backend: ok/no + reason
== Factory choice == auto pick and the pick with KIKO_KEYSTORE_IMPL forced to each backend
== Round trip ==    per available backend, steps marked [PASS]/[FAIL]
== dcop diagnostics == raw dcop listings and function signatures (read-only)
== Summary ==       table and a final RESULT line
```

A passing backend shows exactly these steps, all `[PASS]`:

```
[PASS] set value 1
[PASS] get equals value 1
[PASS] overwrite with value 2
[PASS] get equals value 2 (not value 1)
[PASS] keystore.get_secret() with KIKO_KEYSTORE_IMPL equals value 2
[PASS] delete
[PASS] get after delete is None
```

Values appear only as `len=... sha256=xxxxxxxx`. For `kwallet` the delete steps are skipped and the line
`manual cleanup needed: entry KIKO_SELFTEST_xxxxxxxx in kdewallet/Passwords` is printed: that is expected,
report it to the human, do not try to remove the entry yourself.

### 3.2 Expected per machine

| Machine | Expected PASS | Notes |
|---|---|---|
| Mint | `secret-tool` | `kwallet` and `dcop` probe "not on PATH" (fine) |
| Q4OS TDE | `dcop` (this is the unknown) | `secret-tool` probably "no DBUS..." or "not on PATH" (fine) |

## 4. Force each backend through the kiko switch

`KIKO_KEYSTORE_IMPL` makes kiko use exactly one backend (error if it is unavailable). Run once per backend that probed ok:

```bash
KIKO_KEYSTORE_IMPL=secret-tool python3 tools/linux_keystore_check.py --backend secret-tool --json /tmp/kiko_force_secret-tool.json
KIKO_KEYSTORE_IMPL=kwallet     python3 tools/linux_keystore_check.py --backend kwallet     --json /tmp/kiko_force_kwallet.json
KIKO_KEYSTORE_IMPL=dcop        python3 tools/linux_keystore_check.py --backend dcop        --json /tmp/kiko_force_dcop.json
```

Run ONLY the lines whose backend probed `ok` in section 3; skip the others (do not force an unavailable backend here; a skipped line has no pass/fail). The check `keystore.get_secret() with KIKO_KEYSTORE_IMPL` inside each round trip already exercises the same path kiko uses.

An unknown value must be rejected by the factory:

```bash
KIKO_KEYSTORE_IMPL=bogus python3 -c "import sys; sys.path.insert(0,'.'); from kikolib import keystore; print(keystore.set_secret('KIKO_SELFTEST_X','x'))"
```

Expected: `False` (no traceback, nothing stored).

## 5. dcop diagnostics (Q4OS TDE: the important part)

The `== dcop diagnostics ==` section of section 3 is read-only. What to look at, and what the code assumes
(`DcopStore` in `kikolib/keystore.py`):

| Item | Code assumes | Check in the output |
|---|---|---|
| App/object | `dcop kded kwalletd ...`, fallback `dcop kwalletd ...` | `kded exposes 'kwalletd': True` and `kwalletd` appears under `$ dcop kded` |
| `open` | `open(wallet, wId) -> int handle` (called `open kdewallet 0`) | line `... open(` with 2 args |
| `readPassword` | `(handle, folder, key) -> string` | 3 args |
| `writePassword` | `(handle, folder, key, value) -> 0 on success` | 4 args |
| `removeEntry` | `(handle, folder, key) -> 0 on success` | 3 args |
| `close` | `(handle, force)` | 2 args |

The script prints `DcopStore assumptions vs 'functions' output of target [<app object>]` for every target whose `functions` call succeeded (for example `[kded kwalletd]`, or `[kded tdewalletd]`), with OK or MISMATCH/MISSING per method. The code only ever calls the targets `kded kwalletd` and `kwalletd`, so a comparison for any other target shows a naming difference, which is itself a finding. A
MISMATCH or MISSING is a FINDING to report, not something to fix. Also note:

- whether any command in the diagnostics ended `TIMEOUT` (wallet prompt or daemon not reachable);
- whether other objects with "wallet" in the name were listed (TDE may name the module differently, for example `tdewalletd`); the script runs `functions` on each of them, and that output is the most valuable data to send back.

## 6. What to collect and send back

Send to the human (as files or pasted text; they contain no secrets by design):

1. `/tmp/kiko_keystore_<hostname>.json` (full result) and `/tmp/kiko_keystore_<hostname>.txt` (console output).
2. The `/tmp/kiko_force_*.json` files from section 4.
3. The result of section 7 (one line each).
4. Any password window, KWallet "allow access" window or other prompt you saw, at which step, and what the human did.
5. Any `manual cleanup needed` lines (kwallet) and the exact entry names.
6. For failures: the raw output of `dcop kded kwalletd functions` (already inside the JSON under `dcop_diagnostics`).

Do not summarise away errors: paste them.

## 7. Fallback check: `.env` next to the DB

How kiko finds a key (verified in `kiko_lite/cli.py`, `cmd_serve` and `_key_source`): environment variable
first, then the OS store (`keystore.get_secret`), then a `.env` file that sits NEXT TO THE DATABASE FILE
(`<db folder>/.env`, never the current folder). The DB file is `--db`, else env `KIKO_DB_PATH`, else `~/.kiko/kiko.db`.

This step uses a temporary folder and a throwaway name that is in no store, so it needs no store at all.

```bash
D=$(mktemp -d)
printf 'KIKO_SELFTEST_FALLBACK=throwaway-not-a-key\n' > "$D/.env"
cat > "$D/fb.py" <<'EOF'
import os, sys
sys.path.insert(0, os.getcwd())
from kiko_lite import cli
from kiko_lite.db import default_path
p = default_path(None)
name = "KIKO_SELFTEST_FALLBACK"
print("source:", cli._key_source(name, cli._dotenv_values(p)))
EOF
KIKO_DB_PATH="$D/kiko.db" python3 "$D/fb.py"
KIKO_SELFTEST_FALLBACK=x KIKO_DB_PATH="$D/kiko.db" python3 "$D/fb.py"
KIKO_KEYSTORE_IMPL=dcop KIKO_DB_PATH="$D/kiko.db" python3 "$D/fb.py"
rm -rf "$D"
```

Expected, in order:

```
source: dotenv     (nothing in env, nothing in any store: falls back to .env)
source: env        (environment wins over .env)
source: dotenv     (even with a forced backend that finds nothing, or is unavailable, .env still answers)
```

Run from the repo root (the script imports `kiko_lite` from the current folder). Pass criterion: all three lines as shown. The value is never printed.

## 8. Troubleshooting

### 8.1 No D-Bus session (SSH or console login)

Symptom: probe `secret-tool` says `no DBUS_SESSION_BUS_ADDRESS`, or calls time out. Secret Service and the DCOP server belong to the desktop session. Fix: run the test in a terminal window inside the graphical session on the machine itself. Do not start your own `dbus-launch` / daemons; if only SSH is possible, report "no desktop session reachable" and stop.

### 8.2 Wallet or keyring locked / password or "allow access" window

Symptom: round trip steps fail and the console shows `TIMED OUT` after 20 s, or a window is visible. Action: ask the human to enter the password or answer the KWallet "allow access" (Allow once / Always / Deny) window, then rerun section 3. If it times out again with no window, rerun once with `--timeout 60`, then report.

### 8.3 `dcop` not found

Symptom: `dcop not on PATH`. TDE normally provides it. Report `command -v dcop`, `ls /opt/trinity/bin 2>&1 | head` (TDE often installs under `/opt/trinity`), and `echo $PATH`. If the file exists there, rerun with `PATH=/opt/trinity/bin:$PATH python3 tools/linux_keystore_check.py ...`.

### 8.4 dcop server not reachable

Symptom: `ERROR: Couldn't attach to DCOP server!` in the diagnostics. The DCOP server runs per desktop session and needs the same user, `DISPLAY` and `ICEAUTHORITY`. Run as the logged-in desktop user, not through `sudo`/`su`.

### 8.5 Signature mismatch

Symptom: `DcopStore assumptions` shows MISMATCH/MISSING, or `set value 1` fails while `dcop kded kwalletd` works. Collect the `functions` output (section 6) and report. Do not edit `kikolib/keystore.py`.

### 8.6 kwallet backend

`kwallet-query` belongs to KDE Plasma (5/6), not TDE. On Mint it is normally absent; a `no` probe is the expected result.

### 8.7 Leftover entries

A failed run can leave `KIKO_SELFTEST_*` entries. Tell the human their names (they are in the console output and JSON). For `secret-tool` the human may remove one with `secret-tool clear kiko-identity <NAME>`; never run it for a name you did not create in this run.

## 9. Pass / fail criteria

The test PASSES on a machine when:

1. The script ran to the end and wrote its JSON (no Python traceback).
2. Exit code 0 (at least one backend passed the complete round trip), and the passing backend is the expected one from 3.2. Exception: Mint without `secret-tool` where the human declined the install is `BLOCKED` (section 2.3), not FAIL, and exit code 1 is expected there.
3. In the JSON `passed_backends` contains that backend and no secret value appears in any file you send back.
4. Section 4: forcing each backend that probed ok passes (skipped lines are not counted), and the `bogus` value printed `False` without a traceback.
5. Section 7 printed `dotenv`, `env`, `dotenv`.
6. Every leftover entry is listed for the human (kwallet: always; any backend with `--keep` or a failed delete).

It is a FAIL (report all details, change nothing) when no backend passes on that machine, a step reports `[FAIL]`, a traceback occurs, or a secret value shows up in any output. A backend that is not installed is "unavailable", not a FAIL, as long as the machine's expected backend passes.

On Q4OS TDE a FAIL of `dcop` is a valuable result: send the full JSON, especially `dcop_diagnostics`.
