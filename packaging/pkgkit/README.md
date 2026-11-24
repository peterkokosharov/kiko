# pkgkit

Version 0.1.0 (Windows part only). Standard library only; build needs Python 3.11+.

## What is here

- `build.py` - builds `<name>-<ver>-win-x64.zip` from a project's `app.toml`; excludes tests, lists the zip, fails if a
  test file is inside.
- `install.py`, `uninstall.py`, `winlnk.py`, `manifest.py` - copied into the zip's `_pkg/` and run there.
- `install.cmd.in`, `uninstall.cmd.in` - LF templates; `build.py` fills them in and writes CRLF `.cmd` files.
- `SHA256SUMS`, `CHANGES.md`, `__init__.py` (`__version__`).

Zip layout: `<name>-<ver>-win-x64/` with `install.cmd`, `uninstall.cmd`, `README.txt`, `payload/`, `_pkg/`.
`install.cmd` finds Python (`py -3`, then `python`), checks the minimum version, then runs `_pkg\install.py`: Windows 10+,
payload sha256 against `_pkg\MANIFEST`, copy to `%USERPROFILE%\apps\<name>`, Start-menu shortcut under `Programs\apps`,
`install-record.json`, the app's own check command. Prints `OK <name> <ver> installed in <dir>` or `FAILED: <reason>`
and rolls back. `uninstall.cmd` removes exactly what the record lists (changed files KEPT, locked files retried then KEPT),
then deletes itself; `data\` and `logs\` only with `/purge`.

## What it is for

One packaging method for every kiko-family Windows app, so each project only supplies `packaging/app.toml`.
Consumers copy this folder unmodified (like kikolib): to `packaging/vendor/pkgkit/`, with `eol=lf` in `.gitattributes`
and a test that runs `python manifest.py check <copy>`. Never edit the copy: fix it in kiko-src, bump the version, add a
`CHANGES.md` entry, run `python manifest.py write`, tag `pkgkit-vX.Y.Z`, re-copy.

`app.toml` keys: `[app]` name, description, min_python, version_file (file with `__version__ = "x"`);
`[payload]` dir (folder name inside the install dir), include (repo paths copied under it), `[[payload.file]]` src/dest;
`[shortcut]` name, target (relative to the install dir), args, console; `[check]` cmd; optional `[readme]` extra.
Build: `python -I build.py --root REPO --app REPO/packaging/app.toml [--out DIR] [--extra SRC=DEST]`.

## What a human does

Build the zip with the command above, unzip it, run `install.cmd`; to remove, run `uninstall.cmd`. Release: run the tests,
`python manifest.py write`, commit, tag `pkgkit-vX.Y.Z` locally.

## What an LLM does

Run `tools/run_tests.py` (in kiko-src). Green: report OK. Red: check that http://localhost:4000/health answers; if kiko
works, report "there is a problem" with the failing output. Never try to fix it.

Author: Peter Kokosharov. AI models: claude-sonnet-5-5
