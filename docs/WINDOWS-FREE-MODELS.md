# Windows: adding free-tier models to a kiko-lite install

Applies to a portable Windows install of kiko-lite (for example `%USERPROFILE%\apps\kiko`).
The examples use `%USERPROFILE%` (cmd) or `~` (Git Bash); adjust them to your layout.

## Where the database lives

kiko-lite reads one SQLite database in its config directory (`<install>\data\kiko.db`). When
kiko runs behind a chain launcher, that file is the live one. kiko-lite re-reads the database
about every 2 seconds, so changes made with the CLI apply to a running server without a restart.

## Installing the free catalog

The verified free catalog is listed in `docs/MODELS.md`. `tray/tools/setup-models.sh` installs
it through the kiko-lite CLI. Only key NAMES are written to the database, never key values.
Models whose key is not stored are added disabled, so nothing routes to a model without a key.

Routing impact:

- Class aliases such as `opus`, `sonnet` and `haiku` keep resolving to their existing models;
  free models at the same priority lose on status/id.
- Classes that had no model before (for example `fable`, `eyes`, `tutor`) now resolve to a
  free model, and each model is also reachable by its direct alias.
- A request to a disabled alias returns a clean 404; a keyed model that cannot be reached
  returns a clean error, never a crash.

## Back up first

Before changing the database, copy `kiko.db` together with its `-wal` and `-shm` files, or make
a consistent single-file copy with `sqlite3 kiko.db ".backup kiko.db.snapshot"`.

## How kiko finds a key on Windows (names only)

For a model's `api-key-env` NAME, kiko looks in order: (1) environment variable NAME,
(2) a Windows Credential Manager generic credential with target `KIKO/KIKO_IDENTITY/NAME`,
(3) the file `<install>\data\.env` (`NAME=value`). The first hit wins. Example targets:

    KIKO/KIKO_IDENTITY/NVIDIA_API_KEY      KIKO/KIKO_IDENTITY/GEMINI_API_KEY
    KIKO/KIKO_IDENTITY/OPENROUTER_API_KEY  KIKO/KIKO_IDENTITY/MISTRAL_API_KEY

## Storing a key

Never paste a key into chat or git. This prompts for the value with echo off and writes it
straight to Credential Manager, so the value is never shown and never on the command line:

    python -I -c "import sys,getpass;sys.path.insert(0,r'%USERPROFILE%\apps\kikolib');from kikolib import keystore;n='MISTRAL_API_KEY';keystore.set_secret(n,getpass.getpass(n+': '));print('stored',n)"

Then enable that provider's models with the kiko-lite CLI (see its `--help`) or in the web UI;
the running server picks the change up in about 2 seconds.

## Verifying

Send one request per provider through the gateway. Read the master key from its file in the
config directory so it is not printed, and change `model` to each provider's alias:

    curl -s http://127.0.0.1:4000/v1/messages -H "x-api-key: <master key>" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"<alias>","max_tokens":16,"messages":[{"role":"user","content":"Reply with exactly: OK"}]}'

List every served alias:

    curl -s http://127.0.0.1:4000/v1/models -H "x-api-key: <master key>"

## Rollback

Soft (no restart): disable every added model in the web UI or CLI and keep your original
models. Full: stop the server, restore the backed-up `kiko.db` (and delete any stale `-wal`
and `-shm` files), then start the server again.

## Context sizes

Every model row carries `context_window` / `max_output`; the full table with sources is in
`docs/MODELS.md` ("Context window and max output").

---
Sources: `docs/MODELS.md` (verified free catalog, 2026-11-22); `tray/tools/setup-models.sh`.
