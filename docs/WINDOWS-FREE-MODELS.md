# Windows: free-tier models on the live kiko (port 4000)

Date: 2026-11-26. Applies to the portable Windows install under `%USERPROFILE%\apps\kiko`.

## What changed

The live gateway is the **kiko chain** (`apps\kiko\kiko_chain.py`): a Claude gateway on
`127.0.0.1:4100` plus `kiko-lite serve` on `127.0.0.1:4000`. Its database is
`%USERPROFILE%\apps\kiko\data\kiko.db` (the config dir is `apps\kiko\data`; the
`~/.kiko/kiko.db` file is **not** used by the chain).

Into that live DB I installed the verified free catalog from `docs/MODELS.md`, the same way
`tray/tools/setup-models.sh` does, using the installed kiko-lite CLI. **Only key NAMES were
written; no key values were read, copied or stored.** Added: provider `openrouter` and 13 models.

- **No restart was needed.** kiko-lite re-reads the DB about every 2 seconds, so the running
  `:4000` picked up the new models live. Liveness was confirmed (`/ui` = 200, auth gate = 401).
- Keys already in Windows Credential Manager: `NVIDIA_API_KEY`, `GEMINI_API_KEY` → their 9
  models are **ON**.
- Keys not yet stored: `MISTRAL_API_KEY`, `OPENROUTER_API_KEY` → their 4 models are **OFF**
  (`ministral-3b`, `codestral`, `router-mini`, `gpt-astra`), so nothing routes to a model
  without a key.

### Routing impact (verified)

- **Unchanged for current users:** class aliases `opus`, `sonnet`, `haiku` still resolve to the
  Claude-gateway models. The free models sit at the same priority but lose on status/id, so the
  Claude models keep winning.
- **New, now live:** `fable` → `muse-glimmer`, `eyes` → `nemotron-vl`, `tutor` → `gemini-tutor`
  (these classes had no model before).
- **New direct aliases usable now:** `nemotron-ultra`, `nemotron-super`, `llama-nemotron`,
  `nvidia-large`, `muse-glimmer`, `nemotron-vl`, `gemini-lite`, `gemini-35-lite`, `gemini-tutor`.
- **Inactive until keys stored:** the 4 OFF models above; classes `kiko-coder` and `kiko-router`
  have no model until Mistral is keyed. A request to a disabled alias gives a clean 404; a keyed
  model that cannot be reached gives a clean error, never a crash.

## Backups (in `apps\kiko\data`, made before any change)

- `kiko.db.bak-20261126-121658`, `kiko.db-wal.bak-20261126-121658`, `kiko.db-shm.bak-20261126-121658`
- `kiko.db.snapshot-20261126-121658` (a consistent single-file sqlite copy)

## How kiko finds a key on Windows (names only)

For a model's `api-key-env` NAME, kiko looks in order: (1) environment variable NAME,
(2) **Windows Credential Manager** generic credential, target `KIKO/KIKO_IDENTITY/NAME`,
(3) file `apps\kiko\data\.env` (`NAME=value`). First hit wins. The four targets are:

    KIKO/KIKO_IDENTITY/NVIDIA_API_KEY      KIKO/KIKO_IDENTITY/GEMINI_API_KEY
    KIKO/KIKO_IDENTITY/OPENROUTER_API_KEY  KIKO/KIKO_IDENTITY/MISTRAL_API_KEY

## Owner: store the two missing keys

Get fresh keys first; never paste a key into chat or git. Each line below **prompts for the
value with echo off** and writes it straight to Credential Manager — the value is never shown,
never on the command line, never on disk:

    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I -c "import sys,getpass;sys.path.insert(0,r'%USERPROFILE%\apps\kikolib');from kikolib import keystore;n='MISTRAL_API_KEY';keystore.set_secret(n,getpass.getpass(n+': '));print('stored',n)"

    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I -c "import sys,getpass;sys.path.insert(0,r'%USERPROFILE%\apps\kikolib');from kikolib import keystore;n='OPENROUTER_API_KEY';keystore.set_secret(n,getpass.getpass(n+': '));print('stored',n)"

Then enable that provider's models (the running `:4000` picks it up in ~2 s):

    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I -c "import sqlite3;c=sqlite3.connect(r'%USERPROFILE%\apps\kiko\data\kiko.db');c.execute(\"UPDATE models SET enabled=1 WHERE alias IN ('ministral-3b','codestral','router-mini')\");c.commit();print('mistral on')"

    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I -c "import sqlite3;c=sqlite3.connect(r'%USERPROFILE%\apps\kiko\data\kiko.db');c.execute(\"UPDATE models SET enabled=1 WHERE alias='gpt-astra'\");c.commit();print('openrouter on')"

(`NVIDIA_API_KEY` and `GEMINI_API_KEY` are already stored — nothing to do for them.)

## Verify after (one call per provider, through the gateway)

The master key is read from its file inline and is not printed. Change `model` to test each
provider: `nemotron-ultra` (NVIDIA), `gemini-lite` (Gemini), `ministral-3b` (Mistral),
`gpt-astra` (OpenRouter).

    ! curl -s http://127.0.0.1:4000/v1/messages -H "x-api-key: $(cat ~/apps/kiko/data/masterkey)" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"nemotron-ultra","max_tokens":16,"messages":[{"role":"user","content":"Reply with exactly: OK"}]}'

List every served alias (Claude + keyed free models):

    ! curl -s http://127.0.0.1:4000/v1/models -H "x-api-key: $(cat ~/apps/kiko/data/masterkey)"

A Claude alias still answers as before:

    ! curl -s http://127.0.0.1:4000/v1/messages -H "x-api-key: $(cat ~/apps/kiko/data/masterkey)" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"sonnet","max_tokens":16,"messages":[{"role":"user","content":"Reply with exactly: OK"}]}'

## Rollback

Soft (no restart) — turn every free model off, keep the 3 Claude models:

    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I -c "import sqlite3;c=sqlite3.connect(r'%USERPROFILE%\apps\kiko\data\kiko.db');c.execute(\"UPDATE models SET enabled=0 WHERE alias NOT IN ('opus','sonnet','haiku')\");c.commit();print('free models off')"

Full — restore the pre-change database (stop the chain, restore, start):

    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I "%USERPROFILE%\apps\kiko\kiko_chain.py" --stop
    ! cp -f "~/apps/kiko/data/kiko.db.bak-20261126-121658" "~/apps/kiko/data/kiko.db" && rm -f "~/apps/kiko/data/kiko.db-wal" "~/apps/kiko/data/kiko.db-shm"
    ! "%USERPROFILE%\tools\python-3.14.7\python.exe" -I "%USERPROFILE%\apps\kiko\kiko_chain.py" --detach

---
Sources: `docs/MODELS.md` (verified free catalog, 2026-11-22); `tray/tools/setup-models.sh`;
live install `%USERPROFILE%\apps\kiko` (kiko_chain.py, data/kiko.db) — read 2026-11-26.

## Context sizes (2026-11-26)

The live DB is now schema v9 and every model row carries `context_window` / `max_output`; the full table with sources is in `docs/MODELS.md` ("Context window and max output"). The 20 llmhub models were imported as `hub-*` rows (priority 900); only `hub-gemini-3.5-flash-lite` answered and stayed enabled.
