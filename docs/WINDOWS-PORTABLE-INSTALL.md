# Windows portable install

Every app lives in its own folder, `%USERPROFILE%\apps\<name>`. Nothing is written to the registry or to
AppData, with two exceptions: Start-menu shortcuts in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\apps\` and optional Startup shortcuts. Every shortcut is
recorded. Delete the folder and the recorded shortcuts, and the app is gone.

Code: `tools/win/` (standard library only, Python 3.12+, run with `-I`; the path logic is pathlib, only
`winlnk.py` is Windows-only).

## Usage

    python -I tools/win/install.py kikolib [--dest DIR]
    python -I tools/win/install.py kiko [--dest DIR] [--rex-src DIR] [--kikolib DIR]
    python -I %USERPROFILE%\apps\kikolib\uninstall.py [--purge]

- Default destination: `%USERPROFILE%\apps\<target>`.
- Installing over an existing install first uninstalls it (runtime data is kept). If a recorded file was
  modified, the old install is not touched and the installer stops.
- A destination folder that is not empty and has no record is refused.
- Tests: `python -I tools/win/tests/test_portable.py` (unittest only; installs into temp folders; the .lnk
  test is skipped on non-Windows systems).

## Files in an app folder

- `install-record.json`: what the installer created.
- `uninstall.py`: a copy of `tools/win/uninstall.py`, so uninstall works without the repo.
- the app files; runtime data in `data\` and `logs\` (never recorded).

## Record format

    {"app": "kikolib", "version": "0.2.0",
     "source": {"repo": "kiko-src", "commit": "<git HEAD sha>"},
     "installed_at": "2026-11-24T10:00:00+0100", "python": "3.14.7",
     "created": [{"path": "kikolib/http.py", "kind": "file", "sha256": "..."},
                 {"path": "kikolib", "kind": "dir", "sha256": ""},
                 {"path": "Microsoft/Windows/Start Menu/Programs/apps/X.lnk", "kind": "shortcut", "sha256": ""}]}

Paths of files and dirs are relative to the app folder (forward slashes). Shortcut paths are relative to
`%APPDATA%` and expanded when uninstalling.

## Uninstall

Order: shortcuts first, then files, then empty directories. A file whose sha256 changed is kept and
reported (`KEPT (modified)`); the record stays so uninstall can be repeated. `data\` and `logs\` are removed
only with `--purge`. When nothing is kept, the record, `uninstall.py` and the empty app folder are removed.
Exit code 0 = clean, 1 = something was kept.

## Targets

`install.py` has a table `TARGETS = {name: function(dest) -> Installation}`; add tray and sisi there. A target may also have a `POST` step (runtime data, not recorded).

- `kikolib`: copies `gateway/kikolib` from git HEAD (`git archive`, not the working tree) into
  `apps\kikolib\kikolib\`, then runs `gateway/tools/kikolib_manifest.py check` on the copy. A mismatch rolls
  the install back and fails.

## Shortcuts (`winlnk.py`)

`create_shortcut(lnk, target, args, workdir, description)` writes a `.lnk` through ctypes COM
(CLSID_ShellLink, IShellLinkW, IPersistFile), reads it back and compares it. If COM fails it writes a
`.url` file instead and reports `kind: "url"`. `read_shortcut` and `delete_shortcut` complete the round trip.
Callers record the shortcut with `Installation.add_shortcut(<%APPDATA%-relative path>)`.
- `kiko`: needs kikolib installed (`apps\kikolib\install-record.json`, else it stops). Layout in `apps\kiko\`:
  `kiko-lite\` (the bundle built from git HEAD by `gateway/tools/kiko_lite_bundle.py`, without its own `kikolib`;
  kikolib is reached through PYTHONPATH `apps\kikolib`), `gateway\scripts\claude_cli_gateway.py` and
  `gateway\runtimelib\procutil.py` (from the rex repo's git HEAD, `--rex-src`, default `~/rex`; its working tree
  is never read, the rex repo is not touched), `kiko_chain.py`, and the launchers `kiko-chain.cmd` /
  `kiko-chain-stop.cmd` (they name the installing Python by absolute path). `--kikolib DIR` overrides
  `<dest>/../kikolib` for the install only; at run time `kiko_chain.py` uses `../kikolib` or env
  `KIKO_KIKOLIB_DIR`.

## The kiko chain

`kiko_chain.py` (`python -I kiko_chain.py [--detach | --stop | --init]`, or the `.cmd` launchers) starts the Claude
CLI gateway on 127.0.0.1:4100, waits for it, then kiko-lite on 127.0.0.1:4000, both as children, logs in
`data\logs\`. When it ends (Ctrl-C, `--stop`, or one child dying) both are stopped. Auth is always on: the
gateway needs `data\gwtoken` (passed with `--token-file`; kiko-lite gets it as `CGW_TOKEN`), clients need
`data\masterkey` (`x-api-key` or Bearer). Env set for kiko-lite: `KIKO_CONFIG_DIR=data`,
`KIKO_DB_PATH=data\kiko.db`, `CGW_TOKEN`, `MASTER_KEY`, `PYTHONDONTWRITEBYTECODE=1` (no `__pycache__` inside
the recorded folders). `--stop` kills the pid in `data\chain.pid` (and its children). Only Anthropic models
(haiku, sonnet, opus); the provider/model steps are those of `CLAUDE-CHAIN-WINDOWS.md`, run by the installer
(`--init`) and idempotent.

`data\` (tokens, `kiko.db`, logs, pid) is runtime data: not in the record, kept on reinstall and uninstall,
removed only with `--purge`. The earlier test chain in `%USERPROFILE%\.kiko-chain` is retired (not deleted);
the installed chain replaces it on the same ports 4000/4100.

Limits: the gateway needs the `claude` CLI on PATH and logged in. If a chain is already listening on 4000 or
4100, `kiko_chain.py` refuses to start.
